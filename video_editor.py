import os
import shutil
import subprocess
import tempfile
from pathlib import Path

# Audio fades are deliberately far shorter than the video dip they accompany.
# The video fade IS the transition between clips; audio only needs enough of a
# ramp to avoid a click at the splice.
#
# They used to share one duration, which clipped speech at both ends. Clips
# start on the speaker's first word (measured head slack is 0.00s on every clip
# of every reel) and, in rich tier, end on their last -- encoder.core.emit
# rounds an end up specifically to protect the final word, but then clamps it
# to the next line's start, so a consecutive sentence removes that slack again.
# A 0.4s ramp over either boundary attenuates real speech and the word audibly
# drops.
AUDIO_FADE_SECONDS = 0.1


def _title_alpha_expr(duration: float) -> str:
    """drawtext `alpha` expression for a traditional title: fade in 0.3s, hold,
    fade out 0.3s. The title shows for min(3s, clip) so it appears then leaves.

    Commas are backslash-escaped: ffmpeg treats a raw comma inside a filter
    option value as a filter separator (verified: raw commas crash, `\\,` works
    on ffmpeg 8.x / Windows)."""
    show = min(3.0, max(0.6, duration))
    fade = 0.3
    out_start = max(fade, show - fade)
    return (
        f"if(lt(t\\,{fade})\\,t/{fade}\\,"
        f"if(lt(t\\,{out_start:.3f})\\,1\\,max(0\\,({show:.3f}-t)/{fade})))"
    )


def _fmt_mmss(seconds: int) -> str:
    """Whole seconds → 'M:SS'."""
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def _assert_has_video(output_path: str) -> None:
    """Raise if the encoded clip came out with no video stream.

    A source whose video track is shorter than its audio track (a truncated or
    badly muxed recording) yields an audio-only clip when the requested range
    starts past the video's end — ffmpeg succeeds, because the audio really is
    there. stitch_clips then concatenates that 1-stream clip with normal
    2-stream clips using `-c copy`, which cannot reconcile the mismatch: the
    reel's video timeline is stamped over the audio's duration and the whole
    thing plays in slow motion. Observed on a 628s container whose video stream
    ended at 357s — a 126s reel encoded as 394s at 10.4fps.

    Raising here routes into the caller's existing per-clip failure handling,
    which drops the clip and keeps the rest of the reel intact.
    """
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_type", "-of", "default=nw=1:nk=1",
             output_path],
            capture_output=True, text=True,
        )
    except Exception:
        return  # ffprobe unavailable — never fail a clip we cannot check
    stdout = getattr(probe, "stdout", None)
    if not isinstance(stdout, str):
        return  # no real capture to inspect; fail open
    # ffprobe prints nothing (exit 0) when the file has no v:0 stream, so an
    # empty stdout IS the audio-only signal, not an error.
    if "video" not in stdout:
        raise RuntimeError(
            f"{Path(output_path).name}: encoded clip has no video stream — the "
            f"source's video track likely ends before the requested range"
        )


def _timer_drawtext_filters(duration, out_dir, prefix, fontfile_arg, fontsize):
    """Per-second countdown of the clip's remaining time, pinned top-right.

    ffmpeg can't run a dynamic time expression here — this build treats ':' in a
    filter value as an option separator even when escaped, so `%{eif:…}` and any
    'M:SS' literal break the filter. Instead each whole second is a static
    drawtext gated to its 1-second window with `enable=between(t\\,a\\,b)`
    (commas escaped — verified working), and the 'M:SS' text lives in a side-car
    file so its colon never reaches the filter string.

    Returns the list of drawtext filter strings (side-car files already written).
    """
    n = max(1, round(duration))
    margin = max(12, int(fontsize * 0.6))
    filters = []
    for k in range(n):
        remaining = n - k                       # counts n, n-1, …, 1
        tf = out_dir / f"{prefix}_timer{k}.txt"
        tf.write_text(_fmt_mmss(remaining), encoding="utf-8")
        lo = k
        # Last window runs to the end so a fractional tail still shows a value.
        hi = (k + 1) if k < n - 1 else max(duration, n) + 1
        filters.append(
            f"drawtext={fontfile_arg}textfile={tf.name}"
            f":fontcolor=white:fontsize={fontsize}"
            f":box=1:boxcolor=black@0.5:boxborderw=8"
            f":x=w-text_w-{margin}:y={margin}"
            f":enable=between(t\\,{lo}\\,{hi})"
        )
    return filters


def check_ffmpeg() -> None:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        raise RuntimeError(
            "ffmpeg not found. Install it with:\n"
            "  Windows: winget install ffmpeg\n"
            "  Mac: brew install ffmpeg"
        )


def parse_timestamp_to_seconds(ts: str) -> float:
    parts = ts.split(":")
    return float(int(parts[0]) * 60 + int(parts[1]))


