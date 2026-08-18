"""Turn a recorded take into the handful of frames worth reasoning about.

A three hour shoot is millions of frames and almost all of them are redundant. Sending
every frame to Gemini would be slow and expensive and would not make the answers better,
because what changes between takes is scene state (where the mug is, which hand holds the
prop, whether the jacket is buttoned) and that only moves at scene-change speed.

So this picks frames two ways and merges them:
  1. Scene changes, which is where state actually changes.
  2. An even time sample, so a static take still gets covered.

The output is the input to the Gemini step. Nothing here needs a network or a GPU, which
is deliberate: it means the expensive half of the pipeline can be developed and tested
against real footage before any cloud credentials exist.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

# Frames closer together than this are treated as the same moment. Scene detection often
# fires two or three times across a single cut.
MIN_FRAME_GAP_S = 0.8

# Scene-change sensitivity for ffmpeg's `scene` filter. Lower catches more.
#
# Deliberately low. Measured on a clip with three hard cuts: at 0.25 only one of the three
# fired, at 0.05 all three did. The asymmetry decides it. A missed change is a missed piece
# of scene state, which is the whole product, while an extra frame costs a fraction of a
# cent of quota and gets capped by max_frames anyway. Recall beats precision here.
#
# Note that within a single take there are usually no cuts at all, so this is really
# detecting "the picture changed materially": an actor moved, a prop was set down, the
# lighting shifted. Those are exactly the moments worth a frame.
SCENE_THRESHOLD = 0.10


@dataclass
class Frame:
    """One sampled still from a take."""

    index: int
    timestamp_s: float
    path: str
    source: str  # "scene" or "sample"


@dataclass
class Take:
    """A single recorded take, reduced to the frames worth looking at."""

    take_id: str
    source_video: str
    duration_s: float
    width: int
    height: int
    frames: list[Frame]

    def to_json(self) -> str:
        payload = asdict(self)
        return json.dumps(payload, indent=2)


def _require_ffmpeg() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise RuntimeError(
                f"{tool} not found on PATH. It is needed to read and sample video.\n"
                "Install with: winget install Gyan.FFmpeg"
            )


def probe(video: Path) -> tuple[float, int, int]:
    """Duration in seconds, width, height."""
    out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-show_entries", "format=duration",
            "-of", "json",
            str(video),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(out.stdout)
    stream = data["streams"][0]
    duration = float(data["format"]["duration"])
    return duration, int(stream["width"]), int(stream["height"])


def scene_change_timestamps(video: Path, threshold: float = SCENE_THRESHOLD) -> list[float]:
    """Timestamps where the picture changed enough to be worth a new look.

    Uses ffmpeg's scene score rather than decoding frames in Python, which keeps this
    fast on long files and avoids pulling in a heavy video dependency.
    """
    proc = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostats",
            "-i", str(video),
            "-vf", f"select='gt(scene,{threshold})',showinfo",
            "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
    )
    # showinfo writes to stderr as `... pts_time:12.345 ...`
    stamps: list[float] = []
    for line in proc.stderr.splitlines():
        if "pts_time:" not in line:
            continue
        try:
            piece = line.split("pts_time:")[1].split()[0]
            stamps.append(float(piece))
        except (IndexError, ValueError):
            continue
    return sorted(stamps)


def plan_frames(duration_s: float, scene_stamps: list[float], max_frames: int) -> list[tuple[float, str]]:
    """Choose which timestamps to extract, merging scene changes with an even sample.

    Scene changes win when both land close together, because a cut is a more meaningful
    moment than an arbitrary clock tick.
    """
    # An even sample that always includes something near the start and end of the take.
    sample_count = max(2, min(max_frames, 6))
    step = duration_s / (sample_count + 1)
    sampled = [(step * (i + 1), "sample") for i in range(sample_count)]

    candidates = [(t, "scene") for t in scene_stamps] + sampled
    candidates.sort(key=lambda pair: pair[0])

    chosen: list[tuple[float, str]] = []
    for stamp, source in candidates:
        if stamp < 0 or stamp > duration_s:
            continue
        if chosen and abs(stamp - chosen[-1][0]) < MIN_FRAME_GAP_S:
            # Keep whichever is a scene change; a cut beats a clock tick.
            if source == "scene" and chosen[-1][1] == "sample":
                chosen[-1] = (stamp, source)
            continue
        chosen.append((stamp, source))

    if len(chosen) <= max_frames:
        return chosen

    # Over budget. Scene changes are worth more than clock ticks, so they get the space
    # first, but they must be thinned ACROSS the take rather than truncated. Taking the
    # first N would silently drop the entire back half of a busy take, which on a long
    # take is exactly where a continuity break tends to appear.
    scenes = [c for c in chosen if c[1] == "scene"]
    if len(scenes) > max_frames:
        step = len(scenes) / max_frames
        scenes = [scenes[int(i * step)] for i in range(max_frames)]

    remaining = max_frames - len(scenes)
    if remaining > 0:
        samples = [c for c in chosen if c[1] == "sample"]
        if len(samples) > remaining:
            step = len(samples) / remaining
            samples = [samples[int(i * step)] for i in range(remaining)]
        scenes += samples
    return sorted(scenes, key=lambda pair: pair[0])


def extract_frames(video: Path, plan: list[tuple[float, str]], out_dir: Path) -> list[Frame]:
    """Write one JPEG per planned timestamp.

    Each frame is a separate seek rather than one filtergraph pass. It is slower on paper
    but exact: the alternative gives frames whose real timestamps drift from the ones
    recorded in the manifest, and the manifest is what Gemini gets told.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    frames: list[Frame] = []

    for index, (stamp, source) in enumerate(plan):
        path = out_dir / f"frame_{index:03d}.jpg"
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-ss", f"{stamp:.3f}",
                "-i", str(video),
                "-frames:v", "1",
                "-q:v", "3",
                "-y", str(path),
            ],
            check=True,
        )
        if path.exists() and path.stat().st_size > 0:
            frames.append(
                Frame(index=index, timestamp_s=round(stamp, 3), path=str(path), source=source)
            )
    return frames


