"""Section transition tones (PoI sonic signature).

Per ADR-010 § Section transition tones, each major section break in the
synthesised audio gets a 200 ms, 80 Hz mono sub-bass tick. The function
:func:`generate_tick_tone` returns the WAV bytes for one such tick. It
is cached at module level so repeated calls (one tick per section in a
single synthesis pass) reuse the same buffer.

Pure stdlib (``math``, ``struct``, ``wave``) — no numpy dependency.
Sample rate, bit depth, and channel count match the Piper voice output
(22050 Hz, 16-bit signed, mono) so :mod:`musahit.tts.encoder` can
concatenate Piper WAVs and tick WAVs without resampling.
"""

from __future__ import annotations

import io
import math
import struct
import wave

# WAV format constants — these MUST match the Piper voice
# ``tr_TR-dfki-medium`` configuration (22050 Hz, 16-bit, mono). If the
# voice config ever changes, the synthesiser will fail to concatenate
# WAVs with different format headers and we'll catch the mismatch
# explicitly in :func:`musahit.tts.encoder.concatenate_wavs`.
TICK_SAMPLE_RATE: int = 22050
TICK_SAMPLE_WIDTH_BYTES: int = 2  # 16-bit signed
TICK_CHANNELS: int = 1  # mono
TICK_DURATION_SECONDS: float = 0.2
TICK_FREQUENCY_HZ: float = 80.0
# 16-bit signed integers span [-32768, 32767]. Cap amplitude at ~25 %
# so the tone is audible but doesn't dominate the speech that follows.
TICK_AMPLITUDE: int = 8000

_TICK_CACHE: bytes | None = None


def generate_tick_tone() -> bytes:
    """Return the WAV bytes for the 200 ms, 80 Hz transition tick.

    First call generates and caches; subsequent calls return the same
    buffer. The function is intentionally cacheable across processes
    that use it (test runs touch it once and reuse).
    """
    global _TICK_CACHE
    if _TICK_CACHE is None:
        _TICK_CACHE = _build_tick_wav()
    return _TICK_CACHE


def reset_cache() -> None:
    """Drop the cached tick — for tests that exercise first-call paths."""
    global _TICK_CACHE
    _TICK_CACHE = None


def _build_tick_wav() -> bytes:
    """Generate the silent-prefixed sine-wave tick as a WAV byte string.

    A pure sine wave that starts at zero and ends at zero crossing
    avoids the click artefact you get from a hard cut. The duration is
    chosen so the wave completes an integer number of cycles within
    200 ms at 80 Hz (200 ms × 80 Hz = 16 cycles).
    """
    n_samples = int(TICK_SAMPLE_RATE * TICK_DURATION_SECONDS)
    # Apply a 5 ms linear fade-in and fade-out envelope to soften the
    # attack / release. Without this the listener hears a faint click.
    fade_samples = int(TICK_SAMPLE_RATE * 0.005)
    frames = bytearray()
    for i in range(n_samples):
        envelope = _envelope(i, n_samples, fade_samples)
        value = TICK_AMPLITUDE * envelope * math.sin(
            2.0 * math.pi * TICK_FREQUENCY_HZ * (i / TICK_SAMPLE_RATE)
        )
        frames.extend(struct.pack("<h", _clip16(int(value))))

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(TICK_CHANNELS)
        w.setsampwidth(TICK_SAMPLE_WIDTH_BYTES)
        w.setframerate(TICK_SAMPLE_RATE)
        w.writeframes(bytes(frames))
    return buf.getvalue()


def _envelope(i: int, n: int, fade: int) -> float:
    """Linear attack-decay envelope; 1.0 in the sustained middle."""
    if i < fade:
        return i / fade
    if i >= n - fade:
        return max(0.0, (n - i) / fade)
    return 1.0


def _clip16(v: int) -> int:
    """Clamp to the 16-bit signed range."""
    if v > 32767:
        return 32767
    if v < -32768:
        return -32768
    return v


__all__ = [
    "TICK_AMPLITUDE",
    "TICK_CHANNELS",
    "TICK_DURATION_SECONDS",
    "TICK_FREQUENCY_HZ",
    "TICK_SAMPLE_RATE",
    "TICK_SAMPLE_WIDTH_BYTES",
    "generate_tick_tone",
    "reset_cache",
]