def extract_clip(video_path: str, start_sec: float, end_sec: float, output_path: str,
                 fade_out_secs: float = 0.0, title_lines: list | None = None,
                 font_path: str | None = None, height: int | None = None,
                 fade_in_secs: float = 0.0, show_timer: bool = False,
                 width: int | None = None) -> None:
    # Re-encode (never stream-copy) so every clip starts on an I-frame.
    # -ss before -i: fast input seek. -t duration (not -to) is relative to the
    # seek point. -avoid_negative_ts make_zero zeroes each clip's timestamps so
    # the concat demuxer sees clean zero-based PTS on every clip — prevents AV drift.
    duration = end_sec - start_sec
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start_sec),
        "-i", video_path,
        "-t", str(duration),
        "-avoid_negative_ts", "make_zero",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-r", "30",       # normalise to 30 fps — a single consistent video
        "-c:a", "aac",    # timebase so the concat demuxer sees uniform clips
        "-ar", "48000",
        "-ac", "2",
    ]

    vf = []
    af = []
    run_cwd = None

    # Text overlays (identification title + countdown timer) both need a relative
    # font and side-car text files resolved from the output dir. textfile= and a
    # relative fontfile= keep every path out of the filter string, so the ffmpeg
    # 8.x/Windows drive-letter-colon quirk never bites. Requires cwd=out_dir.
    if title_lines or show_timer:
        out_dir = Path(output_path).parent
        run_cwd = str(out_dir)
        prefix = Path(output_path).stem
        h = height or 1080
        fontsize = max(20, h // 22)

        fontfile_arg = ""
        if font_path and Path(font_path).exists():
            font_dest = out_dir / Path(font_path).name
            if not font_dest.exists():
                shutil.copy(font_path, font_dest)
            fontfile_arg = f"fontfile={Path(font_path).name}:"

        # ── Identification overlay: title_lines top-anchored, fading in/out
        #    like a traditional title (0-second title-card cost). ──
        if title_lines:
            line_height = int(fontsize * 1.35)
            top = max(fontsize, h // 14)
            alpha = _title_alpha_expr(duration)
            # drawtext has no auto-fit: a line wider than the frame is silently
            # cropped at both edges, so a long filename renders as an unreadable
            # middle slice. Shrink only the lines that would overrun (Forven
            # export stems are ~53 chars); short lines keep the base size.
            max_w = (width or int(h * 9 / 16)) * 0.92
            for i, line in enumerate(title_lines):
                tf = out_dir / f"{prefix}_t{i}.txt"
                # drawtext expands % format specifiers even from a textfile.
                tf.write_text(line.replace("%", "%%"), encoding="utf-8")
                y = top + i * line_height
                # 0.55 * fontsize ≈ average glyph width for the sans faces used here.
                line_size = min(fontsize,
                                max(14, int(max_w / (0.55 * max(len(line), 1)))))
                vf.append(
                    f"drawtext={fontfile_arg}textfile={tf.name}"
                    f":fontcolor=white:fontsize={line_size}"
                    f":shadowcolor=black@0.8:shadowx=2:shadowy=2"
                    f":x=w/2-text_w/2:y={y}:alpha={alpha}"
                )

        # ── Countdown timer: remaining clip time, top-right. ──
        if show_timer:
            vf.extend(_timer_drawtext_filters(duration, out_dir, prefix, fontfile_arg, fontsize))

    # Fades come after the overlays so a boundary dip also dims the text.
    # Video and audio fade durations are independent — see AUDIO_FADE_SECONDS.
    if fade_in_secs > 0.0:
        vf.append(f"fade=t=in:st=0:d={fade_in_secs}")
        a_in = min(AUDIO_FADE_SECONDS, fade_in_secs)
        af.append(f"afade=t=in:st=0:d={a_in}")
    if fade_out_secs > 0.0:
        fade_start = max(0.0, duration - fade_out_secs)
        vf.append(f"fade=t=out:st={fade_start}:d={fade_out_secs}")
        a_out = min(AUDIO_FADE_SECONDS, fade_out_secs)
        af.append(f"afade=t=out:st={max(0.0, duration - a_out)}:d={a_out}")

    if vf:
        cmd += ["-vf", ",".join(vf)]
    if af:
        cmd += ["-af", ",".join(af)]
    cmd.append(output_path)
    subprocess.run(
        cmd,
        check=True,
        capture_output=True,
        cwd=run_cwd,
    )
    _assert_has_video(output_path)


def stitch_clips(clip_paths: list[str], output_path: str) -> None:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        concat_list_path = f.name
        for path in clip_paths:
            f.write(f"file '{Path(path).as_posix()}'\n")
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", concat_list_path,
                "-c", "copy",
                output_path,
            ],
            capture_output=True,
        )
        if result.returncode != 0:
            print(result.stderr.decode(errors="replace"), file=__import__("sys").stderr)
            result.check_returncode()
    finally:
        os.unlink(concat_list_path)


def stitch_clips_to_pipe(clip_paths: list[str]) -> subprocess.Popen:
    """Like stitch_clips but streams fragmented MP4 to stdout instead of writing a file.

    Returns a Popen object. Caller must:
    - Read proc.stdout (to consume the stream and avoid pipe buffer deadlock)
    - Drain proc.stderr in a separate thread (to prevent ffmpeg blocking on a full pipe)
    - Call proc.wait() after stdout is exhausted
    - Delete proc._concat_list_path (the temp concat list file) after proc.wait()
    """
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
    concat_list_path = f.name
    for path in clip_paths:
        f.write(f"file '{Path(path).as_posix()}'\n")
    f.close()

    try:
        proc = subprocess.Popen(
            [
                "ffmpeg", "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", concat_list_path,
                "-c", "copy",
                "-movflags", "frag_keyframe+empty_moov",
                "-f", "mp4",
                "pipe:1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except Exception:
        os.unlink(concat_list_path)
        raise
    proc._concat_list_path = concat_list_path
    return proc
