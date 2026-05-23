"""Tests for musahit.tts.piper.

The real :class:`piper.PiperVoice` model is NEVER loaded in tests. The
production class :class:`PiperPythonClient` is exercised by monkey-
patching :func:`piper.PiperVoice.load` so we verify the constructor
path runs end-to-end without touching the ONNX file or the espeak-ng
data bundle. The default code path uses :class:`FakePiper`.
"""

from __future__ import annotations

import io
import wave
from pathlib import Path

import pytest

from musahit.tts.piper import (
    DEFAULT_TIMEOUT_SECONDS,
    FailingPiper,
    FakePiper,
    PiperPythonClient,
)

# ── FakePiper ──────────────────────────────────────────────────────────────


class TestFakePiper:
    async def test_returns_valid_wav_bytes(self) -> None:
        piper = FakePiper()
        wav = await piper.synthesize("merhaba")
        # WAV starts with "RIFF" magic.
        assert wav.startswith(b"RIFF")
        # Parseable as a wave file with the expected format.
        with wave.open(io.BytesIO(wav), "rb") as w:
            assert w.getframerate() == 22050
            assert w.getsampwidth() == 2
            assert w.getnchannels() == 1

    async def test_records_calls(self) -> None:
        piper = FakePiper()
        await piper.synthesize("ilk metin")
        await piper.synthesize("ikinci metin")
        assert piper.call_count == 2
        assert piper.calls == ["ilk metin", "ikinci metin"]

    async def test_deterministic_per_samples_per_call(self) -> None:
        piper = FakePiper(samples_per_call=1000)
        a = await piper.synthesize("x")
        b = await piper.synthesize("y")
        # Same byte length each call (content differs only by name).
        assert len(a) == len(b)


# ── FailingPiper ───────────────────────────────────────────────────────────


class TestFailingPiper:
    async def test_raises_on_call(self) -> None:
        piper = FailingPiper()
        with pytest.raises(RuntimeError, match="simulated piper failure"):
            await piper.synthesize("anything")

    async def test_records_calls_before_raising(self) -> None:
        piper = FailingPiper()
        with pytest.raises(RuntimeError):
            await piper.synthesize("önce")
        assert piper.call_count == 1


# ── PiperPythonClient ──────────────────────────────────────────────────────


class TestPiperPythonClient:
    def test_raises_when_voice_path_missing(self, tmp_path: Path) -> None:
        missing = tmp_path / "nonexistent.onnx"
        with pytest.raises(FileNotFoundError, match="not found"):
            PiperPythonClient(missing)

    def test_loads_voice_via_piper_voice_load(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Constructor should call PiperVoice.load with the resolved path.

        We monkeypatch the load classmethod so no ONNX file is touched.
        """
        fake_voice_path = tmp_path / "tr_TR-dfki-medium.onnx"
        fake_voice_path.write_bytes(b"not a real onnx but exists")

        captured: dict[str, str] = {}

        class _FakeVoice:
            pass

        def fake_load(path: str) -> _FakeVoice:
            captured["path"] = str(path)
            return _FakeVoice()

        # Patch the symbol used inside PiperPythonClient.__init__.
        from piper import PiperVoice  # local import to mirror production lazy import

        monkeypatch.setattr(PiperVoice, "load", staticmethod(fake_load))

        client = PiperPythonClient(fake_voice_path)
        assert captured["path"] == str(fake_voice_path)
        # The client holds the fake voice for reuse across synth calls.
        assert isinstance(client._voice, _FakeVoice)  # type: ignore[attr-defined]

    async def test_synthesize_drives_synthesize_wav(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_voice_path = tmp_path / "tr_TR-dfki-medium.onnx"
        fake_voice_path.write_bytes(b"not a real onnx")

        captured: dict[str, object] = {}

        class _FakeVoice:
            def synthesize_wav(self, text: str, wav_file: wave.Wave_write) -> None:
                captured["text"] = text
                # Write a recognisable PCM frame to verify our buffer
                # captures what synthesize_wav writes.
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(22050)
                wav_file.writeframes(b"\x01\x00\x02\x00\x03\x00")

        from piper import PiperVoice

        monkeypatch.setattr(PiperVoice, "load", staticmethod(lambda _p: _FakeVoice()))
        client = PiperPythonClient(fake_voice_path)
        wav = await client.synthesize("merhaba dünya")
        assert captured["text"] == "merhaba dünya"
        assert wav.startswith(b"RIFF")
        # The PCM payload we wrote shows up in the WAV.
        assert b"\x01\x00\x02\x00\x03\x00" in wav

    def test_default_timeout_value(self) -> None:
        assert DEFAULT_TIMEOUT_SECONDS == 60.0
