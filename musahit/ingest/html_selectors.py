"""Per-source CSS selector configurations for the HTML ingester.

The HTML ingester is structurally uniform — two-phase fetch, four-step
``canonical_timestamp`` chain, identical persistence — but every source has
its own DOM. Rather than scatter selectors across nine implementations, we
keep them in one mapping with a frozen dataclass per source.

Fields used at ingest time:

* ``listing_selector`` (required) — scopes the article-link search to the
  listing region of the source's landing page. Prevents picking up nav,
  footer, or "related stories" links.
* ``article_link_selector`` (required) — finds the ``<a href="…">``
  elements *within* the listing scope.
* ``title_selector`` (optional) — extracts the article title at ingest
  time for diagnostics. Falls back to ``<title>`` when absent.
* ``published_selector`` (optional) — narrows the Turkish-regex step of
  the canonical-timestamp chain to text within this selector. When unset
  the regex scans the full article body. Keeps the chain at four steps
  (per the build-plan tripwire) by tuning step 3 rather than adding one.

Fields reserved for the normalize stage (step 9):

* ``body_selector`` (optional) — main-content selector for body extraction.
  Not read by the ingester; documented here so per-source tuning lives in
  one place.

Verified placeholders vs. TODOs are flagged in the inline comments. The
operator's first nightly run will surface selectors that need adjustment;
each is a one-line edit in this file.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SelectorConfig:
    """CSS selectors for one HTML source.

    Required: ``listing_selector`` (listing-scope container) and
    ``article_link_selector`` (article anchors within the scope).
    All other fields are optional with fallbacks documented per field.
    """

    listing_selector: str
    article_link_selector: str
    title_selector: str | None = None
    body_selector: str | None = None
    published_selector: str | None = None


# ── Selector registry ──────────────────────────────────────────────────────
#
# All entries below are first-pass placeholders. None of these were verified
# against a live fetch during step 5; the operator's first nightly run will
# surface selectors that miss. Each placeholder represents the most
# defensible guess given a brief inspection of the public landing page; the
# alternative — leaving the dict empty — would block step 5 from shipping.
#
# Selector tuning is one-line edits in this file; per-source unit tests in
# step 9 (normalize) will pin them down once real fetches land.

SELECTORS: dict[str, SelectorConfig] = {
    # ── INTERNATIONAL · NEWS ────────────────────────────────────────────
    "ap_tr": SelectorConfig(
        # TODO: verify against https://apnews.com/hub/turkey
        listing_selector="main",
        article_link_selector="a.PagePromo-title, a[data-key='card-headline']",
        title_selector="h1",
        body_selector="div.RichTextStoryBody",
        published_selector="bsp-timestamp, span.Timestamp",
    ),
    # ── MARKETS · PRIMARY_MARKET ────────────────────────────────────────
    "tcmb": SelectorConfig(
        # TODO: verify against https://www.tcmb.gov.tr/ press-release area
        listing_selector="div.press-releases, main",
        article_link_selector="a[href*='/wps/wcm/connect']",
        title_selector="h1, h2.title",
        body_selector="div.content-area, div.text",
        published_selector="span.date, time",
    ),
    "bist": SelectorConfig(
        # TODO: verify against https://www.borsaistanbul.com/
        listing_selector="div.market-announcements, main",
        article_link_selector="a[href*='/duyurular'], a[href*='/news']",
        title_selector="h1",
        body_selector="div.announcement-body, article",
        published_selector="time, span.publish-date",
    ),
    "tuik": SelectorConfig(
        # TODO: verify against https://www.tuik.gov.tr/
        listing_selector="div.haberler, div.press-release-list, main",
        article_link_selector="a[href*='/Bulten/'], a[href*='/PressRelease']",
        title_selector="h1, h2",
        body_selector="div.detay, article",
        published_selector="span.tarih, time",
    ),
    # ── GOV · PRIMARY_GOV ───────────────────────────────────────────────
    "tbmm": SelectorConfig(
        # TODO: verify against https://www.tbmm.gov.tr/ daily agenda
        listing_selector="div.gundem, main",
        article_link_selector="a[href*='/Yasama']",
        title_selector="h1",
        body_selector="div.icerik",
        published_selector="span.tarih, time",
    ),
    "cumhurbaskanligi": SelectorConfig(
        # TODO: verify against https://www.tccb.gov.tr/
        listing_selector="div.haberler, main",
        article_link_selector="a[href*='/haberler/'], a[href*='/kararname']",
        title_selector="h1",
        body_selector="div.haber-detay, article",
        published_selector="span.tarih, time",
    ),
    # ── GOV · PRIMARY_JUDICIAL ──────────────────────────────────────────
    "anayasa_mahkemesi": SelectorConfig(
        # TODO: verify against https://www.anayasa.gov.tr/ decisions list
        listing_selector="div.kararlar, main",
        article_link_selector="a[href*='/Kararlar/']",
        title_selector="h1",
        body_selector="div.karar-detay, article",
        published_selector="span.tarih, time",
    ),
    "yargitay": SelectorConfig(
        # TODO: verify against https://www.yargitay.gov.tr/
        listing_selector="div.haberler, main",
        article_link_selector="a[href*='/haber/']",
        title_selector="h1",
        body_selector="div.haber-detay, article",
        published_selector="span.tarih, time",
    ),
    # danistay's SelectorConfig was removed 2026-05-25 with the source itself
    # · architecturally unreachable without a JS renderer · see sources.py.
}


__all__ = ["SELECTORS", "SelectorConfig"]
