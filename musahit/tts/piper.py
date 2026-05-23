"""Piper TTS client — Protocol + production + fake.

Per ADR-010 amended 2026-05-23 the canonical Piper integration is the
``piper-tts`` Python package (``from piper import PiperVoice``), NOT
subprocess invocation against the archived ``rhasspy/piper`` binary.
The amended ADR's open-questions block explicitly forbids silently
switching back to subprocess.

The :class:`PiperClient` Protocol has one method,
``async synthesize(text) -> bytes`` returning WAV. The pattern is the
same as :class:`musahit.score.llm_client.LlmClient` and
:class:`musahit.cluster.embedder.EmbeddingClient` — production uses a
real client, tests inject :class:`FakePiper` via the synthesiser's
constructor.

PiperVoice is loaded once in :class:`PiperPythonClient.__init__` and
reused across every ``synthesize`` call in a single nightly run; model
load time (~300 ms) only pays once. The synchronous PiperVoice call is
wrapped in :func:`asyncio.to_thread` with a soft 60 s timeout (a
single voiced briefing chunk is rarely > 30 s of speech; 60 s is the
generous upper bound from the operator's hardware budget).
"""

from __future__ import annotations

import asyncio
import io
import wave
from pathlib import Path
from typing import Any, Protocol

DEFAULT_TIMEOUT_SECONDS: float = 60.0


# ── Protocol ───────────────────────────────────────────────────────────────


class PiperClient(Protocol):
    """Async TTS API. Returns the synthesised audio as WAV bytes."""

    async def synthesize(self, text: str) -> bytes: ...


# ── Production implementation ──────────────────────────────────────────────


class PiperPythonClient:
    """Production client wrapping :class:`piper.PiperVoice`.

    The voice ONNX model is loaded once in the constructor. Each
    :meth:`synthesize` call delegates to PiperVoice's ``synthesize_wav``
    method which writes a complete WAV (header + PCM frames) to an
    in-memory buffer; the buffer's contents are returned as ``bytes``.

    PiperVoice's API is synchronous — we run it on a worker thread via
    :func:`asyncio.to_thread` so the pipeline's event loop stays
    responsive (the dashboard's FastAPI server can still serve requests
    while a synthesis is in flight). A 60 s timeout guards against
    pathological inputs that could lock the worker thread.

    The class never imports :mod:`piper` at module load time — the
    import lives inside :meth:`__init__` so the rest of MÜŞAHİT can
    import :mod:`musahit.tts.piper` (e.g., for the Protocol and
    :class:`FakePiper`) on a machine where ``piper-tts`` is not yet
    installed.
    """

    def __init__(
        self,
        voice_path: Path,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        # Lazy import so test environments / harnesses don't require
        # piper-tts to be importable at module collection time.
        from piper import PiperVoice  # noqa: PLC0415

        self._voice_path = Path(voice_path)
        if not self._voice_path.exists():
            raise FileNotFoundError(
                f"Piper voice ONNX not found at {self._voice_path}. "
                f"Run scripts/install_windows.ps1 to download the voice "
                f"or set piper_voice_path in config.toml."
            )
        self._timeout_seconds = timeout_seconds
        # PiperVoice.load is synchronous; called once in __init__.
        self._voice: Any = PiperVoice.load(str(self._voice_path))

    async def synthesize(self, text: str) -> bytes:
        return await asyncio.wait_for(
            asyncio.to_thread(self._synthesize_sync, text),
            timeout=self._timeout_seconds,
        )

    def _synthesize_sync(self, text: str) -> bytes:
        """Drive PiperVoice.synthesize_wav into an in-memory WAV buffer."""
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav_file:
            # synthesize_wav writes the header + PCM frames and sets
            # the format on the first chunk. Returns None unless
            # alignments are requested (we don't).
            self._voice.synthesize_wav(text, wav_file)
        return buf.getvalue()


# ── Fake (testing) implementation ──────────────────────────────────────────


class FakePiper:
    """Deterministic Piper stand-in for tests.

    Returns a valid minimal WAV byte string per call (matching the
    Piper voice format: 22050 Hz, 16-bit, mono) so downstream
    concatenation and MP3-encoding code paths receive structurally
    real audio data. The actual sample content is silence — tests
    don't listen, they only verify byte flow.

    The call log allows assertions about which texts were synthesised
    in what order.
    """

    def __init__(self, *, samples_per_call: int = 2205) -> None:
        # 100 ms of silence per call by default. Long enough to be a
        # non-trivial WAV (header + frames), short enough that
        # concatenation tests stay fast.
        self._samples_per_call = samples_per_call
        self._calls: list[str] = []

    async def synthesize(self, text: str) -> bytes:
        self._calls.append(text)
        return _silent_wav_bytes(self._samples_per_call)

    @property
    def call_count(self) -> int:
        return len(self._calls)

    @property
    def calls(self) -> list[str]:
        return list(self._calls)


class FailingPiper:
    """Piper stand-in that raises on every call — for failure-path tests."""

    def __init__(self, exc: Exception | None = None) -> None:
        self._exc = exc or RuntimeError("simulated piper failure")
        self._calls: list[str] = []

    async def synthesize(self, text: str) -> bytes:
        self._calls.append(text)
        raise self._exc

    @property
    def call_count(self) -> int:
        return len(self._calls)


# ── Helpers ────────────────────────────────────────────────────────────────


def _silent_wav_bytes(n_samples: int) -> bytes:
    """Construct a valid WAV byte string containing ``n_samples`` of silence.

    Format matches the Piper voice (22050 Hz, 16-bit, mono) so callers
    can concatenate these with real Piper output without resampling.
    """
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(b"\x00\x00" * n_samples)
    return buf.getvalue()


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "FailingPiper",
    "FakePiper",
    "PiperClient",
    "PiperPythonClient",
]
