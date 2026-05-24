"""TTS orchestrator: briefing.md → briefing.mp3.

The Synthesizer reads today's briefing markdown, extracts the ADR-009
voiced scope, preprocesses Turkish text, drives :class:`PiperClient`
once per section chunk, interleaves a transition tick tone between
chunks, encodes the concatenated WAV to MP3, and writes the result
alongside the markdown briefing.

Per ADR-012 § Stage 7 TTS the stage must NEVER abort the pipeline.
Any failure during synthesis (Piper crash, ffmpeg missing, malformed
input) is logged and a silent 1-second placeholder MP3 is written
instead · the dashboard's audio element still loads, just plays
silence. The operator sees the failure in ``pipeline_runs.counts`` and
in the briefing's SİSTEM LOG footer.

The orchestrator is idempotent for a given date: re-running on the
same date updates ``briefings.audio_path`` and overwrites
``briefing.mp3``. This mirrors the writer's per-date idempotence.
"""

from __future__ import annotations

import io
import json
import sys
import traceback
import wave
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import duckdb

from musahit.common.logging import get_logger
from musahit.tts.encoder import concatenate_wavs, wav_to_mp3
from musahit.tts.extractor import VoicedBriefing, extract_voiced_briefing
from musahit.tts.piper import PiperClient
from musahit.tts.preprocessor import preprocess_for_tts
from musahit.tts.transitions import generate_tick_tone

_log = get_logger("musahit.tts")

DEFAULT_BRIEFINGS_ROOT: Path = Path("briefings")

# Type alias for the WAV→MP3 callable so the synthesiser can accept a
# test-injectable encoder (e.g. fake bytes) without breaking the
# constructor signature contract (db, piper, briefings_root).
Mp3Encoder = Callable[[bytes], bytes]


