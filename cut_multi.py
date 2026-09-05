"""Cut one reel from clips across SEVERAL interviews.

cut_local.py handles a single source. This crosses interviews, which is what
exercises the transition between clips - and what requires every clip to be
normalised to identical dimensions, because the concat demuxer will not join
streams that differ.

Uses the product's own video_editor.extract_clip with the same symmetric fade
generator_app applies (TRANSITION_FADE_SECONDS), rather than a plain hard-cut
concat, so the seams look like the real thing.

    python cut_multi.py <session_dir>:<REF> <session_dir>:<REF> [...] --clips 2

Prints timings only, never transcript content.
"""

import argparse
import re
import sys
from pathlib import Path

import forven_api
import forven_config
import forven_ingest
import video_editor

LINE = re.compile(r"^\[(?P<start>[\d:]+)-(?P<end>[\d:]+)\]\s*(?P<role>[^:]{1,24}):\s*(?P<text>.*)$")

# Matches generator_app.TRANSITION_FADE_SECONDS - the dip between clips.
FADE_SECONDS = 0.4
MIN_CLIP_SECONDS = 4.0

# Every clip is forced to these dimensions. Sources differ (one interview came
# back at 297 MB for 233s, another at 56 MB for 264s), and concat refuses
# mismatched streams.
TARGET_WIDTH = 1280
TARGET_HEIGHT = 720


def to_seconds(stamp: str) -> float:
    parts = [int(p) for p in stamp.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts[-3:]
    return float(h * 3600 + m * 60 + s)


def parse_aligned(path: Path) -> list:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = LINE.match(line.strip())
        if not match:
            continue
        rows.append(
            {
                "start": to_seconds(match.group("start")),
                "end": to_seconds(match.group("end")),
                "role": match.group("role").strip(),
            }
        )
    return rows


def ensure_media(client, tenant, session_dir: Path, ref: str) -> Path:
    """Download the interview media if it is not already present.

    Media is purged after each run per the retention obligation, so a repeat
    run re-fetches it. The aligned transcript is kept and reused.
    """
    video = session_dir / f"{ref}.mp4"
    if video.exists():
        print(f"  {ref}: media already present")
        return video
    link = client.media_link(tenant, ref, disposition="attachment")
    forven_ingest._download_to_storage(link["url"], f"{session_dir.name}/{ref}.mp4")
    print(f"  {ref}: media downloaded ({video.stat().st_size / 1_000_000:.1f} MB)")
    return video


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sources", nargs="+", help="<session_dir>:<REF>")
    parser.add_argument("--clips", type=int, default=2, help="clips per interview")
    parser.add_argument("--out", default="multi_reel.mp4")
    args = parser.parse_args(argv)

    cfg = forven_config.source_config()
    client = forven_api.ForvenClient(cfg.base_url, cfg.api_key)

    print("Fetching media:")
    plan = []
    for source in args.sources:
        session_name, ref = source.split(":", 1)
        session_dir = Path(__file__).parent / "sessions" / session_name
        aligned = session_dir / f"{ref}.txt"
        if not aligned.exists():
            print(f"  {ref}: no aligned transcript at {aligned} - align it first")
            return 1
        video = ensure_media(client, cfg.tenant_public_id, session_dir, ref)
        plan.append((ref, video, parse_aligned(aligned)))

    work = Path(__file__).parent / "sessions" / "_multicut"
    work.mkdir(parents=True, exist_ok=True)
    clip_paths = []
    total = 0.0

    print("\nCutting:")
    for ref, video, rows in plan:
        usable = [r for r in rows
                  if r["role"].lower().startswith("participant")
                  and (r["end"] - r["start"]) >= MIN_CLIP_SECONDS]
        chosen = sorted(usable, key=lambda r: r["end"] - r["start"], reverse=True)[:args.clips]
        chosen.sort(key=lambda r: r["start"])
        if not chosen:
            print(f"  {ref}: no usable answers")
            continue
        for index, row in enumerate(chosen):
            duration = row["end"] - row["start"]
            total += duration
            out = work / f"{ref}_{index}.mp4"
            print(f"  {ref} clip {index}: {row['start']:.0f}s-{row['end']:.0f}s ({duration:.0f}s)")
            video_editor.extract_clip(
                str(video), row["start"], row["end"], str(out),
                fade_in_secs=FADE_SECONDS, fade_out_secs=FADE_SECONDS,
                width=TARGET_WIDTH, height=TARGET_HEIGHT,
            )
            clip_paths.append(str(out))

    if len(clip_paths) < 2:
        print("\nneed at least two clips for a transition")
        return 1

    reel = Path(__file__).parent / args.out
    video_editor.stitch_clips(clip_paths, str(reel))

    refs = sorted({ref for ref, _, _ in plan})
    print(f"\nreel: {reel.name}  {reel.stat().st_size / 1_000_000:.1f} MB  ~{total:.0f}s")
    print(f"clips: {len(clip_paths)} across {len(refs)} interview(s)")
    print(f"source_interview_refs must be exactly: {refs}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
