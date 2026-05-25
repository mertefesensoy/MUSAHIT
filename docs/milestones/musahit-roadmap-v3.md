# ❯ MÜŞAHİT roadmap

Working document. Updated as work progresses. Last updated 2026-05-25 evening.

## ❯ Status

The pipeline runs end-to-end and ships a complete briefing with audio.
First clean nightly briefing landed 2026-05-24. Three consecutive clean
briefings as of 2026-05-25. Arc evolution shipped 2026-05-25 evening
(645 tests passing, +18 new).

Test count: 645 passing, 2 skipped. Seven implementation fixes shipped
between 2026-05-23 and 2026-05-25. One ADR amendment (ADR-009).

## ❯ Pending findings from the smoke runs

These are bugs identified during production runs. Listed in priority order.

### Government source coverage · curl_cffi fix path confirmed

Status: pending · curl_cffi adoption needed · ready to scope a focused
session

**Triage and curl_cffi spike on 2026-05-25 produced a confirmed fix
path for 3 of 5 gov sources.** Original framing was "each gov source
needs a custom scraper." Real finding: Turkish government CDNs do
TLS-level bot detection that rejects standard Python `httpx`/OpenSSL
clients · but accepts `curl_cffi` with appropriate browser
impersonation.

**Evidence chain:**

1. Browser fetches `resmi_gazete` PDF cleanly · operator verified visually
2. Python `httpx` during production ingest · ConnectError with empty
   message for tccb/danistay · `SSL: CERTIFICATE_VERIFY_FAILED` for
   anayasa (despite GlobalSign cert) · status=OK with 0 articles for
   resmi_gazete (silent parse failure)
3. Python `ssl.create_default_context()` direct probe · `SSL:
   UNEXPECTED_EOF_WHILE_READING` on tccb · `ConnectionReset` on
   danistay · clean verification on anayasa and yargitay
4. PowerShell `Invoke-WebRequest` · 200 OK on homepage, fails on PDF
   endpoint · inconsistent
5. `curl_cffi` with firefox133 impersonation · 200 OK on tccb,
   resmi_gazete homepage, yargitay · cert verify error on anayasa
   (curl_cffi CA bundle issue · configurable) · still fails on danistay
6. `curl_cffi.Session` with firefox133 + homepage-first + Referer header
   · downloaded **11MB of 19MB resmi_gazete PDF** before 30s timeout ·
   the pattern works, just needs adequate timeout
7. Rate limiting kicks in after sustained probe activity from same IP
   · subsequent probes get rejected at TLS layer · respect crawl pacing

**Confirmed working pattern (the path forward):**

```
curl_cffi.Session()
  impersonate="firefox133"
  visit homepage first (establishes ASP.NET .AspNetCore.Antiforgery
                        cookie)
  fetch deep URLs with Referer: <homepage>
  generous timeout (PDFs are ~19MB · several seconds)
  respect rate limiting · pace requests
  use system CA bundle via certifi for anayasa_mahkemesi cert verify
```

**Source-by-source outcome with curl_cffi adoption:**

| Source | curl_cffi outcome | Notes |
|---|---|---|
| `resmi_gazete` | works with session pattern | Saw 11/19MB · timeout was config, not block |
| `cumhurbaskanligi` | works with chrome120 or firefox133 | Cleanest case |
| `anayasa_mahkemesi` | cert verify · fixable | curl_cffi CA bundle config |
| `yargitay` | already works · not the real issue | Production failure was transient DNS |
| `danistay` | server-level refusal across all clients | Likely IP-based or geo-fenced · accept loss |

**resmi_gazete has a SECOND bug** independent of TLS · the existing
`ResmiGazeteIngester._process_pdf` does not validate that the response
body is actually a PDF. When a partial response or HTML interstitial
comes back, the PDF parser produces zero items and the ingester
reports OK·count=0 silently. Fix needs to validate `%PDF` magic bytes
and fail loudly. Independent of TLS work but discovered alongside it.

**Next session shape (when picked up):**

* New `musahit/ingest/gov_http.py` · `curl_cffi.Session` wrapper with
  the established pattern
* Add `is_gov_source` field to `Source` model (or tag-based filter)
* Refactor `HtmlIngester` and `ResmiGazeteIngester` to delegate gov
  fetches to the new module
* Add `%PDF` magic-byte validation to `ResmiGazeteIngester._process_pdf`
* Drop `danistay` from sources with explicit "architecturally
  unreachable" comment
* Tests against captured PDF fixtures (one is at
  `scripts/triage/captured_gazette.pdf` from this spike if Step 2
  saved it)