def ingest(
    video: Path,
    out_dir: Path,
    take_id: str | None = None,
    max_frames: int = 12,
    scene_threshold: float = SCENE_THRESHOLD,
) -> Take:
    _require_ffmpeg()
    if not video.exists():
        raise FileNotFoundError(f"No such video: {video}")

    duration, width, height = probe(video)
    scenes = scene_change_timestamps(video, scene_threshold)
    plan = plan_frames(duration, scenes, max_frames)
    frames = extract_frames(video, plan, out_dir)

    return Take(
        take_id=take_id or video.stem,
        source_video=str(video),
        duration_s=round(duration, 3),
        width=width,
        height=height,
        frames=frames,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path, help="recorded take (mp4 from the glasses)")
    parser.add_argument("--out", type=Path, default=Path("out"), help="where to write frames")
    parser.add_argument("--take-id", default=None)
    parser.add_argument("--max-frames", type=int, default=12)
    parser.add_argument(
        "--scene-threshold",
        type=float,
        default=SCENE_THRESHOLD,
        help=f"scene-change sensitivity, lower catches more (default {SCENE_THRESHOLD})",
    )
    args = parser.parse_args()

    take = ingest(
        args.video,
        args.out / (args.take_id or args.video.stem),
        args.take_id,
        args.max_frames,
        args.scene_threshold,
    )

    manifest = args.out / f"{take.take_id}.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(take.to_json(), encoding="utf-8")

    scene_n = sum(1 for f in take.frames if f.source == "scene")
    print(f"take {take.take_id}: {take.duration_s}s {take.width}x{take.height}")
    print(f"  {len(take.frames)} frames kept ({scene_n} scene changes, {len(take.frames) - scene_n} sampled)")
    print(f"  manifest {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
