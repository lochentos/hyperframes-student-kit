---
name: video-use
description: Edit any video by conversation. Transcribe, cut, color grade, generate overlay animations, burn subtitles — for talking heads, montages, tutorials, travel, interviews. No presets, no menus. Ask questions, confirm the plan, execute, iterate, persist. Production-correctness rules are hard; everything else is artistic freedom.
---

# Video Use

## Principle

1. **LLM reasons from raw transcript + on-demand visuals.** The only derived artifact that earns its keep is a packed phrase-level transcript (`takes_packed.md`). Everything else — filler tagging, retake detection, shot classification, emphasis scoring — you derive at decision time.
2. **Audio is primary, visuals follow.** Cut candidates come from speech boundaries and silence gaps. Drill into visuals only at decision points.
3. **Ask → confirm → execute → iterate → persist.** Never touch the cut until the user has confirmed the strategy in plain English.
4. **Generalize.** Do not assume what kind of video this is. Look at the material, ask the user, then edit.
5. **Artistic freedom is the default.** Every specific value, preset, font, color, duration, pitch structure, and technique in this document is a *worked example* from one proven video — not a mandate. Read them to understand what's possible and why each worked. Then make your own taste calls based on what the material actually is and what the user actually wants. **The only things you MUST do are in the Hard Rules section below.** Everything else is yours.
6. **Invent freely.** If the material calls for a technique not described here — split-screen, picture-in-picture, lower-third identity cards, reaction cuts, speed ramps, freeze frames, crossfades, match cuts, L-cuts, J-cuts, speed ramps over breath, whatever — build it. The helpers are ffmpeg and PIL. They can do anything the format supports. Do not wait for permission.
7. **Verify your own output before showing it to the user.** If you wouldn't ship it, don't present it.

## Hard Rules (production correctness — non-negotiable)

These are the things where deviation produces silent failures or broken output. They are not taste, they are correctness. Memorize them.

1. **Subtitles are applied LAST in the filter chain**, after every overlay. Otherwise overlays hide captions. Silent failure.
2. **Per-segment extract → lossless `-c copy` concat**, not single-pass filtergraph. Otherwise you double-encode every segment when overlays are added.
3. **30ms audio fades at every segment boundary** (`afade=t=in:st=0:d=0.03,afade=t=out:st={dur-0.03}:d=0.03`). Otherwise audible pops at every cut.
4. **Overlays use `setpts=PTS-STARTPTS+T/TB`** to shift the overlay's frame 0 to its window start. Otherwise you see the middle of the animation during the overlay window.
5. **Master SRT uses output-timeline offsets**: `output_time = word.start - segment_start + segment_offset`. Otherwise captions misalign after segment concat.
6. **Never cut inside a word.** Snap every cut edge to a word boundary from the transcript.
7. **Pad every cut edge.** Working window: 30–200ms. Transcript timestamps drift 50–100ms — padding absorbs the drift. Tighter for fast-paced, looser for cinematic.
8. **Word-level verbatim ASR only.** Never SRT/phrase mode (loses sub-second gap data). Never normalized fillers (loses editorial signal).
9. **Cache transcripts per source.** Never re-transcribe unless the source file itself changed.
10. **Parallel sub-agents for multiple animations.** Never sequential. Spawn N at once via the `Agent` tool; total wall time ≈ slowest one.
11. **Strategy confirmation before execution.** Never touch the cut until the user has approved the plain-English plan.
12. **All session outputs in `<videos_dir>/edit/`.** Never write inside the skill directory.

Everything else in this document is a worked example. Deviate whenever the material calls for it.

## Directory layout

The skill lives in `.claude/skills/video-use/`. User footage lives wherever they put it. All session outputs go into `<videos_dir>/edit/`.

```
<videos_dir>/
├── <source files, untouched>
└── edit/
    ├── project.md               ← memory; appended every session
    ├── takes_packed.md          ← phrase-level transcripts, the LLM's primary reading view
    ├── edl.json                 ← cut decisions
    ├── transcripts/<name>.json  ← cached raw transcript JSON
    ├── animations/slot_<id>/    ← per-animation source + render + reasoning
    ├── clips_graded/            ← per-segment extracts with grade + fades
    ├── master.srt               ← output-timeline subtitles
    ├── verify/                  ← debug frames / timeline PNGs
    ├── preview.mp4
    └── final.mp4
```

