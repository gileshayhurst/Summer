import concurrent.futures
import json
import os
import shutil
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Load ANTHROPIC_API_KEY from a .env file in the project root if not already set.
_env_file = Path(__file__).parent / ".env"
if not os.environ.get("ANTHROPIC_API_KEY") and _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8-sig").splitlines():
        _line = _line.strip()
        if _line.startswith("ANTHROPIC_API_KEY=") and not _line.startswith("#"):
            os.environ["ANTHROPIC_API_KEY"] = _line.split("=", 1)[1].strip().strip('"').strip("'")
            break

# WinGet installs ffmpeg to a user-local path that isn't on the subprocess PATH.
# Guard to Windows only — Linux containers find ffmpeg via the system PATH (apt install).
import sys as _sys
if not shutil.which("ffmpeg") and _sys.platform == "win32":
    _winget_base = Path.home() / "AppData/Local/Microsoft/WinGet/Packages"
    for _bin in sorted(_winget_base.glob("Gyan.FFmpeg*/*/bin")):
        os.environ["PATH"] = str(_bin) + os.pathsep + os.environ.get("PATH", "")
        break

from flask import Flask, jsonify, redirect, render_template, request, send_file
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import render_jobs
from claude_client import query_claude
from loader import scan_videos
from timestamp_parser import parse_scored_timestamps
from transcriber import transcribe_video
from video_editor import parse_timestamp_to_seconds
from shared import (
    read_transcript as _read_transcript,
    parse_transcript_lines as _parse_transcript_lines,
    filter_generated_reels as _filter_generated_reels,
    group_lines_into_segments as _group_lines_into_segments,
    lines_in_range as _lines_in_range,
    transcript_tier as _transcript_tier,
)
import storage
import forven_api
import forven_config
import forven_deliver
import forven_ingest
import sz_store

RECENT_FOLDERS_PATH = Path(__file__).parent / "recent_folders.json"
PROMPT_HISTORY_PATH = Path(__file__).parent / "prompt_history.json"

# Maps session_key → local temp dir for the duration of the process
_cloud_session_dirs: dict[str, str] = {}
_cloud_session_ready: dict[str, threading.Event] = {}
_cloud_session_lock = threading.Lock()

_jobs: dict = {}
_jobs_lock = threading.Lock()
_recent_folders_lock = threading.Lock()
_prompt_history_lock = threading.Lock()
_whisper_models: dict = {}
_model_lock = threading.Lock()
_WHISPER_CACHE_DIR = os.environ.get(
    "WHISPER_CACHE_DIR", str(Path(__file__).parent / ".whisper_cache")
)


_VIDEO_KEY_SUFFIXES = {".mp4", ".mov", ".avi", ".mkv", ".webm"}


def _session_video_pairs(keys: list[str]) -> list[tuple[str, str]]:
    """(video_key, transcript_key) for every video with a same-stem .txt.

    Mirrors encoder.job.find_pairs. Kept separate from the tier check so callers
    can distinguish "this session has no transcripts at all" from "its
    transcripts are already rich" — two very different situations that were
    previously reported to the user with the same cheerful message.
    """
    texts = {k.rsplit(".", 1)[0]: k for k in keys if k.lower().endswith(".txt")}
    pairs = []
    for key in keys:
        stem, _, suffix = key.rpartition(".")
        if f".{suffix.lower()}" in _VIDEO_KEY_SUFFIXES and stem in texts:
            pairs.append((key, texts[stem]))
    return sorted(pairs)


def _sessions_needing_encode(keys: list[str]) -> list[str]:
    """Video keys in a session whose transcript is still plain-tier.

    Decides tier through shared.transcript_tier so this app and the encoder
    agree on what "rich" means. Used to check whether dispatching a job would do
    any work at all — which keeps a repeat call free and stops an
    unauthenticated caller manufacturing cost against arbitrary session keys.
    """
    pending = []
    for video_key, text_key in _session_video_pairs(keys):
        try:
            text = storage.read_file_bytes(text_key).decode("utf-8-sig")
        except Exception:
            continue  # unreadable transcript: leave it alone rather than guess
        if _transcript_tier(_parse_transcript_lines(text)) != "rich":
            pending.append(video_key)
    return sorted(pending)


