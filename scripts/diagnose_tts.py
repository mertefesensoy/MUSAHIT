import asyncio
from pathlib import Path

from musahit.tts.encoder import wav_to_mp3
from musahit.tts.extractor import extract_voiced_sections
from musahit.tts.piper import PiperPythonClient
from musahit.tts.preprocessor import preprocess_for_tts

md = Path("briefings/2026/05/23/briefing.md").read_text(encoding="utf-8")
print(f"[1] briefing length: {len(md)} chars")

voiced = extract_voiced_sections(md)
print(f"[2] voiced result type: {type(voiced).__name__}")
print(f"[2] voiced preview: {str(voiced)[:200]!r}")

if hasattr(voiced, "chunks"):
    text = "\n\n".join(c.text if hasattr(c, "text") else str(c) for c in voiced.chunks)
elif hasattr(voiced, "text"):
    text = voiced.text
else:
    text = str(voiced)

processed = preprocess_for_tts(text)
print(f"[3] processed length: {len(processed)} chars")
print(f"[3] processed preview: {processed[:300]!r}")

if not processed.strip():
    print("FAIL: processed text is empty")
    raise SystemExit(1)

piper = PiperPythonClient(
    voice_path="C:/Users/senso/AppData/Local/piper/voices/tr_TR-dfki-medium.onnx"
)

async def synth():
    wav = await piper.synthesize(processed)
    print(f"[4] piper produced {len(wav)} bytes of WAV")
    mp3 = wav_to_mp3(wav)
    print(f"[5] encoder produced {len(mp3)} bytes of MP3")
    Path("briefings/2026/05/23/briefing_diagnostic.mp3").write_bytes(mp3)
    print("[6] wrote briefing_diagnostic.mp3")

asyncio.run(synth())
