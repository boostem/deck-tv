#!/usr/bin/env python3
"""Make the [SAMPLE] test videos: pastel-colored episodes and purple bumps.

    docker compose run --rm samples            # make whatever is missing
    docker compose run --rm samples --force    # remake all of them
    docker compose run --rm samples --clean    # delete every [SAMPLE] file

Each show gets its own color family and each episode its own shade of it, so
"never the same show twice in a row" can be checked by eye. Purple is reserved
for bumps: twelve of them, each a different soft purple.
"""

from __future__ import annotations

import argparse
import colorsys
import os
import random
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SHOWS_ROOT = Path("/media/shows")
BUMPS_ROOT = Path("/media/bumps")
TAG = "[SAMPLE]"  # every sample's filename carries this; --clean keys on it

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
INK = "0x2b2b2b"  # dark gray: readable on every pastel below
SIZE, FPS = "640x480", "30000/1001"

# Show -> (hue in degrees, saturation, lightness) of its color family. All
# soft, and none anywhere near purple (~250-320 degrees): that is for bumps.
SHOWS = {
    "Futurama": (130, 0.45, 0.74),                # mint green
    "The Boondocks": (52, 0.75, 0.74),            # butter yellow
    "Bleach": (28, 0.80, 0.76),                   # peach / orange
    "Family Guy": (208, 0.65, 0.76),              # sky blue
    "Aqua Teen Hunger Force": (172, 0.45, 0.66),  # teal
    "Cowboy Bebop": (4, 0.70, 0.70),              # coral red
}
EPISODES_PER_SHOW = 2
BUMP_COUNT = 12


def hex_color(hue: float, sat: float, light: float) -> str:
    r, g, b = colorsys.hls_to_rgb((hue % 360) / 360, light, sat)
    return "0x{:02x}{:02x}{:02x}".format(round(r * 255), round(g * 255), round(b * 255))


def plan() -> list[tuple[Path, str, float, list[tuple[str, int, str]]]]:
    """Every sample as (path, color, seconds, labels), in a fixed order.

    One seeded RNG drawn in a fixed order, so durations are the same on every
    run no matter which files already exist.
    """
    rng = random.Random(1999)
    jobs = []
    for show, (hue, sat, light) in SHOWS.items():
        title = show.upper()
        # Long names shrink to fit 640 px (DejaVu Bold caps run ~0.78 em).
        title_px = min(64, int(600 / (0.78 * len(title))))
        for n in range(1, EPISODES_PER_SHOW + 1):
            # Spread the shades around the family's center: +-5 deg, +-0.05 L.
            step = n - (EPISODES_PER_SHOW + 1) / 2
            color = hex_color(hue + step * 10, sat, light - step * 0.10)
            code = f"S01E{n:02d}"
            path = SHOWS_ROOT / show / "Season 01" / f"{show} - {code} - Sample Episode {TAG}.mp4"
            labels = [(title, title_px, "(h-text_h)/2-70"),
                      (code, 48, "(h-text_h)/2+10"),
                      ("SAMPLE", 22, "(h-text_h)/2+75")]
            jobs.append((path, color, round(rng.uniform(10, 15), 1), labels))
    for n in range(1, BUMP_COUNT + 1):
        # Lavender (262) through violet to orchid (306), alternating lightness
        # so neighbours differ in brightness as well as hue.
        hue = 262 + (n - 1) * 44 / (BUMP_COUNT - 1)
        color = hex_color(hue, 0.50, 0.80 if n % 2 else 0.70)
        path = BUMPS_ROOT / f"Sample Bump {n:02d} {TAG}.mp4"
        jobs.append((path, color, round(rng.uniform(3, 5), 1),
                     [(f"BUMP {n:02d}", 72, "(h-text_h)/2")]))
    return jobs


def render(out: Path, color: str, seconds: float,
           labels: list[tuple[str, int, str]], scratch: Path) -> None:
    filters = []
    for i, (text, px, y) in enumerate(labels):
        # textfile= rather than text=, so names never need filter escaping.
        textfile = scratch / f"label{i}.txt"
        textfile.write_text(text)
        filters.append(f"drawtext=fontfile={FONT}:textfile={textfile}:expansion=none:"
                       f"fontcolor={INK}:fontsize={px}:x=(w-text_w)/2:y={y}")
    out.parent.mkdir(parents=True, exist_ok=True)
    # Write to a dotfile first: the channel skips dotfiles, so it can never
    # pick up a half-written sample.
    part = out.with_name(f".{out.name}.part")
    subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"color=c={color}:s={SIZE}:r={FPS}",
         "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
         "-vf", ",".join(filters), "-t", f"{seconds:.1f}",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "96k", "-ar", "48000", "-ac", "2",
         "-movflags", "+faststart", "-f", "mp4", str(part)],
        check=True)
    os.replace(part, out)
    # Backdate an hour. The channel ignores files modified in the last 60 s
    # (they may still be copying); these are complete, so let them air now.
    past = time.time() - 3600
    os.utime(out, (past, past))


def clean() -> int:
    """Delete every [SAMPLE] file, then any folder that held one and is now empty."""
    removed, touched = 0, set()
    for root in (SHOWS_ROOT, BUMPS_ROOT):
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                if TAG in name:
                    os.remove(os.path.join(dirpath, name))
                    removed += 1
                    d = Path(dirpath)
                    while d != root:  # never the roots themselves
                        touched.add(d)
                        d = d.parent
    for d in sorted(touched, key=lambda p: len(p.parts), reverse=True):
        try:
            d.rmdir()  # only succeeds if empty, so real media is never touched
        except OSError:
            pass
    return removed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--force", action="store_true", help="remake samples that already exist")
    mode.add_argument("--clean", action="store_true", help="delete every [SAMPLE] file")
    args = ap.parse_args()

    for root in (SHOWS_ROOT, BUMPS_ROOT):
        if not root.is_dir():
            print(f"{root} is not mounted; run this via `docker compose run --rm samples`",
                  file=sys.stderr)
            return 1

    if args.clean:
        print(f"deleted {clean()} sample files")
        return 0

    made = kept = 0
    with tempfile.TemporaryDirectory() as scratch:
        for path, color, seconds, labels in plan():
            rel = path.relative_to("/media")
            if path.exists() and not args.force:
                kept += 1
                continue
            render(path, color, seconds, labels, Path(scratch))
            made += 1
            print(f"made  {rel}  ({seconds:.1f}s, #{color[2:]})")
    shows = len(SHOWS) * EPISODES_PER_SHOW
    print(f"{made} made, {kept} already there: {shows} episodes in {len(SHOWS)} shows, "
          f"{BUMP_COUNT} bumps")
    return 0


if __name__ == "__main__":
    sys.exit(main())