## Setup

Helpers live in `.claude/skills/video-use/helpers/`. Resolve them relative to this skill file. On cold start verify:

- `ffmpeg` + `ffprobe` on PATH (already installed in this workspace).
- Python deps installed: `pip install numpy Pillow requests` (already done).
- **Transcription:** Two options — ElevenLabs Scribe (preferred, word-level + diarization) or local Whisper (fallback, already installed).
  - For ElevenLabs: set `ELEVENLABS_API_KEY` in `.env` at the video-use skill root, or in the environment.
  - For local Whisper: no key needed. The helpers auto-detect which is available.
- Node.js + npm available if the session needs HyperFrames for animations. HyperFrames requires Node.js 22+.

**Transcription quality note:** ElevenLabs Scribe gives superior word-level timestamps, speaker diarization, and audio event detection (laughs, sighs). Local Whisper is adequate for basic retake/silence editing but misses speaker IDs and audio events. For professional deliverables, get the ElevenLabs key.

## Helpers

Resolve all helper paths relative to this SKILL.md (i.e., `.claude/skills/video-use/helpers/`).

- **`transcribe.py <video>`** — single-file transcription. Uses ElevenLabs Scribe if key available, otherwise local Whisper. Cached. `--num-speakers N` optional.
- **`transcribe_batch.py <videos_dir>`** — parallel batch transcription (4 workers default).
- **`pack_transcripts.py --edit-dir <dir>`** — `transcripts/*.json` → `takes_packed.md` (phrase-level, break on silence ≥ 0.5s).
- **`timeline_view.py <video> <start> <end>`** — filmstrip + waveform PNG. On-demand visual drill-down. **Not a scan tool** — use it at decision points.
- **`render.py <edl.json> -o <out>`** — per-segment extract → concat → overlays (PTS-shifted) → subtitles LAST. `--preview` for 720p fast. `--build-subtitles` to generate master.srt inline.
- **`grade.py <in> -o <out>`** — ffmpeg filter chain grade. Presets + `--filter '<raw>'` for custom.

**Windows path note:** When calling helpers on Windows, use full absolute paths with forward slashes or escaped backslashes.

## The process

1. **Inventory.** `ffprobe` every source. Run `transcribe_batch.py` on the directory. Run `pack_transcripts.py` to produce `takes_packed.md`. Sample one or two `timeline_view`s for a visual first impression.
2. **Pre-scan for problems.** One pass over `takes_packed.md` to note verbal slips, obvious mis-speaks, or phrasings to avoid. Plain list, feed into the editor brief.
3. **Converse.** Describe what you see in plain English. Ask questions *shaped by the material*. Collect: content type, target length/aspect, aesthetic/brand direction, pacing feel, must-preserve moments, must-cut moments, animation and grade preferences, subtitle needs. Do not use a fixed checklist — the right questions are different every time.
4. **Propose strategy.** 4–8 sentences: shape, take choices, cut direction, animation plan, grade direction, subtitle style, length estimate. **Wait for confirmation.**
5. **Execute.** Produce `edl.json` via the editor sub-agent brief. Drill into `timeline_view` at ambiguous moments. Build animations in parallel sub-agents. Apply grade per-segment. Compose via `render.py`.
6. **Preview.** `render.py --preview`.
7. **Self-eval (before showing the user).** Run `timeline_view` on the **rendered output** (not the sources) at every cut boundary (±1.5s window). Check each image for:
   - Visual discontinuity / flash / jump at the cut
   - Waveform spike at the boundary (audio pop that slipped past the 30ms fade)
   - Subtitle hidden behind an overlay (Rule 1 violation)
   - Overlay misaligned or showing wrong frames (Rule 4 violation)

   Also sample: first 2s, last 2s, and 2–3 mid-points — check grade consistency, subtitle readability, overall coherence. Run `ffprobe` on the output to verify duration matches the EDL expectation.

   If anything fails: fix → re-render → re-eval. **Cap at 3 self-eval passes** — if issues remain after 3, flag them to the user rather than looping forever. Only present the preview once the self-eval passes.
