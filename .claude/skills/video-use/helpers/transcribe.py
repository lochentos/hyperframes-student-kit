"""Transcribe a video — ElevenLabs Scribe if key available, local Whisper fallback.

Extracts mono 16kHz audio via ffmpeg, transcribes with word-level timestamps,
writes the full response to <edit_dir>/transcripts/<video_stem>.json.

Cached: if the output file already exists, the upload is skipped.

Output format is ElevenLabs Scribe-compatible regardless of which backend ran.
The `words` array entries have:
  - type: "word" | "spacing" | "audio_event"
  - text: the word text (or gap/event description)
  - start: float seconds
  - end: float seconds
  - speaker_id: "speaker_0" (Whisper assumes single speaker; Scribe diarizes)

Usage:
    python helpers/transcribe.py <video_path>
    python helpers/transcribe.py <video_path> --edit-dir /custom/edit
    python helpers/transcribe.py <video_path> --language en
    python helpers/transcribe.py <video_path> --num-speakers 2
    python helpers/transcribe.py <video_path> --backend whisper
    python helpers/transcribe.py <video_path> --backend scribe
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIBE_URL = "https://api.elevenlabs.io/v1/speech-to-text"
WHISPER_EXE = None  # resolved at runtime


def _find_whisper() -> str | None:
    """Find the whisper CLI on PATH."""
    candidates = [
        "C:/Users/linji/AppData/Local/Python/pythoncore-3.14-64/Scripts/whisper.exe",
        "whisper",
        "whisper.exe",
    ]
    for c in candidates:
        try:
            r = subprocess.run([c, "--help"], capture_output=True, timeout=5)
            if r.returncode in (0, 1):  # whisper exits 1 on --help
                return c
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return None


def load_api_key() -> str | None:
    """Return ElevenLabs API key or None if not configured."""
    for candidate in [Path(__file__).resolve().parent.parent / ".env", Path(".env")]:
        if candidate.exists():
            for line in candidate.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == "ELEVENLABS_API_KEY":
                    val = v.strip().strip('"').strip("'")
                    return val if val else None
    v = os.environ.get("ELEVENLABS_API_KEY", "")
    return v if v else None


def extract_audio(video_path: Path, dest: Path) -> None:
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        str(dest),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# -------- ElevenLabs Scribe backend ----------------------------------------

def call_scribe(
    audio_path: Path,
    api_key: str,
    language: str | None = None,
    num_speakers: int | None = None,
) -> dict:
    import requests

    data: dict[str, str] = {
        "model_id": "scribe_v1",
        "diarize": "true",
        "tag_audio_events": "true",
        "timestamps_granularity": "word",
    }
    if language:
        data["language_code"] = language
    if num_speakers:
        data["num_speakers"] = str(num_speakers)

    with open(audio_path, "rb") as f:
        resp = requests.post(
            SCRIBE_URL,
            headers={"xi-api-key": api_key},
            files={"file": (audio_path.name, f, "audio/wav")},
            data=data,
            timeout=1800,
        )

    if resp.status_code != 200:
        raise RuntimeError(f"Scribe returned {resp.status_code}: {resp.text[:500]}")

    return resp.json()


# -------- Local Whisper backend --------------------------------------------

def _whisper_to_scribe(whisper_json: dict) -> dict:
    """Convert whisper word-timestamp JSON to ElevenLabs Scribe format."""
    words = []
    prev_end = 0.0

    for segment in whisper_json.get("segments", []):
        for w in segment.get("words", []):
            raw = (w.get("word") or "").strip()
            if not raw:
                continue
            ws = float(w.get("start", 0.0))
            we = float(w.get("end", ws))

            # Insert spacing entry for gaps ≥ 50ms
            if prev_end > 0 and ws - prev_end >= 0.05:
                words.append({
                    "type": "spacing",
                    "text": " ",
                    "start": prev_end,
                    "end": ws,
                    "speaker_id": None,
                })

            words.append({
                "type": "word",
                "text": raw,
                "start": ws,
                "end": we,
                "speaker_id": "speaker_0",
            })
            prev_end = we

    return {"words": words, "_backend": "whisper"}


def call_whisper(
    audio_path: Path,
    language: str | None = None,
) -> dict:
    """Run whisper CLI with word timestamps on an audio file."""
    whisper_exe = _find_whisper()
    if not whisper_exe:
        raise RuntimeError("whisper CLI not found on PATH")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        cmd = [
            whisper_exe, str(audio_path),
            "--model", "base.en",
            "--output_format", "json",
            "--word_timestamps", "True",
            "--output_dir", str(tmp_dir),
        ]
        if language:
            cmd += ["--language", language]

        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        out_json = tmp_dir / f"{audio_path.stem}.json"
        if not out_json.exists():
            raise RuntimeError(f"Whisper did not produce output JSON at {out_json}")

        whisper_output = json.loads(out_json.read_text())

    return _whisper_to_scribe(whisper_output)


# -------- Unified transcription entry point --------------------------------

def transcribe_one(
    video: Path,
    edit_dir: Path,
    api_key: str | None = None,
    language: str | None = None,
    num_speakers: int | None = None,
    verbose: bool = True,
    backend: str = "auto",
) -> Path:
    """Transcribe a single video. Returns path to transcript JSON.

    Cached: returns existing path immediately if the transcript already exists.
    backend: "auto" | "scribe" | "whisper"
    """
    transcripts_dir = edit_dir / "transcripts"
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    out_path = transcripts_dir / f"{video.stem}.json"

    if out_path.exists():
        if verbose:
            print(f"cached: {out_path.name}")
        return out_path

    if verbose:
        print(f"  extracting audio from {video.name}", flush=True)

    t0 = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        audio = Path(tmp) / f"{video.stem}.wav"
        extract_audio(video, audio)
        size_mb = audio.stat().st_size / (1024 * 1024)

        # Choose backend
        use_scribe = (backend == "scribe") or (backend == "auto" and bool(api_key))
        use_whisper = (backend == "whisper") or (backend == "auto" and not api_key)

        if use_scribe and api_key:
            if verbose:
                print(f"  uploading to ElevenLabs Scribe ({size_mb:.1f} MB)", flush=True)
            payload = call_scribe(audio, api_key, language, num_speakers)
        elif use_whisper:
            if verbose:
                print(f"  transcribing with local Whisper ({size_mb:.1f} MB)", flush=True)
            payload = call_whisper(audio, language)
        else:
            raise RuntimeError("No transcription backend available. Set ELEVENLABS_API_KEY or install whisper.")

    out_path.write_text(json.dumps(payload, indent=2))
    dt = time.time() - t0

    if verbose:
        kb = out_path.stat().st_size / 1024
        backend_used = payload.get("_backend", "scribe")
        print(f"  saved: {out_path.name} ({kb:.1f} KB) in {dt:.1f}s [{backend_used}]")
        if isinstance(payload, dict) and "words" in payload:
            n_words = sum(1 for w in payload["words"] if w.get("type") == "word")
            print(f"    words: {n_words}")

    return out_path


def main() -> None:
    ap = argparse.ArgumentParser(description="Transcribe a video (ElevenLabs Scribe or local Whisper)")
    ap.add_argument("video", type=Path, help="Path to video file")
    ap.add_argument("--edit-dir", type=Path, default=None,
                    help="Edit output directory (default: <video_parent>/edit)")
    ap.add_argument("--language", type=str, default=None,
                    help="Optional ISO language code (e.g., 'en'). Omit to auto-detect.")
    ap.add_argument("--num-speakers", type=int, default=None,
                    help="Optional number of speakers (Scribe only).")
    ap.add_argument("--backend", choices=["auto", "scribe", "whisper"], default="auto",
                    help="Force a specific backend. Default: auto (Scribe if key available, else Whisper).")
    args = ap.parse_args()

    video = args.video.resolve()
    if not video.exists():
        sys.exit(f"video not found: {video}")

    edit_dir = (args.edit_dir or (video.parent / "edit")).resolve()
    api_key = load_api_key()

    if args.backend == "scribe" and not api_key:
        sys.exit("--backend scribe requires ELEVENLABS_API_KEY in .env or environment")

    transcribe_one(
        video=video,
        edit_dir=edit_dir,
        api_key=api_key,
        language=args.language,
        num_speakers=args.num_speakers,
        backend=args.backend,
    )


if __name__ == "__main__":
    main()