def _compute_transcription_parallelism(cpu_count: int, num_videos: int) -> tuple[int, int]:
    """Return (workers, cpu_threads) for parallel transcription.

    - workers: how many videos to transcribe concurrently. Capped at the video
      count and at half the cores, leaving room for each job's internal threads.
    - cpu_threads: CTranslate2 threads per job, dividing cores evenly across workers.

    Invariant: workers * cpu_threads <= cpu_count.
    """
    cpu_count = max(1, cpu_count)
    num_videos = max(1, num_videos)
    workers = min(num_videos, max(1, cpu_count // 2))
    cpu_threads = max(1, cpu_count // workers)
    return workers, cpu_threads


def _get_whisper_model(cpu_threads: int = 0, num_workers: int = 1):
    """Return a cached faster-whisper base model configured for the given thread layout.

    Cached by (cpu_threads, num_workers) so warm jobs reuse the model. compute_type
    int8 gives the CPU speedup; download_root pins weights so cold boots don't re-fetch.
    """
    key = (cpu_threads, num_workers)
    model = _whisper_models.get(key)
    if model is None:
        with _model_lock:
            model = _whisper_models.get(key)
            if model is None:
                from faster_whisper import WhisperModel
                model = WhisperModel(
                    "base",
                    device="cpu",
                    compute_type="int8",
                    cpu_threads=cpu_threads,
                    num_workers=num_workers,
                    download_root=_WHISPER_CACHE_DIR,
                )
                _whisper_models[key] = model
    return model


def _new_job(job_type: str, total: int) -> str:
    job_id = str(uuid.uuid4())
    with _jobs_lock:
        _jobs[job_id] = {
            "type": job_type,
            "status": "running",
            "total": total,
            "done": 0,
            "log": [],
            "result": None,
            "error": None,
            "cancel": threading.Event(),
        }
    return job_id


def _append_log(job_id: str, message: str) -> None:
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id]["log"].append(message)


def _pick_directory() -> str | None:
    """Open a native OS folder dialog. Returns the selected path or None."""
    import tkinter as tk
    from tkinter import filedialog
    result: dict = {"path": None}

    def run() -> None:
        root = tk.Tk()
        root.withdraw()
        root.wm_attributes("-topmost", True)
        result["path"] = filedialog.askdirectory(parent=root) or None
        root.destroy()

    t = threading.Thread(target=run)
    t.start()
    t.join()
    return result["path"]



def _group_by_minute(lines: list[dict]) -> list[dict]:
    buckets: dict[int, list] = {}
    for line in lines:
        b = line["minute_bucket"]
        buckets.setdefault(b, []).append(line)
    result = []
    for b in sorted(buckets):
        result.append({
            "bucket": b,
            "label": f"{b}:00 – {b + 1}:00",
            "lines": buckets[b],
        })
    return result



def _load_recent_folders() -> list:
    if not RECENT_FOLDERS_PATH.exists():
        return []
    try:
        with RECENT_FOLDERS_PATH.open(encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def _save_recent_folder(folder: str, video_count: int) -> None:
    """Prepend folder to recent_folders.json, deduplicate by path, keep max 5."""
    with _recent_folders_lock:
        entries = [e for e in _load_recent_folders() if e.get("path") != folder]
        entries.insert(0, {
            "path": folder,
            "video_count": video_count,
            "last_opened": datetime.now().isoformat(timespec="seconds"),
        })
        entries = entries[:5]
        try:
            with RECENT_FOLDERS_PATH.open("w", encoding="utf-8") as f:
                json.dump(entries, f, indent=2, ensure_ascii=False)
        except OSError:
            pass  # history is best-effort; never fail a load-folder for this



def _load_prompt_history() -> dict:
    if not PROMPT_HISTORY_PATH.exists():
        return {"recent": [], "templates": []}
    try:
        with PROMPT_HISTORY_PATH.open(encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"recent": [], "templates": []}


def _save_prompt_history(data: dict) -> None:
    try:
        with PROMPT_HISTORY_PATH.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except OSError:
        pass


def _prompt_history_use(text: str) -> None:
    with _prompt_history_lock:
        data = _load_prompt_history()
        recent = [t for t in data.get("recent", []) if t != text]
        recent.insert(0, text)
        data["recent"] = recent[:10]
        _save_prompt_history(data)


def _run_analyze(folder: str, prompt: str) -> dict:
    """Call Claude on every transcript in folder. Returns per-video scored
    segments plus a legacy `highlights` union of the matched lines."""
    try:
        video_paths = scan_videos(folder)
    except Exception as exc:
        return {"error": str(exc)}
    video_paths = _filter_generated_reels(video_paths)

    def _analyze_one(vp: Path) -> tuple[str, list[dict], str | None]:
        """Analyze a single video. Returns (name, segments, error).

        Runs the (slow) Claude call plus timestamp matching for one video so the
        whole folder can be processed concurrently — a folder of many long videos
        analyzed serially takes long enough for the hosting proxy to time out and
        return an HTML error page the frontend can't parse as JSON.
        """
        txt_path = vp.with_suffix(".txt")
        if not txt_path.exists() or txt_path.stat().st_size == 0:
            return vp.name, [], None

        transcript = _read_transcript(txt_path)
        all_lines = _parse_transcript_lines(transcript)
        tier = _transcript_tier(all_lines)

        try:
            response = query_claude(transcript, prompt, tier=tier)
            scored = parse_scored_timestamps(response) or []
        except Exception as exc:
            return vp.name, [], f"{vp.name}: {exc}"

        segments: list[dict] = []
        for seg, score in scored:
            start_str, end_str = seg.split("-", 1)
            start_sec = parse_timestamp_to_seconds(start_str)
            end_sec = parse_timestamp_to_seconds(end_str)
            seg_line_dicts = _lines_in_range(all_lines, start_sec, end_sec)
            lines = list(dict.fromkeys(d["raw"] for d in seg_line_dicts))
            if not lines:
                continue  # segment mapped to no respondent lines — drop it
            # The create-screen length estimate must match the clip the generator
            # will actually cut. The clip duration comes from the same shared
            # grouping the generator uses (shared.group_lines_into_segments).
            #
            # Group over the FULL line list, not just this candidate's lines.
            # The clip end is the first unselected line after the run, which
            # only exists with full context — in isolation every candidate
            # would hit the trailing-run path and be estimated at the ceiling.
            # This also matches how generator_app groups (all_lines), keeping
            # the create-screen estimate aligned with the real cut.
            grouped = _group_lines_into_segments(all_lines, set(lines))
            if grouped:
                clip_start = grouped[0][0]
                clip_dur = sum(e - s for s, e in grouped)
            else:
                clip_start, clip_dur = start_sec, max(0.0, end_sec - start_sec)
            segments.append({
                "start": start_str,
                "end": end_str,
                "start_seconds": clip_start,
                "end_seconds": clip_start + clip_dur,
                "duration_seconds": clip_dur,
                "score": score,
                "lines": lines,
            })

        # Reconcile the per-candidate estimates against what the generator would
        # actually cut. Above, each candidate is grouped in ISOLATION, but
        # generator_app groups the UNION of every selected line across the file
        # (one pass over all_lines). Candidates that sit next to each other with
        # no unselected line between them therefore merge into a single run in
        # the generator and get truncated by the MAX_CLIP_SECONDS ceiling — so
        # the naive sum of candidate durations systematically over-promises.
        # Scale the candidates down by the ratio of merged to solo total, which
        # makes a full selection exact and any prefix far closer.
        if segments:
            union_raws = {raw for seg in segments for raw in seg["lines"]}
            merged = _group_lines_into_segments(all_lines, union_raws)
            total_merged = sum(e - s for s, e in merged)
            total_solo = sum(seg["duration_seconds"] for seg in segments)
            if total_solo > total_merged > 0:
                scale = total_merged / total_solo
                for seg in segments:
                    seg["duration_seconds"] *= scale
                    seg["end_seconds"] = seg["start_seconds"] + seg["duration_seconds"]

        segments.sort(key=lambda s: s["start_seconds"])
        return vp.name, segments, None

    segments_by_file: dict[str, list[dict]] = {}
    highlights: dict[str, list[str]] = {}
    errors: list[str] = []

    # Run the per-video Claude calls concurrently. Wall time collapses from the
    # sum of every call to roughly the slowest single call, keeping the request
    # under the hosting proxy's timeout.
    max_workers = min(8, len(video_paths)) or 1
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        results = list(executor.map(_analyze_one, video_paths))

    for name, segments, error in results:
        segments_by_file[name] = segments
        # Legacy union: preserves the existing `highlights` contract for any
        # caller/test that still reads it.
        union: list[str] = []
        for seg in segments:
            for raw in seg["lines"]:
                if raw not in union:
                    union.append(raw)
        highlights[name] = union
        if error:
            errors.append(error)

    if len(errors) == len(video_paths) and not any(highlights.values()):
        return {"error": "; ".join(errors)}

    return {"segments": segments_by_file, "highlights": highlights}


class SessionDownloadCancelled(Exception):
    """A cloud session download was cancelled via its job's cancel event."""


def _ensure_cloud_session(session_key: str, job_id: str | None = None,
                          cancel_event: threading.Event | None = None) -> str:
    """Download session files from S3 into a local temp dir if not already cached.

    Thread-safe: concurrent callers for the same session_key block until the
    first caller finishes downloading (rather than getting a half-populated dir).

    When job_id/cancel_event are supplied (the async /load-folder path), progress
    is reported to the job between files, and the download aborts with
    SessionDownloadCancelled when the event is set. On cancel the cache entries
    are removed BEFORE waiters are released, so a retry re-downloads cleanly and
    any waiter wakes to a missing entry and raises SessionDownloadCancelled too.
    """
    with _cloud_session_lock:
        if session_key in _cloud_session_dirs:
            event = _cloud_session_ready[session_key]
            is_new = False
        else:
            tmp = tempfile.mkdtemp(prefix="sizzle_session_")
            _cloud_session_dirs[session_key] = tmp
            event = threading.Event()
            _cloud_session_ready[session_key] = event
            is_new = True

    if not is_new:
        event.wait()          # block until the first caller finishes
        with _cloud_session_lock:
            cached = _cloud_session_dirs.get(session_key)
        if cached is None:    # first caller was cancelled and cleaned up
            raise SessionDownloadCancelled(session_key)
        return cached

    tmp = _cloud_session_dirs[session_key]
    try:
        # The main app only ever reads .txt sidecars (scan_videos merely enumerates
        # filenames; analyze/transcripts read transcripts). Downloading the video
        # bytes would pile hundreds of MB per session into Render's /tmp and blow the
        # 2GB ephemeral-disk limit. So download only transcripts; give each video a
        # 0-byte placeholder so scan_videos still lists it.
        keys = storage.list_keys(session_key + "/")
        if job_id is not None:
            txt_total = sum(1 for k in keys if Path(k).suffix.lower() == ".txt")
            with _jobs_lock:
                if job_id in _jobs:
                    _jobs[job_id]["total"] = txt_total
        done = 0
        for key in keys:
            if cancel_event is not None and cancel_event.is_set():
                raise SessionDownloadCancelled(session_key)
            filename = Path(key).name
            dest = os.path.join(tmp, filename)
            if Path(filename).suffix.lower() == ".txt":
                storage.download_file(key, dest)
                done += 1
                if job_id is not None:
                    with _jobs_lock:
                        if job_id in _jobs:
                            _jobs[job_id]["done"] = done
            else:
                Path(dest).touch()
    except SessionDownloadCancelled:
        # Remove the cache entries BEFORE the finally releases waiters, so
        # waiters see the missing entry (= cancelled) rather than a
        # half-populated dir, and a retry re-downloads.
        with _cloud_session_lock:
            _cloud_session_dirs.pop(session_key, None)
            _cloud_session_ready.pop(session_key, None)
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    finally:
        event.set()           # release waiters even if download failed
    return tmp


def _scan_load_folder(folder: str) -> tuple[dict | None, str | None]:
    """Scan `folder` and apply every load-folder filter.

    Shared by the synchronous /load-folder path and the cloud session_download
    job thread. Returns (result, error): exactly one is non-None. result is
    {"folder", "files", "needs_transcription"} where needs_transcription is a
    list of video Paths lacking a non-empty .txt transcript.
    """
    try:
        video_paths = scan_videos(folder)
    except ValueError as e:
        return None, str(e)

    video_paths = _filter_generated_reels(video_paths)
    if not video_paths:
        return None, "No source video files found (folder contains only previously generated reels)"

    # Check the sidecar for reels generated into this specific folder.
    # In cloud mode this catches reels that were generated locally and then
    # re-uploaded; in local mode it catches reels not yet in the library
    # (e.g. library cleared) or downloaded from a different session.
    locally_generated: set[str] = set()
    sidecar = Path(folder) / "sizzle_generated_reels.txt"
    if sidecar.exists():
        try:
            locally_generated = set(sidecar.read_text(encoding="utf-8").splitlines())
        except Exception:
            pass
    if locally_generated:
        video_paths = [p for p in video_paths if p.name not in locally_generated]
        if not video_paths:
            return None, "No source video files found (folder contains only previously generated reels)"

    # In cloud mode Whisper is not available — only videos with pre-supplied
    # .txt transcripts can be used.
    if storage.is_cloud():
        video_paths = [p for p in video_paths
                       if p.with_suffix(".txt").exists()
                       and p.with_suffix(".txt").stat().st_size > 0]
        if not video_paths:
            return None, "No transcripts found. In cloud mode, upload a .txt transcript alongside each video."

    _save_recent_folder(folder, len(video_paths))
    filenames = [p.name for p in video_paths]
    needs_transcription = [p for p in video_paths
                           if not p.with_suffix(".txt").exists()
                           or p.with_suffix(".txt").stat().st_size == 0]
    return {"folder": folder, "files": filenames,
            "needs_transcription": needs_transcription}, None


def _valid_session_folder(folder: str) -> bool:
    """Cloud mode: a client-supplied folder must be an upload-session key under
    `sessions/`. Rejecting anything else stops a real server path (e.g. /etc)
    from being passed in to read arbitrary local files. No-op in local mode,
    which is a trusted single-user desktop app.
    """
    if not storage.is_cloud():
        return True
    return bool(folder) and folder.startswith("sessions/")


def create_app(testing: bool = False) -> Flask:
    app = Flask(__name__)
    app.config["TESTING"] = testing

    # ponytail: in-memory limiter storage — per-instance, resets on restart.
    # Fine on Render's single free-tier instance; move to Redis if it scales out.
    # Keyed by client IP; enabled only in cloud (local desktop app is unmetered).
    limiter = Limiter(key_func=get_remote_address, app=app,
                      default_limits=["600 per hour"])
    app.config["RATELIMIT_ENABLED"] = storage.is_cloud()
    # Cap request bodies the host buffers. Large video bytes go browser->R2 via
    # presigned PUT and never hit this host (see /upload/prepare).
    app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

    # The sz_ tables have to exist before the first request touches them, and
    # this app has no migration step. CREATE TABLE IF NOT EXISTS is idempotent,
    # so every boot re-asserts the schema harmlessly. A database that is down
    # must not take the whole app with it: everything except the Forven pages
    # works without one.
    if not testing and sz_store.is_configured():
        try:
            sz_store.init_schema()
        except Exception:
            app.logger.exception("could not create the Sizzle Reel schema")

    @app.get("/")
    def index():
        # Interviews come from Forven, so that is where you start. This screen
        # is the workspace for a session someone already chose; arriving with
        # no session means they have not chosen one yet.
        if not request.args.get("session"):
            return redirect("/forven")
        return render_template(
            "index.html",
            app_mode=os.environ.get("APP_MODE", "local"),
            generator_url=os.environ.get("GENERATOR_URL", "http://localhost:5001"),
        )

    # --- Forven platform integration -------------------------------------
    # Interviews come from the Forven Video Access API. WHICH tenants to read
    # from and deliver to is data (sz_tenant_pairs), not deployment config, so
    # one deployment can serve several organisations. Only the API keys are
    # environment variables - they are the application's credentials.
    #
    # Listing is metadata only: the list endpoint returns no participant names,
    # emails, or transcript text. Pulling fetches transcripts and media, which
    # is where the retention obligation starts.

    def _resolve_pair(pair_id=None):
        """Return (pair, source, destination) for the requested tenant pair.

        Falls back to the environment-only configuration when no pair has been
        set up yet, so a fresh deployment still works.
        """
        pair = None
        if sz_store.is_configured():
            try:
                pair = (sz_store.get_tenant_pair(int(pair_id)) if pair_id
                        else sz_store.default_tenant_pair())
            except Exception:
                app.logger.exception('could not resolve tenant pair')
                pair = None
        if pair:
            source, destination = forven_config.endpoints_for_pair(pair)
        else:
            source = forven_config.source_config()
            destination = forven_config.destination_config()
        return pair, source, destination

    def _connection_status(role, endpoint, expected_name):
        """Describe one tenant, echoing back the org it actually resolves to.

        A wrong-but-valid tenant id returns another org's data silently, so the
        echo is surfaced rather than trusted.
        """
        client = forven_api.ForvenClient(endpoint.base_url, endpoint.api_key)
        try:
            page = client.list_interviews(endpoint.tenant_public_id, page_size=1)
        except forven_api.ForvenApiError as exc:
            return {"role": role, "tenant_name": None, "status": "unset",
                    "detail": f"{type(exc).__name__}: {exc}"}

        if not expected_name:
            status, detail = "unverified", endpoint.tenant_public_id
        elif page.tenant_name == expected_name:
            status, detail = "ok", endpoint.tenant_public_id
        else:
            status = "mismatch"
            detail = f"expected {expected_name!r} — {endpoint.tenant_public_id}"
        return {"role": role, "tenant_name": page.tenant_name,
                "status": status, "detail": detail}

    def _available_pairs():
        if not sz_store.is_configured():
            return []
        try:
            return [{"id": p["id"], "name": p["name"], "is_default": p["is_default"]}
                    for p in sz_store.list_tenant_pairs()]
        except Exception:
            app.logger.exception("could not list tenant pairs")
            return []

    @app.get("/forven")
    def forven_page():
        return render_template("forven.html")

    @app.get("/forven/interviews")
    def forven_interviews():
        payload = {"pairs": _available_pairs()}

        try:
            pair, source, destination = _resolve_pair(request.args.get("pair_id"))
        except forven_config.ConfigError as exc:
            payload["error"] = str(exc)
            return jsonify(payload), 400

        payload["pair_id"] = pair["id"] if pair else None
        payload["pair_name"] = pair["name"] if pair else "environment defaults"
        payload["source_env"] = pair["source_env"] if pair else forven_config.source_env()
        payload["connections"] = [
            _connection_status(
                "source — interviews from", source,
                pair["source_tenant_name"] if pair else forven_config.source_expected_name(),
            ),
            _connection_status(
                "destination — reels to", destination,
                pair["dest_tenant_name"] if pair else forven_config.destination_expected_name(),
            ),
        ]

        client = forven_api.ForvenClient(source.base_url, source.api_key)
        try:
            payload["interviews"] = list(client.iter_interviews(source.tenant_public_id))
        except forven_api.ForvenApiError as exc:
            payload["error"] = f"{type(exc).__name__}: {exc}"
            return jsonify(payload), 502

        return jsonify(payload)

    def _reuse_held_interviews(refs, session_key):
        """Copy interviews we already hold into this session.

        Returns (reused_refs, aligned_refs). The index says where we put things,
        but files get deleted, so the copy is what decides - a ref only counts
        as reused once both its video and its transcript are actually in place.

        Whether the copied transcript is aligned is read from the file, not from
        the index: the encoder overwrites the transcript in place, so the file
        is the truth and a stale index row cannot cause a plain transcript to be
        recorded as aligned.
        """
        if not sz_store.is_configured():
            return [], []
        try:
            held = sz_store.ingested_sessions(refs)
        except Exception:
            app.logger.exception("could not read the ingest index")
            return [], []

        reused, aligned = [], []
        for ref, row in held.items():
            source_prefix = row.get("session_key")
            if not source_prefix or source_prefix == session_key:
                continue
            try:
                keys = set(storage.list_keys(source_prefix))
                text_key = f"{source_prefix}/{ref}.txt"
                video_key = next(
                    (k for k in keys
                     if k.rpartition(".")[0] == f"{source_prefix}/{ref}"
                     and f".{k.rpartition('.')[2].lower()}" in _VIDEO_KEY_SUFFIXES),
                    None)
                if text_key not in keys or video_key is None:
                    continue
                suffix = video_key.rpartition(".")[2]
                storage.copy_key(text_key, f"{session_key}/{ref}.txt")
                storage.copy_key(video_key, f"{session_key}/{ref}.{suffix}")
            except Exception:
                # A copy that fails is not fatal: the ref simply is not reused
                # and gets fetched from Forven like any other.
                app.logger.exception("could not reuse %s", ref)
                continue

            reused.append(ref)
            if not _sessions_needing_encode([f"{session_key}/{ref}.txt",
                                             f"{session_key}/{ref}.{suffix}"]):
                aligned.append(ref)
        return reused, aligned

    @app.post("/forven/pull")
    def forven_pull():
        body = request.get_json(silent=True) or {}
        refs = [str(r).strip() for r in body.get("refs") or []]
        refs = [r for r in refs if r]
        if not refs:
            return jsonify({"error": "Select at least one interview."}), 400

        try:
            pair, source, _ = _resolve_pair(body.get("pair_id"))
        except forven_config.ConfigError as exc:
            return jsonify({"error": str(exc)}), 400

        session_key = storage.new_session_key()

        # An interview we have pulled before is copied across rather than
        # downloaded and aligned again. Alignment is the expensive step - the
        # whole video through faster-whisper on a job - and the recording does
        # not change, so paying for it once per reel was pure waste.
        # refresh=true forces a fresh fetch, for the case Forven re-transcribes
        # an interview and the copy we hold goes stale.
        reused, aligned_reuse = [], []
        if not body.get("refresh"):
            reused, aligned_reuse = _reuse_held_interviews(refs, session_key)

        to_fetch = [r for r in refs if r not in reused]
        if to_fetch:
            client = forven_api.ForvenClient(source.base_url, source.api_key)
            try:
                forven_ingest.ingest(client,
                                     tenant_public_id=source.tenant_public_id,
                                     refs=to_fetch, session_key=session_key)
            except forven_api.ForvenApiError as exc:
                return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 502

        # Interviews whose transcript is not ready are skipped, so report what
        # actually landed rather than what was asked for.
        landed = [Path(k).stem for k in storage.list_keys(session_key)
                  if k.endswith(".txt")]

        if landed and sz_store.is_configured():
            # Recorded in two groups: a copied-in aligned transcript keeps its
            # alignment, a freshly fetched one is plain and must lose it.
            groups = [([r for r in landed if r in aligned_reuse], True),
                      ([r for r in landed if r not in aligned_reuse], False)]
            for group, preserve in groups:
                if not group:
                    continue
                try:
                    sz_store.record_ingested(
                        group, tenant_public_id=source.tenant_public_id,
                        session_key=session_key, preserve_aligned=preserve)
                except Exception:
                    # The pull succeeded; failing to index it must not lose it.
                    app.logger.exception("could not record ingest index")

        return jsonify({"session_key": session_key,
                        "ingested": len(landed),
                        "requested": len(refs),
                        "reused": len(reused),
                        "pair_name": pair["name"] if pair else None})

    @app.post("/forven/pairs")
    def forven_add_pair():
        """Add or update an organisation pair.

        Without this the first pair could only be created with a psql prompt,
        which leaves a fresh deployment unusable by anyone but a developer.
        """
        if not sz_store.is_configured():
            return jsonify({
                "error": "No database configured, so organisations cannot be "
                         "saved. Set DATABASE_URL."
            }), 503

        body = request.get_json(silent=True) or {}
        fields = {k: (body.get(k) or "").strip()
                  for k in ("name", "source_tenant_id", "dest_tenant_id",
                            "source_tenant_name", "dest_tenant_name")}
        missing = [k for k in ("name", "source_tenant_id", "dest_tenant_id")
                   if not fields[k]]
        if missing:
            return jsonify({"error": "Required: " + ", ".join(missing)}), 400

        source_env = (body.get("source_env") or "production").strip()
        dest_env = (body.get("dest_env") or "staging").strip()
        if source_env not in ("production", "staging") or dest_env not in ("production", "staging"):
            return jsonify({"error": "Environments must be production or staging."}), 400

        try:
            pair = sz_store.upsert_tenant_pair(
                name=fields["name"],
                source_tenant_id=fields["source_tenant_id"],
                dest_tenant_id=fields["dest_tenant_id"],
                source_tenant_name=fields["source_tenant_name"] or None,
                dest_tenant_name=fields["dest_tenant_name"] or None,
                source_env=source_env,
                dest_env=dest_env,
                is_default=bool(body.get("is_default")),
            )
        except Exception as exc:
            app.logger.exception("could not save tenant pair")
            return jsonify({"error": f"Could not save: {exc}"}), 500

        return jsonify({"id": pair["id"], "name": pair["name"]})

    @app.delete("/forven/pairs/<int:pair_id>")
    def forven_delete_pair(pair_id):
        if not sz_store.is_configured():
            return jsonify({"error": "No database configured."}), 503
        try:
            removed = sz_store.delete_tenant_pair(pair_id)
        except Exception as exc:
            app.logger.exception("could not delete tenant pair")
            return jsonify({"error": f"Could not delete: {exc}"}), 500
        return jsonify({"deleted": removed})

    @app.get("/forven/reels")
    def forven_reels():
        """Reels in the library, with whether each can be delivered to Forven.

        A reel is deliverable when it has an object key AND records which
        interviews it was cut from. Entries made before the generator recorded
        source_videos cannot be delivered without someone confirming the
        sources, because register demands exactly the interviews in the cut.
        """
        try:
            library = storage.load_library()
        except Exception as exc:
            return jsonify({"error": f"Could not read the library: {exc}"}), 502

        reels = []
        for entry in library:
            sources = entry.get("source_videos") or []
            refs = sorted({Path(name).stem for name in sources})
            reels.append({
                "id": entry.get("id"),
                "filename": entry.get("filename"),
                "prompt": entry.get("prompt"),
                "duration_seconds": entry.get("duration_seconds"),
                "clip_count": entry.get("clip_count"),
                "created_at": entry.get("created_at"),
                "reel_s3_key": entry.get("reel_s3_key"),
                "source_refs": refs,
                "deliverable": bool(entry.get("reel_s3_key") and refs),
            })
        reels.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        return jsonify({"reels": reels})

    @app.post("/forven/deliver")
    def forven_deliver_reel():
        """Upload a finished reel to Forven and register it.

        Runs upload-start, PUT, register - and records the outcome so the reel
        can be traced back to what Forven called it.
        """
        body = request.get_json(silent=True) or {}
        reel_id = (body.get("reel_id") or "").strip()
        title = (body.get("title") or "").strip()
        if not reel_id:
            return jsonify({"error": "reel_id is required."}), 400

        try:
            pair, _, destination = _resolve_pair(body.get("pair_id"))
        except forven_config.ConfigError as exc:
            return jsonify({"error": str(exc)}), 400

        try:
            entry = next(e for e in storage.load_library() if e.get("id") == reel_id)
        except StopIteration:
            return jsonify({"error": "No such reel."}), 404
        except Exception as exc:
            return jsonify({"error": f"Could not read the library: {exc}"}), 502

        s3_key = entry.get("reel_s3_key")
        if not s3_key:
            return jsonify({"error": "This reel has no stored file to deliver."}), 422

        refs = sorted({Path(n).stem for n in (entry.get("source_videos") or [])})
        if not refs:
            return jsonify({
                "error": "This reel does not record which interviews it came from, "
                         "so it cannot be delivered. Regenerate it."
            }), 422

        # The reel lives in our object storage; Forven wants the bytes.
        local = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        local.close()
        try:
            storage.download_file(s3_key, local.name)
            client = forven_api.ForvenClient(destination.base_url, destination.api_key)
            result = forven_deliver.deliver(
                client,
                tenant_public_id=destination.tenant_public_id,
                reel_path=local.name,
                title=title or entry.get("prompt") or entry.get("filename") or "Sizzle reel",
                duration_seconds=int(entry.get("duration_seconds") or 0) or 1,
                source_interview_refs=refs,
                metadata={"library_id": reel_id, "clips": entry.get("clip_count")},
            )
        except forven_api.ForvenApiError as exc:
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 502
        except Exception as exc:
            app.logger.exception("reel delivery failed reel_id=%s", reel_id)
            return jsonify({"error": f"Delivery failed: {exc}"}), 500
        finally:
            try:
                os.remove(local.name)
            except OSError:
                pass

        if sz_store.is_configured():
            try:
                stored = sz_store.create_reel(
                    title=title or entry.get("prompt") or "Sizzle reel",
                    prompt=entry.get("prompt") or "",
                    session_key=entry.get("source_folder") or "",
                    tenant_pair_id=pair["id"] if pair else None,
                    clips=[{"interview_ref": ref, "start_seconds": 0.0,
                            "end_seconds": 0.0} for ref in refs],
                )
                sz_store.set_media(stored["id"], media_key=s3_key,
                                   duration_seconds=entry.get("duration_seconds"))
                sz_store.mark_delivered(stored["id"],
                                        reel_ref=result.get("reel_ref"),
                                        reel_public_id=result.get("reel_public_id"))
            except Exception:
                # Delivered is delivered; failing to record it must not report
                # a failure the customer would retry.
                app.logger.exception("could not record delivery reel_id=%s", reel_id)

        return jsonify({
            "reel_ref": result.get("reel_ref"),
            "reel_public_id": result.get("reel_public_id"),
            "source_refs": refs,
            "tenant_name": pair["dest_tenant_name"] if pair else None,
        })

    _VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
    _ALLOWED_UPLOAD_EXTENSIONS = _VIDEO_EXTENSIONS | {".txt"}

    @app.post("/upload")
    @limiter.limit("30 per minute")
    def upload():
        """Cloud-mode endpoint: receive uploaded video and transcript files as a session.

        Accepts video files (.mp4, .mov, .avi, .mkv, .webm) and pre-made transcript
        files (.txt). When a .txt file is uploaded alongside a video, transcription is
        skipped for that video — the app uses the supplied transcript directly.
        At least one video file must be included.
        """
        files = request.files.getlist("files")
        if not files or all(f.filename == "" for f in files):
            return jsonify({"error": "No files provided"}), 400

        # Validate all files before writing any
        has_video = False
        for f in files:
            ext = Path(f.filename).suffix.lower()
            if ext not in _ALLOWED_UPLOAD_EXTENSIONS:
                return jsonify({"error": f"Unsupported file type: {f.filename}. Upload videos (.mp4 .mov .avi .mkv .webm) and/or transcripts (.txt)."}), 400
            if ext in _VIDEO_EXTENSIONS:
                has_video = True
        if not has_video:
            return jsonify({"error": "At least one video file is required."}), 400

        session_key = storage.new_session_key()

        # Determine local session directory
        if storage.is_cloud():
            session_dir = Path(tempfile.mkdtemp(prefix="sizzle_"))
        else:
            session_dir = storage._data_root() / session_key
            session_dir.mkdir(parents=True, exist_ok=True)

        saved_names = []
        for f in files:
            filename = Path(f.filename).name  # strip any path components
            dest = session_dir / filename
            f.save(str(dest))
            if storage.is_cloud():
                storage.upload_file(str(dest), f"{session_key}/{filename}")
            saved_names.append(filename)

        # In cloud mode, files are now in S3; clean up the local temp dir
        if storage.is_cloud():
            shutil.rmtree(str(session_dir), ignore_errors=True)
            # session_dir is gone; return S3 key as folder indicator
            folder_indicator = session_key
        else:
            folder_indicator = str(session_dir)

        return jsonify({
            "session_key": session_key,
            "folder": folder_indicator,
            "files": saved_names,
        })

    @app.post("/upload/prepare")
    @limiter.limit("30 per minute")
    def upload_prepare():
        """Cloud-mode: validate filenames and create an upload session.

        The browser calls this first to get a session_key plus one presigned PUT
        URL per file, then uploads each file DIRECTLY to R2 (browser → R2, the
        host never sees the bytes), then calls /upload/commit.

        Uploading straight to R2 avoids routing large video bytes through this
        host — the old /upload/file proxy hit the host's request body-size limit
        (surfacing as "unexpected end of JSON input" in the browser) and doubled
        the host's metered bandwidth per file.

        Request JSON: {"files": ["video1.mp4", "transcript1.txt", ...]}
        Response JSON: {"session_key": "sessions/<uuid>", "folder": "sessions/<uuid>",
                        "uploads": {"video1.mp4": "<presigned PUT url>", ...}}
        """
        if not storage.is_cloud():
            return jsonify({"error": "This endpoint is only available in cloud mode"}), 400

        body = request.get_json(silent=True) or {}
        filenames = body.get("files", [])
        if not filenames:
            return jsonify({"error": "No files provided"}), 400

        has_video = False
        for name in filenames:
            ext = Path(name).suffix.lower()
            if ext not in _ALLOWED_UPLOAD_EXTENSIONS:
                return jsonify({"error": f"Unsupported file type: {name}. Upload videos (.mp4 .mov .avi .mkv .webm) and/or transcripts (.txt)."}), 400
            if ext in _VIDEO_EXTENSIONS:
                has_video = True
        if not has_video:
            return jsonify({"error": "At least one video file is required."}), 400

        session_key = storage.new_session_key()
        # 24h, not 2h. Every URL is minted here, up front, but the browser uploads
        # them one at a time — so the LAST file's URL has to outlive the whole
        # transfer. The reference folder is 3.3 GB across 8 interviews (largest
        # single file 1.4 GB); a 35-interview study on a 20 Mbps line runs past
        # two hours and the tail URLs start 403ing mid-upload, stranding a
        # half-populated session. Expiry is the wrong thing to be tight about
        # here: the key is already unguessable and scoped to one object.
        uploads = {
            name: storage.presigned_put_url(f"{session_key}/{Path(name).name}", expires=86400)
            for name in filenames
        }
        return jsonify({
            "session_key": session_key,
            "folder": session_key,
            "uploads": uploads,
        })

    @app.post("/encode-session")
    @limiter.limit("6 per minute")
    def encode_session():
        """Launch a Render one-off job that turns this session's plain Forven
        transcripts into rich ones (design doc D5/D6).

        Returns {"job_id": ..., "status": ...}, or {"skipped": true} when there is
        nothing to do — which makes a repeat call free rather than expensive.

        ⚠️ This app has no authentication, so §10's "platform-admin only" control
        does not exist. What guards it: a rate limit, a fixed command template, a
        validated session key, the requirement that the session exist and
        actually need work, and a concurrency cap inside render_jobs.
        """
        if not storage.is_cloud():
            return jsonify({"error": "This endpoint is only available in cloud mode"}), 400
        if not render_jobs.is_configured():
            return jsonify({"error": "Encoder jobs are not configured on this deployment"}), 503

        session_key = (request.get_json(silent=True) or {}).get("session_key", "")
        if not render_jobs.SESSION_KEY_RE.match(session_key):
            return jsonify({"error": "A valid session_key is required"}), 400

        # Only dispatch when the session really contains work. This is what stops
        # an unauthenticated caller manufacturing cost against arbitrary keys, and
        # it makes re-dispatch a no-op rather than a duplicate encode.
        try:
            keys = storage.list_keys(session_key)
        except Exception as exc:
            return jsonify({"error": f"Could not read the session: {exc}"}), 502
        if not keys:
            return jsonify({"error": "No such session"}), 404

        # A session with no video/transcript pairs is NOT "already encoded" — it
        # has nothing to encode from. Reporting both as a success made a real
        # misconfiguration look like a tick.
        if not _session_video_pairs(keys):
            return jsonify({"error": "No video/transcript pairs in this session — "
                                     "upload a .txt transcript alongside each video"}), 422

        pending = _sessions_needing_encode(keys)
        if not pending:
            return jsonify({"skipped": True, "reason": "all transcripts already rich"})

        def _record(payload):
            # Recorded so Render's job list can be reconciled against jobs this
            # app launched (§10 detection) — and so a FAILED dispatch leaves a
            # trace, since the browser's error message is gone in seconds.
            try:
                storage.write_json(f"{session_key}/encode_job.json", {
                    **payload,
                    "interviews": pending,
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                })
            except Exception:
                pass  # never fail the request over the audit note

        try:
            job = render_jobs.create_encode_job(session_key)
        except render_jobs.RenderError as exc:
            _record({"error": str(exc),
                     "plan_id": render_jobs.configured_plan_id(),
                     "service_id": os.environ.get("RENDER_ENCODER_SERVICE_ID")})
            return jsonify({"error": str(exc)}), 503

        _record({
            "job_id": job["job_id"],
            "start_command": job["start_command"],
            "plan_id": job["plan_id"],
        })
        return jsonify({"job_id": job["job_id"], "status": job["status"],
                        "interviews": len(pending)})

    def _record_alignment(session_key):
        """Mark the interviews this session actually got aligned.

        Truth comes from the artifact, not from the job exiting zero. The
        encoder writes <REF>.forven.txt (the preserved original) only for
        interviews it really encoded, and skips any whose sentences would not
        anchor - those keep turn-level timings and are NOT aligned. Marking the
        whole session on a green job would claim otherwise, and the claim would
        be wrong for exactly the interviews that cut badly.
        """
        if not sz_store.is_configured():
            return
        try:
            refs = [key.rsplit("/", 1)[-1][: -len(".forven.txt")]
                    for key in storage.list_keys(session_key)
                    if key.endswith(".forven.txt")]
            if refs:
                sz_store.mark_aligned(refs)
        except Exception:
            app.logger.exception("could not record alignment for %s", session_key)

    @app.get("/encode-status/<job_id>")
    @limiter.limit("120 per minute")
    def encode_status(job_id):
        """Poll an encoder job. The frontend blocks on this before opening the
        folder, so a plain→rich swap can never land under an active session."""
        if not render_jobs.is_configured():
            return jsonify({"error": "Encoder jobs are not configured"}), 503
        try:
            job = render_jobs.get_job(job_id)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except render_jobs.RenderError as exc:
            return jsonify({"error": str(exc)}), 502

        # The encoder is deliberately standalone - it holds no database
        # credentials and its only contract is the transcript file format - so
        # the app records the outcome on its behalf, from storage.
        session_key = request.args.get("session_key", "")
        if job.get("status") == "succeeded" and render_jobs.SESSION_KEY_RE.match(session_key):
            _record_alignment(session_key)
        return jsonify(job)

    @app.post("/upload/commit")
    def upload_commit():
        """Cloud-mode: acknowledge that the browser finished uploading to R2.

        Called after all presigned PUT uploads complete. Server just validates
        the request and echoes back the session info — no file I/O needed here
        since files are already in R2.

        Request JSON: {"session_key": "sessions/<uuid>", "files": ["video1.mp4", ...]}
        Response JSON: {"session_key": "sessions/<uuid>", "folder": "sessions/<uuid>", "files": [...]}
        """
        if not storage.is_cloud():
            return jsonify({"error": "This endpoint is only available in cloud mode"}), 400

        body = request.get_json(silent=True) or {}
        session_key = body.get("session_key")
        if not session_key:
            return jsonify({"error": "session_key is required"}), 400

        files = body.get("files", [])
        return jsonify({
            "session_key": session_key,
            "folder": session_key,
            "files": files,
        })

    @app.post("/browse")
    def browse():
        path = _pick_directory()
        if path is None:
            return jsonify({"path": None})
        return jsonify({"path": path})

    @app.get("/recent-folders")
    def recent_folders():
        return jsonify(_load_recent_folders())

    @app.post("/load-folder")
    def load_folder():
        folder = (request.get_json() or {}).get("folder", "").strip()
        if not _valid_session_folder(folder):
            return jsonify({"error": "forbidden"}), 403
        if storage.is_cloud() and folder and not Path(folder).exists():
            session_key = folder
            with _cloud_session_lock:
                ready = _cloud_session_ready.get(session_key)
                cached = (ready is not None and ready.is_set()
                          and session_key in _cloud_session_dirs)
            if not cached:
                # Download runs as a cancellable background job; the frontend
                # polls /status/<job_id> and cancels via DELETE /jobs/<job_id>.
                job_id = _new_job("session_download", 0)

                def _download():
                    cancel_event = _jobs[job_id]["cancel"]
                    try:
                        local_dir = _ensure_cloud_session(
                            session_key, job_id=job_id, cancel_event=cancel_event)
                    except SessionDownloadCancelled:
                        with _jobs_lock:
                            if job_id in _jobs and _jobs[job_id]["status"] == "running":
                                _jobs[job_id]["status"] = "cancelled"
                        return
                    except Exception as exc:
                        with _jobs_lock:
                            if job_id in _jobs:
                                _jobs[job_id]["status"] = "error"
                                _jobs[job_id]["error"] = str(exc)
                        return
                    result, error = _scan_load_folder(local_dir)
                    with _jobs_lock:
                        if job_id not in _jobs:
                            return
                        if error:
                            _jobs[job_id]["status"] = "error"
                            _jobs[job_id]["error"] = error
                        else:
                            _jobs[job_id]["status"] = "done"
                            _jobs[job_id]["result"] = {
                                "folder": result["folder"],
                                "files": result["files"],
                            }

                threading.Thread(target=_download, daemon=True).start()
                return jsonify({"job_id": job_id, "job_type": "session_download"})
            folder = _ensure_cloud_session(session_key)
        if not folder or not Path(folder).exists():
            return jsonify({"error": "Folder not found"}), 404

        result, error = _scan_load_folder(folder)
        if error:
            return jsonify({"error": error}), 422

        filenames = result["files"]
        needs_transcription = result["needs_transcription"]

        if not needs_transcription:
            return jsonify({"job_id": None, "files": filenames, "folder": folder})

        job_id = _new_job("transcription", len(needs_transcription))

        def _transcribe():
            cancel_event = _jobs[job_id]["cancel"]
            cpu_count = os.cpu_count() or 1
            workers, cpu_threads = _compute_transcription_parallelism(
                cpu_count, len(needs_transcription)
            )
            model = _get_whisper_model(cpu_threads, workers)
            _append_log(
                job_id,
                f"⟳ transcribing {len(needs_transcription)} video(s) "
                f"({workers} at a time)...",
            )

            def _do_one(vp):
                transcript = transcribe_video(str(vp), model=model)
                # A cancel may have fired while this video was transcribing; skip
                # the write/upload so no transcript appears after status=cancelled.
                if cancel_event.is_set():
                    return
                vp.with_suffix(".txt").write_text(transcript, encoding="utf-8")
                if storage.is_cloud():
                    for sk, td in _cloud_session_dirs.items():
                        if td == str(vp.parent):
                            storage.upload_file(
                                str(vp.with_suffix(".txt")),
                                f"{sk}/{vp.stem}.txt",
                            )
                            break

            executor = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
            futures = {executor.submit(_do_one, vp): vp for vp in needs_transcription}
            pending = set(futures)
            done_count = 0
            try:
                while pending:
                    if cancel_event.is_set():
                        executor.shutdown(wait=False, cancel_futures=True)
                        with _jobs_lock:
                            if job_id in _jobs:
                                _jobs[job_id]["status"] = "cancelled"
                        _append_log(job_id, "✗ transcription cancelled")
                        return
                    just_done, pending = concurrent.futures.wait(
                        pending,
                        timeout=0.5,
                        return_when=concurrent.futures.FIRST_COMPLETED,
                    )
                    for future in just_done:
                        vp = futures[future]
                        try:
                            future.result()
                            _append_log(job_id, f"✓ {vp.name} — done")
                        except Exception as exc:
                            _append_log(job_id, f"✗ {vp.name} — failed: {exc}")
                            with _jobs_lock:
                                if job_id in _jobs:
                                    _jobs[job_id]["error"] = f"{vp.name}: {exc}"
                        done_count += 1
                        with _jobs_lock:
                            if job_id not in _jobs:
                                return
                            _jobs[job_id]["done"] = done_count
            finally:
                executor.shutdown(wait=False)
            with _jobs_lock:
                if job_id not in _jobs:
                    return
                _jobs[job_id]["status"] = "done"
                _jobs[job_id]["result"] = {"folder": folder, "files": filenames}

        threading.Thread(target=_transcribe, daemon=True).start()
        return jsonify({"job_id": job_id, "files": filenames, "folder": folder})

    @app.get("/status/<job_id>")
    def job_status(job_id):
        with _jobs_lock:
            job = _jobs.get(job_id)
        if job is None:
            return jsonify({"error": "not found"}), 404
        return jsonify({
            "type": job["type"],
            "status": job["status"],
            "total": job["total"],
            "done": job["done"],
            "log": list(job["log"]),
            "result": job["result"],
            "error": job["error"],
        })

    @app.delete("/jobs/<job_id>")
    def cancel_job(job_id):
        with _jobs_lock:
            job = _jobs.get(job_id)
            if job:
                job["cancel"].set()
                if job["status"] == "running":
                    job["status"] = "cancelled"
        return jsonify({"ok": True})

    @app.get("/transcripts")
    def get_transcripts():
        folder = request.args.get("folder", "").strip()
        if not _valid_session_folder(folder):
            return jsonify({"error": "forbidden"}), 403
        if storage.is_cloud() and folder and not Path(folder).exists():
            folder = _ensure_cloud_session(folder)
        if not folder or not Path(folder).exists():
            return jsonify({"error": "Folder not found"}), 404
        try:
            video_paths = scan_videos(folder)
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        video_paths = _filter_generated_reels(video_paths)
        sidecar = Path(folder) / "sizzle_generated_reels.txt"
        if sidecar.exists():
            try:
                locally_generated = set(sidecar.read_text(encoding="utf-8").splitlines())
                video_paths = [p for p in video_paths if p.name not in locally_generated]
            except Exception:
                pass
        files = []
        for vp in video_paths:
            txt_path = vp.with_suffix(".txt")
            if not txt_path.exists():
                lines = []
            else:
                lines = _parse_transcript_lines(_read_transcript(txt_path))
            files.append({"name": vp.name, "lines": lines})
        return jsonify({"files": files})

    @app.post("/analyze")
    @limiter.limit("10 per minute;100 per hour")
    def analyze():
        body = request.get_json() or {}
        folder = body.get("folder", "").strip()
        prompt = body.get("prompt", "").strip()
        if not prompt:
            return jsonify({"error": "prompt is required"}), 400
        if not _valid_session_folder(folder):
            return jsonify({"error": "forbidden"}), 403
        if storage.is_cloud() and folder and not Path(folder).exists():
            folder = _ensure_cloud_session(folder)
        if not folder or not Path(folder).exists():
            return jsonify({"error": "Folder not found"}), 404
        result = _run_analyze(folder, prompt)
        if "error" in result:
            return jsonify(result), 500
        return jsonify(result)

    @app.get("/prompt-history")
    def get_prompt_history():
        with _prompt_history_lock:
            return jsonify(_load_prompt_history())

    @app.post("/prompt-history")
    def post_prompt_history():
        body = request.get_json() or {}
        action = body.get("action", "")
        text = body.get("text", "").strip()
        name = body.get("name", "").strip()
        if action == "use":
            if text:
                _prompt_history_use(text)
        elif action == "save_template":
            if name and text:
                with _prompt_history_lock:
                    data = _load_prompt_history()
                    templates = data.get("templates", [])
                    templates = [t for t in templates if t["name"] != name]
                    templates.append({"name": name, "text": text})
                    data["templates"] = templates
                    _save_prompt_history(data)
        elif action == "delete_template":
            if name:
                with _prompt_history_lock:
                    data = _load_prompt_history()
                    data["templates"] = [t for t in data.get("templates", []) if t["name"] != name]
                    _save_prompt_history(data)
        else:
            return jsonify({"error": "unknown action"}), 400
        return jsonify({"ok": True})

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=True, port=5000)