8. **Iterate + persist.** Natural-language feedback, re-plan, re-render. Never re-transcribe. Final render on confirmation. Append to `project.md`.

## Cut craft (techniques)

- **Audio-first.** Candidate cuts from word boundaries and silence gaps.
- **Preserve peaks.** Laughs, punchlines, emphasis beats. Extend past punchlines to include reactions — the laugh IS the beat.
- **Speaker handoffs** benefit from air between utterances. Common values: 400–600ms. Less for fast-paced, more for cinematic. Taste call.
- **Audio events as signals.** `(laughs)`, `(sighs)`, `(applause)` mark beats. Extend past them.
- **Silence gaps are cut candidates.** Silences ≥400ms are usually the cleanest. 150–400ms phrase boundaries are usable with a visual check. <150ms is unsafe (mid-phrase).
- **Example cut padding:** 50ms before the first kept word, 80ms after the last. Tighter for montage energy, looser for documentary. Stay in the 30–200ms working window (Hard Rule 7).
- **Never reason audio and video independently.** Every cut must work on both tracks.

## Retake and silence editing (Lin's primary use case)

When the task is "clean up raw talking-head footage — remove silences, retakes, and mistakes":

1. Transcribe all files → `takes_packed.md`
2. Read `takes_packed.md` to understand what was said and how many times each phrase was repeated.
3. **Silence removal:** Any gap ≥ 400ms is a cut candidate. Gaps ≥ 1.5s between phrases are almost always dead space — cut them.
4. **Retake detection:** When you see the same phrase (≥ 70% word overlap) appearing 2+ times, those are retakes. Keep ALL complete takes (user picks best delivery). Remove only obvious fragments (< 3 words of a phrase that completes later).
5. **Mistake identification:** Sentence that stops mid-phrase AND is followed by a restart of the same thought → the fragment is a mistake. Remove it. When unsure → keep (the user can always cut more; you cannot restore deleted audio).
6. **Conservative by default.** If a segment is ambiguous — keep it. Label your reasoning in `edl.json` `"reason"` field.
7. Build `edl.json` with every kept take as a range entry. Run `render.py` to assemble.
8. Output files to `<videos_dir>/edit/`.

## The packed transcript (primary reading view)

`pack_transcripts.py` reads all `transcripts/*.json` and produces one markdown file where each take is a list of phrase-level lines, each prefixed with its `[start-end]` time range. Phrases break on any silence ≥ 0.5s OR speaker change. This is the artifact the editor sub-agent reads to pick cuts — it gives word-boundary precision from text alone at 1/10 the tokens of raw JSON.

Example line:
```
## DJI_20260428163801_0108_D  (duration: 25.3s, 3 phrases)
  [000.00-004.70] S0 Modern day halsome culture is the reason why we are all poor.
  [004.70-009.58] S0 Modern day halsome culture is keeping us all poor.
  [009.58-025.30] S0 Modern day halsome culture is keeping us all poor.
```

## Editor sub-agent brief (for multi-take selection)

When the task is "pick the best take of each beat across many clips," spawn a dedicated sub-agent with a brief shaped like this:

```
You are editing a <type> video. Pick the best take of each beat and 
assemble them chronologically by beat, not by source clip order.

INPUTS:
  - takes_packed.md (time-annotated phrase-level transcripts of all takes)
  - Product/narrative context: <2 sentences from the user>
  - Speaker(s): <name, role, delivery style note>
  - Expected structure: <pick an archetype or invent one>
  - Verbal slips to avoid: <list from the pre-scan pass>
  - Target runtime: <seconds>

RULES:
  - Start/end times must fall on word boundaries from the transcript.
  - Pad cut boundaries (working window 30–200ms).
  - Prefer silences ≥ 400ms as cut targets.
  - Keep ALL complete takes of repeated phrases (user picks delivery).
  - If a phrase is incomplete (< 3 words, stops mid-sentence), remove only if
    the same phrase completes later in the same clip. Otherwise keep.
  - Note reasons for every non-obvious decision in the "reason" field.

OUTPUT (JSON array, no prose):
  [{"source": "DJI_0108_D", "start": 0.05, "end": 4.65, "beat": "HOOK",
    "quote": "...", "reason": "..."}, ...]

Return the final EDL and a one-line total runtime check.
```

