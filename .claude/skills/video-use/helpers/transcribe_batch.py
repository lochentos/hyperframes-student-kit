"""Batch-transcribe every video in a directory with parallel workers.

Walks <videos_dir> for common video extensions, runs transcription on
each, writes transcripts to <videos_dir>/edit/transcripts/<name>.json.

Cached per-file: any source that already has a transcript is skipped.

Usage:
    python helpers/transcribe_batch.py <videos_dir>
    python helpers/transcribe_batch.py <videos_dir> --workers 4
    python helpers/transcribe_batch.py <videos_dir> --num-speakers 2
    python helpers/transcribe_batch.py <videos_dir> --edit-dir /custom/edit
    python helpers/transcribe_batch.py <videos_dir> --backend whisper
"""

from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Import from sibling file
import importlib.util, os
_here = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("transcribe", _here / "transcribe.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
load_api_key = _mod.load_api_key
transcribe_one = _mod.transcribe_one


VIDEO_EXTS = {".mp4", ".MP4", ".mov", ".MOV", ".mkv", ".MKV", ".avi", ".AVI", ".m4v", ".M4V"}


def find_videos(videos_dir: Path) -> list[Path]:
    return sorted(p for p in videos_dir.iterdir() if p.is_file() and p.suffix in VIDEO_EXTS)


def main() -> None:
    ap = argparse.ArgumentParser(description="Parallel batch transcription of a videos directory")
    ap.add_argument("videos_dir", type=Path, help="Directory containing source videos")
    ap.add_argument("--edit-dir", type=Path, default=None,
                    help="Edit output directory (default: <videos_dir>/edit)")
    ap.add_argument("--workers", type=int, default=4, help="Parallel workers (default: 4)")
    ap.add_argument("--language", type=str, default=None,
                    help="Optional ISO language code. Omit to auto-detect per file.")
    ap.add_argument("--num-speakers", type=int, default=None,
                    help="Optional number of speakers (Scribe only).")
    ap.add_argument("--backend", choices=["auto", "scribe", "whisper"], default="auto",
                    help="Transcription backend. Default: auto.")
    args = ap.parse_args()

    videos_dir = args.videos_dir.resolve()
    if not videos_dir.is_dir():
        sys.exit(f"not a directory: {videos_dir}")

    edit_dir = (args.edit_dir or (videos_dir / "edit")).resolve()
    (edit_dir / "transcripts").mkdir(parents=True, exist_ok=True)

    videos = find_videos(videos_dir)
    if not videos:
        sys.exit(f"no videos found in {videos_dir}")

    already_cached = [v for v in videos if (edit_dir / "transcripts" / f"{v.stem}.json").exists()]
    pending = [v for v in videos if v not in already_cached]

    print(f"found {len(videos)} videos ({len(already_cached)} cached, {len(pending)} to transcribe)")
    if not pending:
        print("nothing to do — all transcripts cached")
        return

    api_key = load_api_key()
    if args.backend == "scribe" and not api_key:
        sys.exit("--backend scribe requires ELEVENLABS_API_KEY in .env or environment")

    if not api_key and args.backend == "auto":
        print("no ELEVENLABS_API_KEY found — using local Whisper (word timestamps, single-speaker)")
    elif api_key:
        print("using ElevenLabs Scribe (word timestamps + speaker diarization)")

    # Whisper is not thread-safe with the model loaded in memory, so limit workers
    workers = args.workers if (api_key or args.backend == "scribe") else 1
    if workers != args.workers:
        print(f"note: whisper backend uses 1 worker (not {args.workers}) to avoid model conflicts")

    print(f"transcribing {len(pending)} files with {workers} worker(s)")
    t0 = time.time()

    errors: list[tuple[Path, str]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                transcribe_one,
                video=v,
                edit_dir=edit_dir,
                api_key=api_key,
                language=args.language,
                num_speakers=args.num_speakers,
                verbose=True,
                backend=args.backend,
            ): v
            for v in pending
        }
        for fut in as_completed(futures):
            v = futures[fut]
            try:
                out = fut.result()
                print(f"  + {v.stem}")
            except Exception as e:
                errors.append((v, str(e)))
                print(f"  x {v.stem}  FAILED: {e}")

    dt = time.time() - t0
    print(f"\ndone in {dt:.1f}s")
    if errors:
        print(f"{len(errors)} failures:")
        for v, msg in errors:
            print(f"  {v.name}: {msg}")
        sys.exit(1)


if __name__ == "__main__":
    main()
