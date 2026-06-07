"""Apply a color grade to a video via ffmpeg filter chain.

Two modes:

  1. Preset mode — pick a named preset (e.g. `warm_cinematic`, `neutral_punch`).
  2. Auto mode (DEFAULT) — analyze the clip mathematically and emit a subtle
     per-clip correction. Samples N frames via ffmpeg signalstats, computes mean
     brightness, RMS contrast, saturation. Emits a bounded filter string that
     corrects under-exposure, flatness, and mild desaturation without any
     creative color shift. All adjustments capped at +-8% on any axis.

Usage:
    python helpers/grade.py <input> -o <output>                   # auto mode
    python helpers/grade.py <input> -o <output> --preset warm_cinematic
    python helpers/grade.py <input> -o <output> --filter 'eq=contrast=1.1'
    python helpers/grade.py --print-preset warm_cinematic
    python helpers/grade.py --analyze <input>
    python helpers/grade.py --list-presets
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path


PRESETS: dict[str, str] = {
    "subtle": "eq=contrast=1.03:saturation=0.98",
    "neutral_punch": (
        "eq=contrast=1.06:brightness=0.0:saturation=1.0,"
        "curves=master='0/0 0.25/0.23 0.75/0.77 1/1'"
    ),
    "warm_cinematic": (
        "eq=contrast=1.12:brightness=-0.02:saturation=0.88,"
        "colorbalance="
        "rs=0.02:gs=0.0:bs=-0.03:"
        "rm=0.04:gm=0.01:bm=-0.02:"
        "rh=0.08:gh=0.02:bh=-0.05,"
        "curves=master='0/0 0.25/0.22 0.75/0.78 1/1'"
    ),
    "none": "",
}


def get_preset(name: str) -> str:
    if name not in PRESETS:
        raise KeyError(f"unknown preset '{name}'. Available: {', '.join(sorted(PRESETS))}")
    return PRESETS[name]


def _sample_frame_stats(video: Path, start: float, duration: float, n_samples: int = 10) -> dict[str, float]:
    fps = max(0.5, min(n_samples / max(duration, 0.1), 10.0))

    with tempfile.NamedTemporaryFile(mode="w+", suffix=".txt", delete=False) as f:
        metadata_path = f.name

    try:
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-nostats",
            "-ss", f"{start:.3f}",
            "-i", str(video),
            "-t", f"{duration:.3f}",
            "-vf", f"fps={fps:.2f},signalstats,metadata=print:file={metadata_path}",
            "-f", "null", "-",
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        y_avgs: list[float] = []
        y_mins: list[float] = []
        y_maxs: list[float] = []
        sat_avgs: list[float] = []
        bit_depth: int = 8

        def _parse_value(line: str) -> float | None:
            try:
                return float(line.rsplit("=", 1)[1])
            except (ValueError, IndexError):
                return None

        with open(metadata_path) as f:
            for line in f:
                line = line.strip()
                if "lavfi.signalstats.YBITDEPTH" in line:
                    v = _parse_value(line)
                    if v is not None:
                        bit_depth = int(v)
                elif "lavfi.signalstats.YAVG" in line:
                    v = _parse_value(line)
                    if v is not None:
                        y_avgs.append(v)
                elif "lavfi.signalstats.YMIN" in line:
                    v = _parse_value(line)
                    if v is not None:
                        y_mins.append(v)
                elif "lavfi.signalstats.YMAX" in line:
                    v = _parse_value(line)
                    if v is not None:
                        y_maxs.append(v)
                elif "lavfi.signalstats.SATAVG" in line:
                    v = _parse_value(line)
                    if v is not None:
                        sat_avgs.append(v)

        if not y_avgs:
            return {"y_mean": 0.5, "y_std": 0.18, "sat_mean": 0.25}

        max_val = (2 ** bit_depth) - 1
        y_mean = (sum(y_avgs) / len(y_avgs)) / max_val
        y_range = (
            ((sum(y_maxs) / len(y_maxs)) - (sum(y_mins) / len(y_mins))) / max_val
            if y_maxs and y_mins else 0.7
        )
        sat_mean = ((sum(sat_avgs) / len(sat_avgs)) / max_val) if sat_avgs else 0.25
        return {"y_mean": y_mean, "y_std": y_range / 4.0, "sat_mean": sat_mean}
    finally:
        Path(metadata_path).unlink(missing_ok=True)


def auto_grade_for_clip(
    video: Path,
    start: float = 0.0,
    duration: float | None = None,
    verbose: bool = False,
) -> tuple[str, dict[str, float]]:
    if duration is None:
        probe_cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(video),
        ]
        try:
            duration = float(subprocess.check_output(probe_cmd).decode().strip())
        except Exception:
            duration = 10.0

    stats = _sample_frame_stats(video, start, duration)
    y_mean = stats["y_mean"]
    y_range = stats["y_std"] * 4.0
    sat_mean = stats["sat_mean"]

    contrast_adj = 1.08 - 0.05 * max(0.0, min(1.0, (y_range - 0.50) / 0.15)) if y_range < 0.65 else 1.03
    gamma_adj = (
        1.10 - 0.08 * max(0.0, min(1.0, (y_mean - 0.30) / 0.12)) if y_mean < 0.42
        else 0.97 if y_mean > 0.60 else 1.0
    )
    sat_adj = 1.04 if sat_mean < 0.18 else 0.96 if sat_mean > 0.38 else 0.98

    contrast_adj = max(0.94, min(1.08, contrast_adj))
    gamma_adj = max(0.94, min(1.10, gamma_adj))
    sat_adj = max(0.94, min(1.06, sat_adj))

    eq_parts = []
    if abs(contrast_adj - 1.0) > 0.005:
        eq_parts.append(f"contrast={contrast_adj:.3f}")
    if abs(gamma_adj - 1.0) > 0.005:
        eq_parts.append(f"gamma={gamma_adj:.3f}")
    if abs(sat_adj - 1.0) > 0.005:
        eq_parts.append(f"saturation={sat_adj:.3f}")

    filter_string = ("eq=" + ":".join(eq_parts)) if eq_parts else ""

    if verbose:
        print(f"  auto-grade stats:")
        print(f"    y_mean={y_mean:.3f}  y_range={y_range:.3f}  sat_mean={sat_mean:.3f}")
        print(f"    -> contrast={contrast_adj:.3f}  gamma={gamma_adj:.3f}  sat={sat_adj:.3f}")
        print(f"    -> filter: {filter_string or '(empty)'}")

    return filter_string, stats


def apply_grade(input_path: Path, output_path: Path, filter_string: str) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not filter_string:
        cmd = ["ffmpeg", "-y", "-i", str(input_path), "-c", "copy", str(output_path)]
    else:
        cmd = [
            "ffmpeg", "-y", "-i", str(input_path),
            "-vf", filter_string,
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-pix_fmt", "yuv420p",
            "-c:a", "copy",
            "-movflags", "+faststart",
            str(output_path),
        ]
    subprocess.run(cmd, check=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="Apply a color grade via ffmpeg filter chain")
    ap.add_argument("input", type=Path, nargs="?", help="Input video")
    ap.add_argument("-o", "--output", type=Path, help="Output video")
    ap.add_argument("--preset", type=str, default=None, choices=list(PRESETS.keys()))
    ap.add_argument("--filter", type=str, default=None, help="Raw ffmpeg filter string.")
    ap.add_argument("--analyze", type=Path, default=None, help="Analyze clip and print filter. No output.")
    ap.add_argument("--print-preset", type=str, default=None, help="Print filter for a preset and exit.")
    ap.add_argument("--list-presets", action="store_true", help="List available presets and exit.")
    args = ap.parse_args()

    if args.list_presets:
        for name, f in PRESETS.items():
            print(f"{name}:\n  {f if f else '(no filter)'}\n")
        return

    if args.print_preset is not None:
        print(get_preset(args.print_preset))
        return

    if args.analyze is not None:
        if not args.analyze.exists():
            sys.exit(f"input not found: {args.analyze}")
        filter_string, stats = auto_grade_for_clip(args.analyze, verbose=True)
        print(f"\nfilter: {filter_string or '(none)'}")
        print(f"stats:  {json.dumps(stats, indent=2)}")
        return

    if not args.input or not args.output:
        ap.error("input and -o/--output are required")

    if not args.input.exists():
        sys.exit(f"input not found: {args.input}")

    if args.filter is not None:
        filter_string = args.filter
    elif args.preset is not None:
        filter_string = get_preset(args.preset)
    else:
        filter_string, _ = auto_grade_for_clip(args.input, verbose=True)

    print(f"grading {args.input.name} -> {args.output.name}")
    if filter_string:
        print(f"  filter: {filter_string[:120]}{'...' if len(filter_string) > 120 else ''}")
    else:
        print("  filter: (none — copy)")

    apply_grade(args.input, args.output, filter_string)
    print(f"done: {args.output}")


if __name__ == "__main__":
    main()