## Color grade (when requested)

Your job is to **reason about the image**, not apply a preset. Look at a frame (via `timeline_view`), decide what's wrong, adjust one thing, look again.

Mental model is ASC CDL. Per channel: `out = (in * slope + offset) ** power`, then global saturation.

**Presets** (`grade.py --list-presets`):
- **`warm_cinematic`** — retro/technical, subtle teal/orange split, desaturated.
- **`neutral_punch`** — minimal corrective: contrast bump + gentle S-curve. No hue shifts.
- **`none`** — straight copy.

Default: `auto` mode analyzes each segment and applies a data-driven subtle correction (±8% max on any axis). Safe for talking heads.

## Subtitles (when requested)

**`bold-overlay`** style (proven for short-form social):
```
FontName=Helvetica,FontSize=18,Bold=1,
PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H00000000,
BorderStyle=1,Outline=2,Shadow=0,
Alignment=2,MarginV=90
```
2-word UPPERCASE chunks, break on punctuation, MarginV=90 keeps captions clear of social platform UI chrome. Subtitles LAST (Hard Rule 1).

## Animations (when requested)

**Tool options:**
- **HyperFrames** — HTML/CSS/GSAP compositions: kinetic typography, product UI motion, overlays, lower thirds. Already in this workspace.
- **PIL + PNG sequence + ffmpeg** — simple overlay cards, counters, typewriter text. Fast to iterate.
- **Manim** — formal diagrams, state machines, equation derivations.

For HyperFrames animation slots, scaffold inside `edit/animations/slot_<id>/` and use the `/hyperframes` skill.

**Duration rules:** ≥ 3s for sync-to-narration cards, 0.5–2s for beat-synced accents. Hold final frame ≥ 1s before cut. Over voiceover: duration ≥ narration_length + 1s.

## Output spec

Match the source unless the user asks for something specific. Common targets: `1920×1080@24` cinematic, `1920×1080@30` screen content, `1080×1920@30` vertical social. `render.py` defaults to 1080p.

## EDL format

```json
{
  "version": 1,
  "sources": {"DJI_0108_D": "/abs/path/DJI_20260428163801_0108_D.MP4"},
  "ranges": [
    {"source": "DJI_0108_D", "start": 0.05, "end": 4.65,
     "beat": "INTRO", "quote": "Modern day halsome culture...", "reason": "Clean complete take."},
    {"source": "DJI_0108_D", "start": 4.65, "end": 9.50,
     "beat": "INTRO_TAKE2", "quote": "Modern day halsome culture...", "reason": "Keep — user picks delivery."}
  ],
  "grade": "auto",
  "overlays": [],
  "subtitles": null,
  "total_duration_s": 9.45
}
```

## Memory — `project.md`

Append one section per session at `<edit>/project.md`:

```markdown
## Session N — YYYY-MM-DD

**Strategy:** one paragraph describing the approach
**Decisions:** take choices, cuts, grades, animations + why
**Reasoning log:** one-line rationale for non-obvious decisions
**Outstanding:** deferred items
```

On startup, read `project.md` if it exists and summarize the last session in one sentence before asking whether to continue.

## Anti-patterns

- **Hierarchical pre-computed scoring.** Over-engineering. Derive from the transcript at decision time.
- **Hand-tuned moment-scoring functions.** The LLM picks better than any heuristic.
- **Running transcription with phrase-level timestamps.** Always word-level.
- **Re-transcribing cached sources.** Check if transcript exists first.
- **Burning subtitles before overlays.** Overlays hide them. (Hard Rule 1.)
- **Single-pass filtergraph when you have overlays.** Double re-encodes. Per-segment extract → concat.
- **Hard audio cuts.** Always 30ms fade in/out at every boundary (Hard Rule 3).
- **Sequential sub-agents for multiple animations.** Always parallel.
- **Editing before confirming strategy.** Never.
- **Assuming what kind of video it is.** Look first, ask second, edit last.
- **Being trigger-happy on "remove."** When unsure — keep. The user can always cut; you can't restore.
