"""Integration tests for musahit.tts.synthesizer.Synthesizer.

These tests inject :class:`FakePiper` via the synthesiser's constructor
so PiperVoice is never loaded. The MP3 encoder is injected too · its
default uses :func:`musahit.tts.encoder.wav_to_mp3` which requires
ffmpeg in PATH; the test rig substitutes a tiny fake that returns a
known byte string so the synthesiser orchestration is exercised on
machines without ffmpeg.

The synthesiser MUST always produce *some* ``briefing.mp3`` per
ADR-012 § Stage 7 TTS · even when Piper crashes, the briefings table
is missing data, or the MP3 encoder fails. Each test pins a specific
failure mode to verify the always-ships invariant.
"""

from __future__ import annotations

import io
import json
import wave
from collections.abc import Generator
from datetime import datetime
from pathlib import Path

import duckdb
import pytest

from musahit.common.migrations import init_db
from musahit.ingest.sources import seed_sources
from musahit.tts.piper import FailingPiper, FakePiper
from musahit.tts.synthesizer import (
    Synthesizer,
    silent_placeholder_mp3,
)

RUN_ID = "run_tts_test"
NOW = datetime(2026, 5, 23, 8, 0, 0)
TODAY = NOW.date()


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture()
def db(tmp_path: Path) -> Generator[duckdb.DuckDBPyConnection, None, None]:
    db_path = tmp_path / "x.duckdb"
    init_db(db_path, load_vss=False)
    conn = duckdb.connect(str(db_path))
    seed_sources(conn)
    conn.execute(
        "INSERT INTO pipeline_runs (run_id, started_at, status, stages_done, counts) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            RUN_ID,
            NOW,
            "RUNNING",
            json.dumps(["ingest", "normalize", "cluster", "score", "arc-link", "write"]),
            json.dumps({}),
        ],
    )
    yield conn
    conn.close()


def _seed_briefing(
    conn: duckdb.DuckDBPyConnection,
    briefings_root: Path,
    *,
    body: str | None = None,
) -> Path:
    """Write a briefing.md and seed the briefings row pointing at it."""
    md_path = (
        briefings_root
        / f"{TODAY.year:04d}"
        / f"{TODAY.month:02d}"
        / f"{TODAY.day:02d}"
        / "briefing.md"
    )
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(body or _default_briefing_md(), encoding="utf-8")
    html_path = md_path.with_name("briefing.html")
    conn.execute(
        "INSERT INTO briefings (date, generated_at, markdown_path, html_path, "
        "audio_path, peak_defcon, cluster_count, arc_count, open_arc_count) "
        "VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)",
        [TODAY, NOW, str(md_path), str(html_path), 3, 1, 1, 1],
    )
    return md_path


def _default_briefing_md() -> str:
    return "\n".join(
        [
            "# MÜŞAHİT · GÜNLÜK BRİF",
            "",
            "**Tarih** · 23 Mayıs 2026 · Cumartesi",
            "**Zirve DEFCON** · 2",
            "",
            "---",
            "",
            "## ❯ DEFCON 1-2 · ÖNCELİKLİ",
            "",
            "### Önemli olay",
            "TCMB ve BDDK açıklama yaptı.",
            "",
            "---",
            "",
            "## ❯ DEFCON 3 · MATERYAL",
            "",
            "### POLİTİKA",
            "#### İkinci olay",
            "Olay özeti.",
            "**Kaynaklar** · sabah·gov_aligned",
            "",
            "---",
            "",
            "## ❯ AÇIK GELİŞMELER · DEVAM EDEN TAKİP",
            "",
            "### Devam eden hikaye",
            "Bugünkü güncelleme.",
            "",
            "---",
            "",
            "## ❯ DEFCON 4 · GÜNDEM",
            "",
            "- Önemsiz olay",
            "",
            "---",
            "",
            "## ❯ DİKKAT · YALNIZCA SOSYALDE",
            "",
            "Yok.",
            "",
            "---",
            "",
            "## ❯ AMBİYANS · DEFCON 5",
            "",
            "10 başlık ambiyans.",
            "",
            "---",
            "",
            "## ❯ KAPATILAN HİKAYELER",
            "",
            "Yok.",
            "",
            "---",
            "",
            "## ❯ SİSTEM LOG",
            "",
            "**Runtime** · 5h",
        ]
    )


