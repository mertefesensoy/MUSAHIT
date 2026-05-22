# ADR-010 · TTS and delivery

**Status** · Accepted · 2026-05-22
**Author** · Mert Efe Şensoy
**Supersedes** · none
**Cross-references** · ADR-009 · ADR-011

---

## ❯ Context

The operator requested that the morning briefing be voiced in Turkish. The hardware is a
Windows laptop with iGPU only and no internet dependency requirement for inference. The
PoI aesthetic argues for a slightly synthetic, deliberate voice rather than a fully
natural one · the Machine is a system, not a person.

## ❯ Decision

**Piper TTS** with the Turkish voice `tr_TR-dfki-medium` synthesizes the briefing's voiced
portion at the end of the pipeline. The output is an MP3 file delivered alongside the
HTML dashboard.

### Why Piper

- **Local** · runs entirely offline · no API dependency · matches the project's local-LLM
  posture
- **Fast** · ~50 ms/sentence on CPU · the entire voiced portion (4-6 minutes of audio)
  synthesizes in under 2 minutes
- **Quality** · the `tr_TR-dfki-medium` voice is intelligible · slight robotic edge fits
  the PoI aesthetic
- **Lightweight** · ~60 MB model · loads in under a second
- **Stable** · no API changes · the model file is checked into the system after first
  pull and does not need to be re-downloaded

### Installation

The install script downloads the voice model:

```powershell
# Inside scripts/install_windows.ps1
$voiceDir = "$env:LOCALAPPDATA\piper\voices"
New-Item -ItemType Directory -Force -Path $voiceDir
Invoke-WebRequest `
    -Uri "https://huggingface.co/rhasspy/piper-voices/resolve/main/tr/tr_TR/dfki/medium/tr_TR-dfki-medium.onnx" `
    -OutFile "$voiceDir\tr_TR-dfki-medium.onnx"
Invoke-WebRequest `
    -Uri "https://huggingface.co/rhasspy/piper-voices/resolve/main/tr/tr_TR/dfki/medium/tr_TR-dfki-medium.onnx.json" `
    -OutFile "$voiceDir\tr_TR-dfki-medium.onnx.json"
```

Python integration via `piper-tts` package:

```python
from piper import PiperVoice

voice = PiperVoice.load("path/to/tr_TR-dfki-medium.onnx")
with open("briefing.wav", "wb") as f:
    voice.synthesize(text, f)
```

### Pipeline integration

The TTS stage runs at 06:00 in the pipeline. Its input is the parsed briefing markdown
(produced by the writer at 05:00-06:00). The TTS stage:

1. Parses the briefing markdown into sections
2. Extracts the voiced sections per ADR-009 scope:
   - Header
   - DEFCON 1-2 · ÖNCELİKLİ (full items)
   - DEFCON 3 · MATERYAL (one-paragraph summaries only)
   - AÇIK GELİŞMELER (full)
   - Closing line
3. Strips markdown formatting (links, bold, italics) but preserves natural punctuation
4. Inserts brief pauses (`<break time="500ms" />` equivalent · Piper uses sentence
   boundaries naturally)
5. Inserts section transitions with a brief sound cue (a soft sub-bass tick · 200ms ·
   embedded in the rendered audio)
6. Synthesizes to WAV
7. Encodes to MP3 (128 kbps · mono · sufficient for speech)
8. Writes to `briefings/YYYY/MM/DD/briefing.mp3`

### Section transition tones (PoI aesthetic touch)

Each major section break (DEFCON tier change, arc section start) gets a 200ms 80 Hz
sub-bass tick. The tone serves both as an audio cue and as the PoI sonic signature ·
analogous to the show's transition stings.

The tones are pre-rendered as `dashboard/static/audio/tick.wav` and concatenated into the
final mp3 at synthesis time.

### Text preprocessing for TTS

Some preprocessing is needed for clean Turkish pronunciation:

- **Abbreviations** · `TCMB` → "Tee See Em Be" · `BDDK` → "Be De De Ka" · etc. · a small
  dictionary in `src/tts/abbreviations.py`
- **Numbers** · "500 baz puan" stays as is · Piper handles this · monitor for issues
- **Foreign names** · "Biden" · "Putin" · etc. · stay as written · Piper produces Turkish
  pronunciation which is acceptable
- **DEFCON labels** · "DEFCON 2" → "DEFCON İki" for clearer pronunciation
- **Markdown stripping** · `**bold**` · `*italic*` · `[link](url)` · all removed
- **Source attribution lines** · "Kaynaklar · anadolu·gov_aligned · ..." · skipped in
  voiced output (these are visual reference only)

### Delivery

The audio file is written to `briefings/YYYY/MM/DD/briefing.mp3` at 06:30. The dashboard
serves it via a route `/briefing/audio/today` (FastAPI streams the file).

The dashboard's briefing view has an audio player at the top:

```html
<audio controls preload="metadata" src="/briefing/audio/today">
    Your browser does not support audio playback.
</audio>
```

The operator can listen while reading the HTML version below. The audio plays the
voiced portion only · the HTML shows everything.

### Operator can rerun TTS

If the audio quality is poor for a specific run, the operator can rerun TTS without
regenerating the briefing:

```powershell
python -m musahit.pipeline run --date today --stage tts --only
```

The `--only` flag ensures only the TTS stage runs.

### Fallback

If Piper synthesis fails (rare · usually a malformed input), the pipeline:

1. Logs the failure to `pipeline_runs.stages_done` as `tts: FAILED`
2. Writes a placeholder `briefing.mp3` (silent · 1 second) so the dashboard player still
   loads
3. The dashboard shows a banner · "Ses sentezi başarısız · briefi yazılı olarak okuyun"
4. The briefing's written form is unaffected

This is consistent with ADR-012's failure isolation policy: the day's briefing always
ships even when components fail.

## ❯ Consequences

**Positive**
- Fully local TTS · matches privacy posture · no API costs · no internet dependency
- Piper's slight robotic edge complements PoI aesthetic rather than fighting it
- Pipeline can rerun just TTS for quality issues without burning LLM time
- Section transition tones add identity without being intrusive

**Negative**
- Voice is not premium quality · operator must accept the robotic edge · the alternative
  (XTTS-v2) was rejected for CPU performance reasons
- Turkish pronunciation of foreign names is occasionally rough · acceptable for OSINT
  context · would need a phoneme dictionary to fix systematically · deferred
- MP3 encoding adds a small dependency (`ffmpeg-python` or `pydub`) · acceptable

## ❯ Alternatives considered

- **Coqui XTTS-v2** · higher quality · supports voice cloning · slow on CPU (10x
  realtime would be ~50 minutes for 5 minutes of audio) · rejected for budget reasons
- **Microsoft Edge TTS** · excellent Turkish voices (`tr-TR-EmelNeural`,
  `tr-TR-AhmetNeural`) · not local · free · operator chose local · rejected
- **Microsoft SAPI Turkish voice** · built-in to Windows · quality is significantly
  worse than Piper · rejected
- **No TTS in v0.1** · operator explicitly asked for voiced output · rejected

## ❯ Open questions

- Phoneme dictionary for proper pronunciation of foreign names · deferred · revisit if
  it becomes annoying in practice
- Whether to add a "play at 1.5x" option in the dashboard · the HTML audio element
  supports `playbackRate` · trivial addition · likely add in dashboard polish round
- Whether the operator wants a Bluetooth speaker auto-play at 07:00 via Windows audio
  routing · this is operator-side configuration, not in scope for the system
