"""Tests for musahit.tts.encoder.

The ``wav_to_mp3`` roundtrip test is gated on ffmpeg's availability —
pydub shells out to ffmpeg under the hood and the test environment
may not have it installed (CI containers, fresh checkouts). The skip
is one of the project's authorized skips alongside the existing
optional-dependency tests.
"""

from __future__ import annotations

import io
import shutil
import wave

import pytest

from musahit.tts.encoder import check_ffmpeg_available, concatenate_wavs, wav_to_mp3
from musahit.tts.piper import FakePiper


def _wav_with_marker(byte_marker: int, n_samples: int = 100) -> bytes:
    """Construct a small WAV whose PCM frames are filled with a marker.

    The frames are ``n_samples`` little-endian int16 samples set to
    ``byte_marker``. This lets ``test_concatenate_preserves_frames``
    verify the concatenation order by reading the marker bytes.
    """
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        # Each sample is the same byte_marker value as int16.
        payload = byte_marker.to_bytes(2, "little", signed=False) * n_samples
        w.writeframes(payload)
    return buf.getvalue()


# ── concatenate_wavs ───────────────────────────────────────────────────────


class TestConcatenateWavs:
    def test_empty_list_returns_empty_wav(self) -> None:
        out = concatenate_wavs([])
        # Parseable WAV with zero frames.
        with wave.open(io.BytesIO(out), "rb") as w:
            assert w.getnframes() == 0

    def test_single_wav_roundtrip(self) -> None:
        w1 = _wav_with_marker(1, n_samples=50)
        out = concatenate_wavs([w1])
        # Frame count survives the roundtrip.
        with wave.open(io.BytesIO(out), "rb") as w:
            assert w.getnframes() == 50

    def test_two_wavs_concatenate_lengths(self) -> None:
        w1 = _wav_with_marker(1, n_samples=50)
        w2 = _wav_with_marker(2, n_samples=70)
        out = concatenate_wavs([w1, w2])
        with wave.open(io.BytesIO(out), "rb") as w:
            assert w.getnframes() == 120

    def test_concatenation_preserves_order(self) -> None:
        w1 = _wav_with_marker(0x0001, n_samples=10)
        w2 = _wav_with_marker(0x0002, n_samples=10)
        out = concatenate_wavs([w1, w2])
        with wave.open(io.BytesIO(out), "rb") as w:
            frames = w.readframes(w.getnframes())
        # First 20 bytes = ten int16=1 samples; next 20 = ten int16=2 samples.
        first_ten = frames[:20]
        next_ten = frames[20:40]
        assert all(b == 1 or b == 0 for b in first_ten)
        assert any(b == 2 for b in next_ten)

    def test_format_mismatch_raises(self) -> None:
        # Construct a second WAV with a different sample rate.
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(44100)  # mismatch
            w.writeframes(b"\x00\x00" * 100)
        w_normal = _wav_with_marker(1, n_samples=50)
        with pytest.raises(ValueError, match="format mismatch"):
            concatenate_wavs([w_normal, buf.getvalue()])


# ── ffmpeg pre-flight check ─────────────────────────────────────────────────


class TestCheckFfmpegAvailable:
    def test_raises_when_ffmpeg_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Patch shutil.which globally — monkeypatch reverts on teardown.
        monkeypatch.setattr(shutil, "which", lambda _name: None)
        with pytest.raises(RuntimeError, match="ffmpeg not found on PATH"):
            check_ffmpeg_available()

    def test_passes_when_ffmpeg_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Simulate ffmpeg discovery — any non-None return passes.
        monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/ffmpeg")
        # Should not raise.
        check_ffmpeg_available()

    def test_wav_to_mp3_short_circuits_without_ffmpeg(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``wav_to_mp3`` MUST raise RuntimeError before invoking pydub
        when ffmpeg is missing — surfacing the env gap clearly rather
        than letting pydub's deep ``FileNotFoundError [WinError 2]``
        leak out.
        """
        monkeypatch.setattr(shutil, "which", lambda _name: None)
        # Build a small but format-valid WAV so the test pins behaviour
        # on the ffmpeg check, not on a malformed-input failure.
        good_wav = _wav_with_marker(1, n_samples=10)
        with pytest.raises(RuntimeError, match="ffmpeg not found on PATH"):
            wav_to_mp3(good_wav)


# ── wav_to_mp3 (ffmpeg-gated) ───────────────────────────────────────────────


def _ffmpeg_available() -> bool:
    """True if pydub can find an ffmpeg binary in PATH."""
    return shutil.which("ffmpeg") is not None or shutil.which("avconv") is not None


@pytest.mark.skipif(
    not _ffmpeg_available(),
    reason="ffmpeg/avconv not on PATH — pydub MP3 encoding requires one of these. "
    "Operator install: winget install ffmpeg (or equivalent).",
)
class TestWavToMp3:
    async def test_roundtrip_produces_mp3_bytes(self) -> None:
        # Use a FakePiper WAV as the input so the test data is realistic.
        piper = FakePiper(samples_per_call=22050)  # 1 second of silence
        wav = await piper.synthesize("test")
        mp3 = wav_to_mp3(wav)
        # MP3 file signature: starts with either ID3 (ID3v2 tag) or
        # 0xFF 0xFB (MPEG-1 Layer 3 frame sync).
        assert mp3.startswith(b"ID3") or mp3[:2] == b"\xff\xfb"
        assert len(mp3) > 0
