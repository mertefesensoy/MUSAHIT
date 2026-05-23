"""WAV concatenation and WAV → MP3 encoding.

:func:`concatenate_wavs` is pure stdlib (``wave``) — it reads each
input's header, validates that the format matches across all inputs,
and writes a single output WAV with the combined frame data. No
resampling: the Piper voice and the tick tone share format
(22050 Hz / 16-bit / mono) by design.

:func:`wav_to_mp3` uses :mod:`pydub` to drive ffmpeg under the hood.
This is the one part of the TTS stage that has an external system
dependency (ffmpeg in PATH). On machines without ffmpeg the function
raises; the synthesiser catches and writes the silent-placeholder MP3
instead, so the pipeline always produces *some* ``briefing.mp3``.
"""

from __future__ import annotations

import io
import shutil
import warnings
import wave

# Pydub is installed as a project dependency; module-level import is
# fine. The pydub package itself imports lazily — it doesn't probe for
# ffmpeg until the first encode call.
from pydub import AudioSegment

MP3_BITRATE: str = "128k"
MP3_CHANNELS: int = 1  # mono per ADR-010 § Pipeline integration step 7

_FFMPEG_MISSING_MSG: str = (
    "ffmpeg not found on PATH · install via winget install ffmpeg on "
    "Windows or apt install ffmpeg on Linux"
)


def check_ffmpeg_available() -> None:
    """Raise :class:`RuntimeError` if ffmpeg cannot be found on PATH.

    Pydub's ``export(format="mp3")`` shells out to ffmpeg without a
    pre-flight check; on a machine without ffmpeg the failure surfaces
    as :class:`FileNotFoundError` with ``[WinError 2]`` deep inside the
    subprocess launch — opaque to the operator reading a pipeline log.
    This function is the explicit pre-flight check so the diagnostic
    points at the actual environment gap.
    """
    if shutil.which("ffmpeg") is None:
        raise RuntimeError(_FFMPEG_MISSING_MSG)


# Module-load diagnostic. The warning fires once per process when the
# module is imported on a machine without ffmpeg. Import must still
# succeed — unit tests for the extractor, preprocessor, transitions,
# and Piper Protocol don't actually encode MP3, and ffmpeg is an
# operator-side install rather than a hard test prerequisite. The
# strict raise lives in :func:`wav_to_mp3` itself.
try:
    check_ffmpeg_available()
except RuntimeError as _exc:  # pragma: no cover — diagnostic surface
    warnings.warn(str(_exc), UserWarning, stacklevel=2)


def concatenate_wavs(wavs: list[bytes]) -> bytes:
    """Concatenate WAV byte strings into a single WAV.

    All inputs must share sample rate, sample width, and channel count.
    A mismatch raises :class:`ValueError` rather than silently
    resampling — the TTS pipeline guarantees the Piper voice and the
    tick tone share format, so a mismatch indicates a bug upstream
    that should surface immediately.

    Returns an empty WAV header (no frames) when the input list is
    empty — this keeps the call-site simple at the cost of one
    "empty briefing" edge case writing a zero-frame audio file.
    """
    if not wavs:
        return _empty_wav()

    framerate: int | None = None
    sampwidth: int | None = None
    nchannels: int | None = None
    frames_chunks: list[bytes] = []
    for i, wav_bytes in enumerate(wavs):
        with wave.open(io.BytesIO(wav_bytes), "rb") as w:
            if framerate is None:
                framerate = w.getframerate()
                sampwidth = w.getsampwidth()
                nchannels = w.getnchannels()
            else:
                if (
                    w.getframerate() != framerate
                    or w.getsampwidth() != sampwidth
                    or w.getnchannels() != nchannels
                ):
                    raise ValueError(
                        f"WAV format mismatch at input index {i}: "
                        f"expected {framerate}Hz/{sampwidth}b/{nchannels}ch "
                        f"got {w.getframerate()}Hz/{w.getsampwidth()}b/"
                        f"{w.getnchannels()}ch"
                    )
            frames_chunks.append(w.readframes(w.getnframes()))

    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        assert framerate is not None  # appeases mypy; guaranteed by the loop
        assert sampwidth is not None
        assert nchannels is not None
        out.setframerate(framerate)
        out.setsampwidth(sampwidth)
        out.setnchannels(nchannels)
        out.writeframes(b"".join(frames_chunks))
    return buf.getvalue()


def wav_to_mp3(wav_bytes: bytes) -> bytes:
    """Encode WAV bytes to a 128 kbps mono MP3 via pydub.

    Raises :class:`RuntimeError` (via :func:`check_ffmpeg_available`)
    when ffmpeg is not on PATH — the strict pre-flight check that
    surfaces the environment gap before pydub's opaque subprocess
    failure. Any other pydub / ffmpeg encoding error also propagates.
    The synthesiser catches the broad ``Exception`` and falls back to
    the silent placeholder per ADR-012 — keeping the briefing always-
    shipping discipline.
    """
    check_ffmpeg_available()
    seg = AudioSegment.from_wav(io.BytesIO(wav_bytes))
    if seg.channels != MP3_CHANNELS:
        seg = seg.set_channels(MP3_CHANNELS)
    out = io.BytesIO()
    seg.export(out, format="mp3", bitrate=MP3_BITRATE)
    return out.getvalue()


# ── Helpers ────────────────────────────────────────────────────────────────


def _empty_wav() -> bytes:
    """Construct a zero-frame WAV with the Piper voice format."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
    return buf.getvalue()


__all__ = [
    "MP3_BITRATE",
    "MP3_CHANNELS",
    "check_ffmpeg_available",
    "concatenate_wavs",
    "wav_to_mp3",
]
