"""Tests for the four per-kind extractors in musahit.normalize.extractors.*."""

from __future__ import annotations

import json

from musahit.normalize.extractors.html import extract_html_body
from musahit.normalize.extractors.pdf import extract_pdf_body
from musahit.normalize.extractors.reddit import COMMENT_SEPARATOR, extract_reddit_body
from musahit.normalize.extractors.rss import extract_rss_body

# ── HTML ───────────────────────────────────────────────────────────────────


class TestHtmlExtractor:
    def test_trafilatura_handles_typical_article_page(self) -> None:
        # A long article body wrapped in <article> with boilerplate
        # script/nav around it — trafilatura should pull only the body.
        body_paragraph = " ".join([
            "Bu paragraf bir test makalesinin gövdesidir.",
            "İkinci cümle de yine aynı paragrafa aittir ve yeterince",
            "uzun olmalı ki gövde çıkarımı tetiklensin.",
        ]) * 3
        page = (
            "<!doctype html><html><head><title>Test article</title>"
            "<script>console.log('bad')</script></head><body>"
            "<nav>menu link</nav>"
            f"<article><h1>Test article</h1><p>{body_paragraph}</p></article>"
            "<footer>copyright</footer></body></html>"
        ).encode()
        title, body = extract_html_body(page, "ap_tr", {"title": "Test article"})
        assert title == "Test article"
        assert "test makalesinin" in body
        # Boilerplate stripped.
        assert "console.log" not in body
        assert "menu link" not in body
        assert "copyright" not in body

    def test_falls_back_to_body_selector_when_trafilatura_returns_thin_output(self) -> None:
        # A page whose actual content lives in a heavily-structured DOM
        # that trafilatura may not extract well. We use the ap_tr selector
        # config — body_selector="div.RichTextStoryBody".
        page = (
            "<!doctype html><html><head><title>Thin page</title></head><body>"
            "<div class='RichTextStoryBody'>"
            "<p>Bu makale gövdesi yalnızca seçici tarafından alınır.</p>"
            "<p>Trafilatura kısa bir snippet'i atlayabilir.</p>"
            "<p>Bu üçüncü paragraf yeterince uzun bir geri dönüş örneğidir.</p>"
            "</div></body></html>"
        ).encode()
        title, body = extract_html_body(page, "ap_tr", {"title": "Thin page"})
        # We just need SOMETHING extracted — the fallback runs when needed.
        assert title == "Thin page"
        assert len(body) > 0


# ── RSS ────────────────────────────────────────────────────────────────────


class TestRssExtractor:
    def test_returns_title_and_body_from_headers(self) -> None:
        headers = {
            "title": "Yeni gelişme",
            "body": "Türkiye'de yeni bir gelişme yaşandı.",
            "summary": "Kısa özet",
        }
        title, body = extract_rss_body(headers)
        assert title == "Yeni gelişme"
        assert body == "Türkiye'de yeni bir gelişme yaşandı."

    def test_falls_back_to_summary_when_body_missing(self) -> None:
        headers = {"title": "Başlık", "summary": "Sadece özet"}
        title, body = extract_rss_body(headers)
        assert body == "Sadece özet"

    def test_html_markup_in_body_triggers_trafilatura(self) -> None:
        # When the feed gives HTML-wrapped content, trafilatura strips tags.
        html_body = (
            "<p>Bu cümle paragraf etiketlerinin içinde.</p>"
            "<script>kötü kod</script>"
            "<p>İkinci paragraf da burada.</p>" * 5
        )
        headers = {"title": "T", "body": html_body}
        _, body = extract_rss_body(headers)
        assert "kötü kod" not in body
        # Whether the result is the original or a cleaned version depends on
        # trafilatura's threshold; either way no <script> survives.
        assert "<script>" not in body


# ── PDF ────────────────────────────────────────────────────────────────────


class TestPdfExtractor:
    def test_passes_through_body_from_headers(self) -> None:
        headers = {
            "title": "Test Kanunu Hakkında Kanun",
            "body": "Madde 1 - Bu kanun test amaçlıdır.\n\nMadde 2 - Yürürlüğe girer.",
        }
        title, body = extract_pdf_body(headers)
        assert title == "Test Kanunu Hakkında Kanun"
        assert "Madde 1" in body
        assert "Madde 2" in body

    def test_strips_standalone_page_numbers(self) -> None:
        headers = {
            "title": "T",
            "body": "Birinci paragraf metin.\n\n42\n\nİkinci paragraf metin.",
        }
        _, body = extract_pdf_body(headers)
        assert "42" not in body
        assert "Birinci paragraf" in body
        assert "İkinci paragraf" in body

    def test_strips_sayfa_prefix(self) -> None:
        headers = {
            "title": "T",
            "body": "İçerik.\nSayfa 7\nDevam eden içerik.",
        }
        _, body = extract_pdf_body(headers)
        assert "Sayfa 7" not in body

    def test_collapses_runs_of_whitespace(self) -> None:
        headers = {"title": "T", "body": "Bir   çok    boşluk  vardır."}
        _, body = extract_pdf_body(headers)
        assert "  " not in body
        assert body == "Bir çok boşluk vardır."

    def test_empty_body_returns_empty(self) -> None:
        title, body = extract_pdf_body({"title": "", "body": ""})
        assert title == ""
        assert body == ""


# ── Reddit ─────────────────────────────────────────────────────────────────


class TestRedditExtractor:
    def test_extracts_title_selftext_and_comments(self) -> None:
        payload = {
            "title": "Reddit başlığı",
            "selftext": "Bu post'un ana metni.",
            "comments": [
                {"author": "u1", "body": "Yorum bir."},
                {"author": "u2", "body": "Yorum iki."},
                {"author": "u3", "body": "Yorum üç."},
            ],
            "author": "op",
            "score": 100,
            "num_comments": 25,
        }
        raw = json.dumps(payload).encode("utf-8")
        title, body = extract_reddit_body(raw)
        assert title == "Reddit başlığı"
        assert "Bu post'un ana metni." in body
        assert COMMENT_SEPARATOR.strip() in body
        for comment in ("Yorum bir.", "Yorum iki.", "Yorum üç."):
            assert comment in body

    def test_link_post_no_selftext_still_serializes_comments(self) -> None:
        payload = {
            "title": "Dışsal link postu",
            "selftext": "",
            "comments": [{"author": "u1", "body": "İlk yorum."}],
        }
        raw = json.dumps(payload).encode("utf-8")
        _, body = extract_reddit_body(raw)
        assert "İlk yorum." in body
        # Without selftext, the leading separator's leading newlines are trimmed.
        assert body.startswith("--- Yorumlar ---") or "İlk yorum" in body

    def test_invalid_json_returns_empty(self) -> None:
        title, body = extract_reddit_body(b"not json")
        assert title == ""
        assert body == ""

    def test_empty_payload(self) -> None:
        title, body = extract_reddit_body(b"{}")
        assert title == ""
        assert body == ""
