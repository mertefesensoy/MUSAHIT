"""Hand-crafted Resmî Gazete fixture generator.

Run **once** to (re)build the fixture PDFs in this directory; commit the
results. The runtime test suite does not import this module and the
project's runtime dependencies do not include :mod:`reportlab` — this
script is a developer tool, kept here so future regeneration is local.

Usage::

    pip install reportlab    # one-time
    python tests/fixtures/resmi_gazete/_generate.py
"""

from __future__ import annotations

from pathlib import Path

from reportlab.lib.pagesizes import A4  # type: ignore[import-not-found]
from reportlab.pdfbase import pdfmetrics  # type: ignore[import-not-found]
from reportlab.pdfbase.ttfonts import TTFont  # type: ignore[import-not-found]
from reportlab.pdfgen import canvas  # type: ignore[import-not-found]

FIXTURE_DIR = Path(__file__).resolve().parent

# Use a Turkish-capable font. Arial ships with Windows; fall back to DejaVu
# on Linux/macOS if needed. The fixture only needs to render the few
# Turkish glyphs the parser searches for.
_FONT_CANDIDATES = (
    "C:/Windows/Fonts/arial.ttf",
    "/Library/Fonts/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def _register_font() -> str:
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            pdfmetrics.registerFont(TTFont("Turkish", path))
            return "Turkish"
    raise RuntimeError(
        "No Turkish-capable TTF found in the standard locations. "
        "Edit _FONT_CANDIDATES with a path that exists on this machine."
    )


# Each tuple = (filename, list of page-text blocks). Text uses raw Unicode.
FIXTURES: tuple[tuple[str, list[str]], ...] = (
    (
        "sample_gazette.pdf",
        [
            # Page 1 — Executive: a law and a presidential decree.
            "\n".join(
                [
                    "                YÜRÜTME VE İDARE BÖLÜMÜ",
                    "",
                    "                          KANUN",
                    "",
                    "       Test Kanunu Hakkında Kanun",
                    "",
                    "Kanun No: 7460        Kabul Tarihi: 23.05.2026",
                    "",
                    "Madde 1 - Bu kanun test amaçlıdır.",
                    "Madde 2 - Yürürlüğe girer.",
                    "",
                    "",
                    "                CUMHURBAŞKANLIĞI KARARNAMESİ",
                    "",
                    "       Yeni Daire Başkanlıkları Hakkında Kararname",
                    "",
                    "Karar Sayısı: 152",
                    "",
                    "Madde 1 - Test kararname içeriği.",
                ]
            ),
            # Page 2 — Judicial + Announcement.
            "\n".join(
                [
                    "                  YARGI BÖLÜMÜ",
                    "",
                    "                  MAHKEME KARARI",
                    "",
                    "         Anayasa Mahkemesi Kararı",
                    "",
                    "Esas No: 2026/45      Karar No: 2026/123",
                    "",
                    "İptal kararı gerekçesi...",
                    "",
                    "",
                    "                  İLAN BÖLÜMÜ",
                    "",
                    "                  TEBLİĞ",
                    "",
                    "       Test Tebliği Hakkında",
                    "",
                    "Tebliğ No: 2026/89",
                    "",
                    "Bu tebliğ test amaçlıdır.",
                ]
            ),
        ],
    ),
    (
        "mukerrer_supplement.pdf",
        [
            "\n".join(
                [
                    "                YÜRÜTME VE İDARE BÖLÜMÜ",
                    "",
                    "                          YÖNETMELİK",
                    "",
                    "       Acil Yönetmelik Hakkında",
                    "",
                    "Yönetmelik No: 2026/7",
                    "",
                    "Madde 1 - Test yönetmelik.",
                ]
            ),
        ],
    ),
)


def _draw_page(c: canvas.Canvas, font_name: str, page_text: str) -> None:
    c.setFont(font_name, 11)
    y = 800
    for line in page_text.split("\n"):
        c.drawString(50, y, line)
        y -= 14


def build_all() -> None:
    font_name = _register_font()
    for filename, page_texts in FIXTURES:
        out_path = FIXTURE_DIR / filename
        c = canvas.Canvas(str(out_path), pagesize=A4)
        for page_text in page_texts:
            _draw_page(c, font_name, page_text)
            c.showPage()
        c.save()
    # Corrupted-bytes fixture: anything pdfplumber rejects.
    corrupted = FIXTURE_DIR / "corrupted.bin"
    corrupted.write_bytes(b"%PDF-this is not a valid PDF file at all\x00garbage")


if __name__ == "__main__":
    build_all()