def _fake_mp3_encoder(wav_bytes: bytes) -> bytes:
    """Stand-in for ``wav_to_mp3`` · returns a marker byte string."""
    return b"FAKE_MP3:" + wav_bytes[:8]


# ── Happy path ─────────────────────────────────────────────────────────────


class TestHappyPath:
    async def test_writes_mp3_and_updates_audio_path(
        self, db: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        root = tmp_path / "briefings"
        _seed_briefing(db, root)

        piper = FakePiper()
        synth = Synthesizer(db, piper, root, mp3_encoder=_fake_mp3_encoder)
        result = await synth.run(RUN_ID)

        assert result["used_placeholder"] is False
        assert result["chunks"] >= 3  # header, priority, material, open arcs, closing
        mp3_path = Path(result["mp3_path"])
        assert mp3_path.name == "briefing.mp3"
        assert mp3_path.exists()
        assert mp3_path.read_bytes().startswith(b"FAKE_MP3:")
        # briefings.audio_path is now set.
        row = db.execute(
            "SELECT audio_path FROM briefings WHERE date = ?", [TODAY]
        ).fetchone()
        assert row[0] == str(mp3_path)

    async def test_piper_called_per_chunk(
        self, db: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        root = tmp_path / "briefings"
        _seed_briefing(db, root)
        piper = FakePiper()
        synth = Synthesizer(db, piper, root, mp3_encoder=_fake_mp3_encoder)
        await synth.run(RUN_ID)
        # Header + DEFCON 1-2 + DEFCON 3 + open arcs + closing → at least 4 chunks.
        # The closing line is the literal CLOSING_LINE constant.
        assert piper.call_count >= 4
        # The header line "**Zirve DEFCON** · 2" gets respelled to
        # "Zirve Defkon İki" by the preprocessor (DEFCON → Defkon
        # plus numeral expansion; the regex now matches the ``·``
        # separator path). Asserting on the post-respelling form
        # makes the test pin both the chunking AND the respelling.
        assert any("Zirve Defkon İki" in c for c in piper.calls)
        assert any("Önemli olay" in c for c in piper.calls)
        assert any("dashboard" in c for c in piper.calls)

    async def test_stages_done_appends_tts(
        self, db: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        root = tmp_path / "briefings"
        _seed_briefing(db, root)
        synth = Synthesizer(
            db, FakePiper(), root, mp3_encoder=_fake_mp3_encoder
        )
        await synth.run(RUN_ID)

        row = db.execute(
            "SELECT stages_done, counts FROM pipeline_runs WHERE run_id = ?",
            [RUN_ID],
        ).fetchone()
        stages = json.loads(row[0])
        counts = json.loads(row[1])
        assert "tts" in stages
        assert stages[-1] == "tts"
        assert counts.get("tts_used_placeholder") is False


# ── Failure paths ──────────────────────────────────────────────────────────


class TestPiperFailure:
    async def test_piper_crash_falls_through_to_placeholder(
        self, db: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        root = tmp_path / "briefings"
        _seed_briefing(db, root)
        synth = Synthesizer(
            db, FailingPiper(), root, mp3_encoder=_fake_mp3_encoder
        )
        result = await synth.run(RUN_ID)

        assert result["used_placeholder"] is True
        assert result["reason"]  # non-empty
        mp3_path = Path(result["mp3_path"])
        assert mp3_path.exists()
        # The placeholder is a real (silent) WAV bytestring.
        assert mp3_path.read_bytes() == silent_placeholder_mp3()
        # audio_path still updated.
        row = db.execute(
            "SELECT audio_path FROM briefings WHERE date = ?", [TODAY]
        ).fetchone()
        assert row[0] == str(mp3_path)

    async def test_piper_crash_prints_traceback_to_stderr(
        self,
        db: duckdb.DuckDBPyConnection,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The exception path MUST surface the underlying error to
        stderr · diagnostic visibility for manual / smoke-test runs
        where configure_logging() hasn't been called. The structured
        log call still fires; the placeholder MP3 still writes; the
        traceback just adds an extra signal that points at the actual
        problem.
        """
        root = tmp_path / "briefings"
        _seed_briefing(db, root)
        synth = Synthesizer(
            db,
            FailingPiper(),
            root,
            mp3_encoder=_fake_mp3_encoder,
        )
        result = await synth.run(RUN_ID)

        captured = capsys.readouterr()
        # Traceback header line includes the file/function path.
        assert "Traceback" in captured.err
        # The original exception type + message appears in the trace.
        assert "RuntimeError" in captured.err
        assert "simulated piper failure" in captured.err

        # Existing behaviour is preserved: placeholder written, audio
        # path updated, used_placeholder flag set.
        assert result["used_placeholder"] is True
        mp3_path = Path(result["mp3_path"])
        assert mp3_path.read_bytes() == silent_placeholder_mp3()

    async def test_piper_crash_marks_counts(
        self, db: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        root = tmp_path / "briefings"
        _seed_briefing(db, root)
        synth = Synthesizer(
            db, FailingPiper(), root, mp3_encoder=_fake_mp3_encoder
        )
        await synth.run(RUN_ID)
        row = db.execute(
            "SELECT counts FROM pipeline_runs WHERE run_id = ?",
            [RUN_ID],
        ).fetchone()
        assert json.loads(row[0])["tts_used_placeholder"] is True


class TestEncoderFailure:
    async def test_mp3_encoder_exception_falls_through(
        self, db: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        root = tmp_path / "briefings"
        _seed_briefing(db, root)

        def failing_encoder(_wav: bytes) -> bytes:
            raise RuntimeError("ffmpeg missing")

        synth = Synthesizer(db, FakePiper(), root, mp3_encoder=failing_encoder)
        result = await synth.run(RUN_ID)
        assert result["used_placeholder"] is True
        mp3_path = Path(result["mp3_path"])
        assert mp3_path.read_bytes() == silent_placeholder_mp3()


class TestMissingBriefingRow:
    async def test_no_briefings_row_returns_early(
        self, db: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        # No _seed_briefing call → empty briefings table.
        root = tmp_path / "briefings"
        synth = Synthesizer(db, FakePiper(), root, mp3_encoder=_fake_mp3_encoder)
        result = await synth.run(RUN_ID)
        assert result["used_placeholder"] is True
        assert result["reason"] == "no_briefing_row"


# ── Idempotence ────────────────────────────────────────────────────────────


class TestIdempotence:
    async def test_second_run_overwrites_mp3(
        self, db: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        root = tmp_path / "briefings"
        _seed_briefing(db, root)
        synth = Synthesizer(db, FakePiper(), root, mp3_encoder=_fake_mp3_encoder)
        first = await synth.run(RUN_ID)
        second = await synth.run(RUN_ID)
        # Same path each time; file gets overwritten.
        assert first["mp3_path"] == second["mp3_path"]
        # briefings row count stays at one.
        n = db.execute(
            "SELECT COUNT(*) FROM briefings WHERE date = ?", [TODAY]
        ).fetchone()[0]
        assert n == 1


# ── Placeholder constant ───────────────────────────────────────────────────


class TestSilentPlaceholder:
    def test_silent_placeholder_is_valid_wav(self) -> None:
        bytes_ = silent_placeholder_mp3()
        assert bytes_.startswith(b"RIFF")
        with wave.open(io.BytesIO(bytes_), "rb") as w:
            assert w.getnchannels() == 1
            assert w.getsampwidth() == 2
            assert w.getframerate() == 22050
            # ~1 second of audio.
            duration = w.getnframes() / w.getframerate()
            assert abs(duration - 1.0) < 0.01

    def test_silent_placeholder_is_deterministic(self) -> None:
        assert silent_placeholder_mp3() == silent_placeholder_mp3()


# ── Regression: per-chunk resilience (2026-05-24 silent-MP3 fix) ───────────


_SENTINEL_FAIL = "FAIL_THIS_CHUNK_PLEASE"


class _SentinelFailingPiper:
    """Piper stand-in that fails only on chunks containing a sentinel.

    Chunks without the sentinel return a valid silent WAV (same shape
    as :class:`FakePiper`). Used to exercise the per-chunk try/except
    added on 2026-05-24 · one bad chunk in the middle of a real
    briefing must not poison the rest of the synthesis.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.failed_calls: list[int] = []
        self.succeeded_calls: list[int] = []

    async def synthesize(self, text: str) -> bytes:
        idx = len(self.calls)
        self.calls.append(text)
        if _SENTINEL_FAIL in text:
            self.failed_calls.append(idx)
            raise RuntimeError(
                f"simulated per-chunk failure on chunk {idx} ({_SENTINEL_FAIL})"
            )
        self.succeeded_calls.append(idx)
        # 128 silent samples · enough to concatenate, small to keep tests fast.
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(22050)
            w.writeframes(b"\x00\x00" * 128)
        return buf.getvalue()


def _briefing_with_failing_chunk() -> str:
    """Briefing where the AÇIK GELİŞMELER section contains the
    failure sentinel · the synthesiser should skip that chunk and
    still produce a real MP3 from the other chunks."""
    return "\n".join(
        [
            "# MÜŞAHİT · GÜNLÜK BRİF",
            "",
            "**Tarih** · 24 Mayıs 2026",
            "**Zirve DEFCON** · 3",
            "",
            "---",
            "",
            "## ❯ DEFCON 1-2 · ÖNCELİKLİ",
            "",
            "### Önemli olay",
            "Güvenli içerik · sentezlenebilir.",
            "",
            "---",
            "",
            "## ❯ AÇIK GELİŞMELER · DEVAM EDEN TAKİP",
            "",
            f"### Sorunlu arc · {_SENTINEL_FAIL}",
            f"Bu chunk sentinel içeriyor: {_SENTINEL_FAIL}",
            "",
            "---",
            "",
            "## ❯ DEFCON 4 · GÜNDEM",
            "",
            "- Voiced değil · zaten skipped.",
        ]
    )


class TestPerChunkResilience:
    async def test_one_failing_chunk_does_not_fail_whole_stage(
        self, db: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        """One sentinel-bearing chunk skipped · remaining chunks
        succeed · real MP3 written (no placeholder). This is the
        regression for the 2026-05-23 silent-MP3 bug · one oversized
        AÇIK GELİŞMELER chunk used to fail the whole stage."""
        root = tmp_path / "briefings"
        _seed_briefing(db, root, body=_briefing_with_failing_chunk())

        piper = _SentinelFailingPiper()
        synth = Synthesizer(db, piper, root, mp3_encoder=_fake_mp3_encoder)
        result = await synth.run(RUN_ID)

        assert result["used_placeholder"] is False
        assert len(piper.failed_calls) == 1
        assert len(piper.succeeded_calls) >= 2  # at least header + DEFCON 1-2 + closing
        mp3_path = Path(result["mp3_path"])
        assert mp3_path.read_bytes().startswith(b"FAKE_MP3:")

    async def test_failing_piper_called_once_per_chunk_before_raising(
        self, db: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        """The per-chunk wrapping must attempt every chunk before
        giving up · proving the synthesiser does NOT bail on first
        failure. Combined with the existing
        ``test_piper_crash_falls_through_to_placeholder`` (which
        pins the all-fail → placeholder contract), this nails down
        the per-chunk loop's shape."""
        root = tmp_path / "briefings"
        _seed_briefing(db, root)

        piper = FailingPiper()
        synth = Synthesizer(db, piper, root, mp3_encoder=_fake_mp3_encoder)
        result = await synth.run(RUN_ID)

        # Placeholder still fires (all chunks failed) · existing
        # behaviour is preserved.
        assert result["used_placeholder"] is True
        # But Piper got every chunk · NOT just the first one. The
        # default seeded briefing has at least 4 voiced chunks
        # (header, DEFCON 1-2, DEFCON 3, open arcs, closing).
        assert piper.call_count >= 4