* Documentation · new ADR amendment about gov-source HTTP path
* Operational note · gov sources need slower crawl rate to avoid IP
  rate-limit triggering

**Realistic scope:** 4-6 hours · own focused session · don't combine
with other work.

**Real-world cost surfaced 2026-05-25:** Bilgi University re-opening
(Cumhurbaşkanlığı Kararı in Resmî Gazete) missed from voiced section.
The decree was in the gazette PDF that the curl_cffi spike was
downloading when timeout hit. This is exactly the kind of news that
this fix would catch.

### KAP source is structurally obsolete

Status: pending · decision needed

KAP (Kamuyu Aydınlatma Platformu) migrated from RSS to a Next.js SPA.
Triage on 2026-05-25 confirmed:
* `https://www.kap.org.tr/` → 307 redirect to `/en/` (HTML SPA)
* No RSS endpoint exists at any obvious path
* All probed candidate paths return Next.js 404 pages
* Content is rendered client-side from internal API calls

**Options:**
* Drop `kap` from sources (5 min · honest)
* Build a custom `KAPApiIngester` reverse-engineering their internal
  endpoints (4-6 hours · uncertain reliability · they can change the
  API any time)
* Defer indefinitely (do nothing · accept blindness to KAP disclosures)

**Recommended:** Drop from sources for now. KAP filings are
business-disclosure-specific (listed company material disclosures) ·
they rarely surface news that mainstream Turkish business press
doesn't pick up within hours. Worth re-evaluating if AĞ (the parked
corruption-graph project) gets resurrected · KAP is core data for that.

### Arc continuation summaries don't evolve

Status: SHIPPED 2026-05-25 ·
see docs/implementations/2026-05-25-arc-evolution.md

Migration 004 applied · 431 arcs backfilled cleanly · 18 new tests ·
zero regressions. First live test fires tomorrow morning's nightly
run. Verify by listening for `Güncelleme` prefix in audio and the
italic stalled marker in markdown.

### Trendyol writer unusable · architectural

Status: pending · biggest LLM issue · likely needs ADR-002 amendment

The writer LLM (`serkandyck/trendyol-llm-7b-chat-v1.8-gguf`) cannot
reliably follow the eight-section template prompt. Four retry attempts
with validator feedback produce increasingly garbled output ·
attempt 3 often consists of the model echoing OUR retry-feedback string
back as a section heading. The deterministic fallback writer fires
every time.

Options:
* Swap to a larger or differently-tuned model (qwen2.5:14b · gemma2 ·
  newer Trendyol release)
* Restructure the prompt (smaller sections per call · chain-of-thought ·
  few-shot examples · constrained decoding)
* Accept the deterministic fallback as permanent and improve its prose
* Hybrid · use Trendyol for individual section content and assemble
  deterministically

Estimated scope: 4-6 hours of investigation + experimentation. Own session.

### DEFCON-3 promotion collapse · model/prompt issue

Status: pending · second-deepest finding

Qwen2.5 systematically under-rates Turkish news severity. From the
2026-05-25 run: 487 clusters scored, only 1 reached DEFCON 3 (the
ongoing MİT Syria arc). Cluster examples that should have been
DEFCON 3 today:

* Bilgi University police intervention with pepper gas (11 sources)
* CHP HQ police intervention
* Magnitude 4.9 Adana earthquake
* US-Iran ceasefire negotiations

The ladder is collapsing toward DEFCON 4-5. Categorization is mostly
correct; severity rating is the failure surface.

Investigation needed:
* Query the actual raw_defcon distribution from production runs
* Read 10-20 sample worker prompts + responses end-to-end
* Compare against a manually-rated benchmark (operator rates 30
  clusters by hand, compare to model output)
* Decide on remediation · prompt restructure · anchor changes · model
  swap · ADR-004 amendment

Estimated scope: 4-6 hours of careful data exploration. Own session.

### Empty-headline arcs in fallback briefing

Status: pending · small · slightly higher priority now

Two arcs (`arc_20260523_0036`, `arc_20260523_0146`) leak into the
briefing as `(başlıksız) · SINIFLANDIRILMADI`. As of 2026-05-25,
one of these now leaks into the voiced Öne Çıkanlar section, not
just the bulleted tail. The audio will read out a section header
with no content. The fallback writer should suppress these.

Estimated scope: 30 minutes.

### Source registry tuning · first-month work

Status: pending · ongoing operational · scoped down after gov-source
triage moved 5 sources to their own category