class Synthesizer:
    """Drive the briefing → MP3 pipeline for one nightly run."""

    def __init__(
        self,
        conn: duckdb.DuckDBPyConnection,
        piper: PiperClient,
        briefings_root: Path = DEFAULT_BRIEFINGS_ROOT,
        *,
        mp3_encoder: Mp3Encoder | None = None,
    ) -> None:
        self._conn = conn
        self._piper = piper
        self._root = Path(briefings_root)
        # Optional injection point: tests pass a fake encoder so the
        # happy-path test doesn't require ffmpeg. Production uses the
        # real :func:`wav_to_mp3` from :mod:`musahit.tts.encoder`.
        self._mp3_encoder: Mp3Encoder = mp3_encoder or wav_to_mp3

    async def run(self, run_id: str) -> dict[str, Any]:
        log = _log.bind(run_id=run_id)
        target_date = self._resolve_briefing_date(run_id)
        if target_date is None:
            log.warning("tts_no_briefing_row", run_id=run_id)
            return {"used_placeholder": True, "reason": "no_briefing_row"}

        markdown_path = self._read_markdown_path(target_date)
        used_placeholder = False
        chunks_synthesised = 0
        reason: str | None = None

        try:
            briefing_md = Path(markdown_path).read_text(encoding="utf-8")
            voiced = extract_voiced_briefing(briefing_md)
            chunks = self._preprocessed_chunks(voiced)
            if not chunks:
                # An entirely empty voiced briefing is itself a soft
                # failure mode · fall through to the placeholder.
                raise ValueError("voiced briefing produced no chunks")
            wavs = await self._synthesise_chunks(chunks, log=log)
            chunks_synthesised = len(chunks)
            combined_wav = concatenate_wavs(self._interleave_with_ticks(wavs))
            mp3_bytes = self._mp3_encoder(combined_wav)
        except Exception as exc:
            # Print the full traceback to stderr so manual invocations
            # (smoke tests, ad-hoc operator reruns) surface the root
            # cause even when configure_logging() has not been called.
            # The structured log call below still fires for production
            # runs where the JSON log pipeline is wired up.
            traceback.print_exc(file=sys.stderr)
            log.warning(
                "tts_failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            mp3_bytes = silent_placeholder_mp3()
            used_placeholder = True
            reason = f"{type(exc).__name__}: {exc}"

        audio_path = self._write_mp3(target_date, mp3_bytes)
        self._update_audio_path(target_date, audio_path)
        self._mark_stage_done(run_id, used_placeholder=used_placeholder)

        log.info(
            "tts_done",
            used_placeholder=used_placeholder,
            chunks=chunks_synthesised,
            mp3_path=str(audio_path),
        )
        return {
            "used_placeholder": used_placeholder,
            "chunks": chunks_synthesised,
            "mp3_path": str(audio_path),
            "reason": reason,
        }

    # ── Chunk pipeline ──────────────────────────────────────────────────

    def _preprocessed_chunks(self, voiced: VoicedBriefing) -> list[str]:
        """Pull voiced chunks, preprocess, drop empties."""
        out: list[str] = []
        for chunk in voiced.chunks():
            cleaned = preprocess_for_tts(chunk).strip()
            if cleaned:
                out.append(cleaned)
        return out

    async def _synthesise_chunks(
        self, chunks: list[str], *, log: Any
    ) -> list[bytes]:
        """Synthesise each chunk via Piper · per-chunk failures are skipped.

        Per the 2026-05-24 fix following the smoke-run silent-MP3 bug:
        one oversized chunk (e.g. an enormous AÇIK GELİŞMELER section)
        used to fail the whole stage via Piper timeout. Now each chunk
        is wrapped individually · a single chunk's failure produces a
        ``tts_chunk_failed`` warning and the next chunk is still tried.
        If every chunk fails the function raises ``ValueError`` so the
        outer try/except in :meth:`run` still falls through to the
        silent placeholder path (ADR-012 always-ships invariant).

        The all-fail ``ValueError`` is raised with ``from last_exc`` so
        the original exception (e.g. Piper timeout) is preserved in the
        traceback chain. This keeps the stderr-traceback diagnostic
        path (see ``test_piper_crash_prints_traceback_to_stderr``)
        useful for manual / smoke-test runs.
        """
        out: list[bytes] = []
        last_exc: Exception | None = None
        for i, chunk in enumerate(chunks):
            log.debug("tts_chunk_start", index=i, chars=len(chunk))
            try:
                wav = await self._piper.synthesize(chunk)
            except Exception as exc:
                log.warning(
                    "tts_chunk_failed",
                    index=i,
                    chars=len(chunk),
                    error=f"{type(exc).__name__}: {exc}",
                )
                last_exc = exc
                continue
            out.append(wav)
        if not out:
            raise ValueError("all chunks failed synthesis") from last_exc
        return out

    def _interleave_with_ticks(self, wavs: list[bytes]) -> list[bytes]:
        """Place a transition tone between consecutive speech WAVs.

        Pattern: [speech_0, tick, speech_1, tick, …, speech_N]. No
        leading or trailing tick · those would feel like the briefing
        starts and ends with a clipped sound rather than speech.
        """
        if len(wavs) <= 1:
            return list(wavs)
        tick = generate_tick_tone()
        out: list[bytes] = []
        for i, wav in enumerate(wavs):
            if i > 0:
                out.append(tick)
            out.append(wav)
        return out

    # ── Persistence ─────────────────────────────────────────────────────

    def _resolve_briefing_date(self, run_id: str) -> date | None:
        """Find the briefing row associated with this run.

        We use the most recent briefing whose ``markdown_path`` exists ·
        the writer just wrote it, and in the nominal case there's
        exactly one row keyed by today's date. If a test or operator
        rerun produced multiple, the most recent one wins.
        """
        row = self._conn.execute(
            "SELECT date FROM briefings ORDER BY generated_at DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        # DuckDB returns date as a Python date. Type the call site.
        d = row[0]
        return d if isinstance(d, date) else None

    def _read_markdown_path(self, target_date: date) -> str:
        row = self._conn.execute(
            "SELECT markdown_path FROM briefings WHERE date = ?",
            [target_date],
        ).fetchone()
        if row is None or row[0] is None:
            raise FileNotFoundError(
                f"briefings row for {target_date.isoformat()} has no "
                f"markdown_path"
            )
        return str(row[0])

    def _write_mp3(self, target_date: date, mp3_bytes: bytes) -> Path:
        directory = (
            self._root
            / f"{target_date.year:04d}"
            / f"{target_date.month:02d}"
            / f"{target_date.day:02d}"
        )
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "briefing.mp3"
        path.write_bytes(mp3_bytes)
        return path

    def _update_audio_path(self, target_date: date, audio_path: Path) -> None:
        self._conn.execute(
            "UPDATE briefings SET audio_path = ? WHERE date = ?",
            [str(audio_path), target_date],
        )

    def _mark_stage_done(self, run_id: str, *, used_placeholder: bool) -> None:
        row = self._conn.execute(
            "SELECT stages_done, counts FROM pipeline_runs WHERE run_id = ?",
            [run_id],
        ).fetchone()
        if row is None:
            # Unusual but not fatal · the synthesiser ran without a
            # pipeline_runs row. Log silently; the briefing.mp3 is
            # written regardless.
            return
        stages = json.loads(row[0]) if row[0] else []
        counts: dict[str, Any] = json.loads(row[1]) if row[1] else {}
        if "tts" not in stages:
            stages.append("tts")
        counts["tts_used_placeholder"] = used_placeholder
        self._conn.execute(
            "UPDATE pipeline_runs SET stages_done = ?, counts = ? WHERE run_id = ?",
            [json.dumps(stages), json.dumps(counts), run_id],
        )


# ── Placeholder MP3 ────────────────────────────────────────────────────────


def silent_placeholder_mp3() -> bytes:
    """Return the bytes for the 1-second silent placeholder audio.

    Pure stdlib (``wave``): the bytes are a valid WAV (22050 Hz / 16-bit
    / mono, ~22 050 zero-valued frames). Written into ``briefing.mp3``
    when synthesis fails · HTML5 ``<audio>`` content-sniffs and plays
    these bytes as a one-second silent track regardless of the file
    extension, satisfying the ADR-012 "briefing always ships"
    discipline without dragging the ffmpeg dependency into the failure
    path. When ffmpeg+pydub are healthy the normal :func:`wav_to_mp3`
    path produces a true MP3; this fallback only fires when that path
    has already broken.
    """
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(b"\x00\x00" * 22050)
    return buf.getvalue()


__all__ = [
    "DEFAULT_BRIEFINGS_ROOT",
    "Mp3Encoder",
    "Synthesizer",
    "silent_placeholder_mp3",
]
