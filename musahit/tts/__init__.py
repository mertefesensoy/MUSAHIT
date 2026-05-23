"""Phase 3 · delivery stage 1 · TTS.

Reads the writer's ``briefing.md``, extracts the ADR-009 § TTS scope
(header · DEFCON 1-2 · DEFCON 3 § summaries · AÇIK GELİŞMELER · closing),
preprocesses Turkish text for natural pronunciation, synthesises a WAV
via the Piper Python API (ADR-010 amended 2026-05-23 · NO subprocess),
encodes to MP3, writes ``briefings/YYYY/MM/DD/briefing.mp3`` and stamps
``briefings.audio_path``.
"""
