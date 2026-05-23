"""Tests for musahit.tts.transitions."""

from __future__ import annotations

import io
import wave

from musahit.tts.transitions import (
    TICK_CHANNELS,
    TICK_DURATION_SECONDS,
    TICK_SAMPLE_RATE,
    TICK_SAMPLE_WIDTH_BYTES,
    generate_tick_tone,
    reset_cache,
)


class TestTickToneFormat:
    def test_returns_valid_wav_bytes(self) -> None:
        reset_cache()
        wav = generate_tick_tone()
        assert wav.startswith(b"RIFF")
        with wave.open(io.BytesIO(wav), "rb") as w:
            assert w.getframerate() == TICK_SAMPLE_RATE
            assert w.getsampwidth() == TICK_SAMPLE_WIDTH_BYTES
            assert w.getnchannels() == TICK_CHANNELS

    def test_duration_matches_configuration(self) -> None:
        reset_cache()
        wav = generate_tick_tone()
        with wave.open(io.BytesIO(wav), "rb") as w:
            n_frames = w.getnframes()
            duration = n_frames / w.getframerate()
        # Allow tiny rounding tolerance for integer-truncated sample count.
        assert abs(duration - TICK_DURATION_SECONDS) < 0.01


class TestTickToneCache:
    def test_second_call_returns_same_object(self) -> None:
        reset_cache()
        first = generate_tick_tone()
        second = generate_tick_tone()
        # ``is`` check — cache returns the same bytes object, not a copy.
        assert first is second

    def test_reset_cache_forces_regeneration(self) -> None:
        reset_cache()
        first = generate_tick_tone()
        reset_cache()
        second = generate_tick_tone()
        # New bytes object, but byte-equal content.
        assert first is not second
        assert first == second

    def test_byte_content_is_stable(self) -> None:
        """Deterministic generation — important for snapshot-style tests later."""
        reset_cache()
        a = generate_tick_tone()
        reset_cache()
        b = generate_tick_tone()
        assert a == b