Remaining non-gov failures from 2026-05-25:
* `milliyet` · RSS parse error (mismatched tag)
* `cumhuriyet` · RSS returns empty
* `halk_tv` · 301 redirect to broken feed
* `medyascope` · 403 forbidden
* `dunya` · RSS parse error
* `mahfi` · RSS parse error
* `voa_tr` · 404 (URL rot · API endpoint changed)
* `reuters_tr` · DNS failure (`feeds.reuters.com` gone)
* `bianet` · 301 redirect, then 0 entries
* `tcmb` · timeout (slow site)
* `reddit_turkey` · SKIPPED (credentials not configured)

Each is an individual fix. Probably 2-3 hours total across a few sessions.

## ❯ Triage diagnostic infrastructure (new 2026-05-25)

`scripts/triage/` now holds three diagnostic probes:
* `probe_gov_sources.ps1` · multi-UA HTTP probe against gov sources
* `probe_ssl.py` · TLS handshake inspection for cert and handshake errors
* `probe_kap.py` · KAP redirect chain + candidate RSS endpoint check
* `probe_resmi_gazete.py` · candidate PDF URLs + content-type inspection

These were ad-hoc for this triage but worth keeping · they'll be useful
when revisiting any source health issue.

## ❯ Remaining build steps

### Step 17 · Liveness probe + failure alert path

Per ADR-007. Task Scheduler fires the pipeline at 01:00. If no briefing
is on disk by 07:00, a Windows toast notification fires alerting the
operator.

Estimated scope: 1-2 sessions.

### Step 18 · Task Scheduler XML + Windows power plan

Production deployment. Pipeline becomes autonomous.

Estimated scope: 1 session.

### Step 19 · FastAPI dashboard

PoI-themed UI. HTMX + Jinja templates + dark monospace aesthetic.
Intentionally deferred to last.

Estimated scope: 3-5 sessions.

## ❯ Longer-term work

### Audio QA cycles

Hearing the briefing every morning surfaces pronunciation issues,
pacing problems, English-loanword respelling needs, awkward chunk
transitions. Ongoing for weeks after deployment.

### MEMORY.md conventions to file

Patterns surfaced during the 2026-05-23 → 2026-05-25 work that
deserve to live in MEMORY.md:
* "Defense in depth at stage boundaries" · cap-at-source + per-stage
  resilience for any bounded-input assumption
* "Validator-as-safety-net" · the validator is becoming the operator's
  last line of defense against model misbehavior
* "One symptom, multiple roots" · investigate before drafting fixes
* "Spec drift requires ADR amendment" · don't code around stale specs
* "When a diagnostic probe contradicts the production behavior, stop
  and find the difference" · 2026-05-25 gov-source triage discovered
  TLS fingerprinting only after multiple contradicting probes

Estimated scope: 30 minutes.

### Investigation pipeline improvements

The DEFCON-3, gov-source, and resmi_gazete work all needed ad-hoc data
exploration scripts. The pattern is now solid · keep `scripts/triage/`
as a permanent directory and add more probes as needed.

## ❯ Parked ideas

### AĞ · corruption network graph

Notes in personal Obsidian vault. Separate sibling project. Inspired
by Brazilian OSINT projects.

Note for future-AĞ: if/when this resurrects, the gov-source TLS
fingerprinting problem becomes critical · UYAP, KAP, Resmî Gazete
are all primary data sources for AĞ. The `curl_cffi` decision for
MÜŞAHİT determines whether AĞ has a viable data-ingestion path.

Status: parked.

## ❯ Operator daily routine (when deployed)

1. Wake at 07:00 · check phone for liveness alert (should be none)
2. Open dashboard at `localhost:8001` · read briefing markdown
3. Listen to briefing.mp3 while making coffee · note pronunciation issues
4. After listening · spend 5 minutes reviewing AÇIK GELİŞMELER for arc
   continuity
5. Once per week · review source health dashboard, fix one failing source
6. Once per month · review DEFCON distribution, audit a sample of
   classifications

## ❯ Notes for future sessions

* Don't pick up DEFCON-3 and Trendyol in the same session · entangled
  but separate diagnoses.
* The audit-by-Claude-Code pattern worked well for the pipeline
  lifecycle bug · use it again when manual diagnosis is going in circles.
* The "investigate first, draft after" discipline caught real things
  multiple times.
* When pulling code for diagnosis, always read it before reasoning ·
  memory of the structure is unreliable.
* Government sources are blind spots that compound · the Bilgi
  University miss demonstrated this is a real operational cost, not
  theoretical concern.
* The "what's different between probe and production" question is the
  right way to interrogate intermittent failures · diagnostic probes
  often pass when production fails, and that gap is itself the bug.
