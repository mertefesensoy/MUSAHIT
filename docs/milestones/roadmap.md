# ❯ MÜŞAHİT roadmap

Working document. Updated as work progresses. Last updated 2026-05-25.

## ❯ Status

The pipeline runs end-to-end and ships a complete briefing with audio.
First clean nightly briefing landed 2026-05-24. Three consecutive clean
briefings as of 2026-05-25. The remaining work is roughly half "fix
known issues from production" and half "complete the deployment
surface that was deferred during the build phase."

Test count: 627 passing, 2 skipped. Six implementation fixes shipped
since 2026-05-23. One ADR amendment (ADR-009).

## ❯ Pending findings from the smoke runs

These are bugs identified during production runs that haven't been
fixed yet. Listed in rough priority order.

### Government source coverage critical gap

Status: pending · structural issue · elevated priority

Roughly half of MÜŞAHİT's government-source surface is broken across
all runs:

* `anayasa_mahkemesi` (HTTP_ERROR) · constitutional court
* `cumhurbaskanligi` (HTTP_ERROR) · presidency
* `danistay` (HTTP_ERROR) · council of state
* `yargitay` (HTTP_ERROR) · supreme court of appeals
* `kap` (PARSE_ERROR) · capital markets authority filings
* `resmi_gazete` · status unclear from logs (silently absent from
  failures · may indicate silent failure rather than success)

The bias-band concept (per ADR-003) assumes government sources are
part of the corroboration triangle. With half of them broken, the
bias balance is structurally skewed toward opposition + mainstream +
foreign. Government sources should be the most observed, not the
most broken.

Real-world cost surfaced 2026-05-25: the Bilgi University re-opening
(Cumhurbaşkanlığı Kararı in Resmî Gazete) was missed in the voiced
section. Mainstream picked it up but MÜŞAHİT couldn't track it from
the authoritative source. By the time it surfaced, the briefing
treated it as a tertiary story.

Components needed:
* Diagnostic per failing source · why each fails (UA blocking, JS
  rendering required, URL changed, session cookies, etc.)
* Custom scrapers per source · government sites rarely follow
  scraping conventions
* Possible "GovScraper" base class with Playwright/Selenium fallback
* Possibly · DEFCON promotion logic for government-sourced clusters
  (a presidential decree is materially different from a tweet)
* Possibly · ADR amendment around source weighting

Estimated scope: 1-2 sessions. Diagnostic-first investigation.

### Arc continuation summaries don't evolve

Status: starting work today (2026-05-25)

Arc summaries are frozen at arc-seed time. Day 3 of the MİT Syria
arc reads identical to day 1 · same sentence, same "Açıldı"
date. The operator hears the same content repeatedly with no
signal of whether the story is moving or stalled.

Fix shape (Option C from design discussion):
* Schema: `arcs.last_update_summary`, `arcs.last_update_headline`,
  `arcs.last_update_at`, `arcs.last_update_cluster_id`
* Arc-link: every joining cluster updates these fields (most recent
  wins · same-day continuations evolve too)
* Writer: active-today arcs render `**Güncelleme** · [today's
  cluster summary]`; stalled arcs render the original with
  `**Son güncelleme** · X gün önce` and an explicit "Bu arc'da bugün
  yeni gelişme yok" line
* TTS: active-today arcs get priority in the Öne Çıkanlar voiced cap

Started: 2026-05-25 afternoon.

### Trendyol writer unusable · architectural

Status: pending · biggest LLM issue · likely needs ADR-002 amendment

The writer LLM (`serkandyck/trendyol-llm-7b-chat-v1.8-gguf`) cannot
reliably follow the eight-section template prompt. Four retry attempts
with validator feedback produce increasingly garbled output ·
attempt 3 often consists of the model echoing OUR retry-feedback string
back as a section heading. The deterministic fallback writer fires
every time and ships a structurally valid but mechanically-written
briefing.

Options:
* Swap to a larger or differently-tuned model (qwen2.5:14b · gemma2 ·
  newer Trendyol release)
* Restructure the prompt (smaller sections per call · chain-of-thought ·
  few-shot examples · constrained decoding)
* Accept the deterministic fallback as permanent and improve its prose
  (less mechanical phrasing · richer cross-source framing)
* Hybrid · use Trendyol for individual section content and assemble
  deterministically

Estimated scope: 4-6 hours of investigation + experimentation. Own session.

### DEFCON-3 promotion collapse · model/prompt issue

Status: pending · second-deepest finding

Qwen2.5 systematically under-rates Turkish news severity. From the
2026-05-25 run: 487 clusters scored, only 1 reached DEFCON 3 (the
ongoing MİT Syria arc). Cluster examples that should have been
DEFCON 3 today:

* Bilgi University police intervention with pepper gas (11 sources!)
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

Status: pending · ongoing operational

15 sources fail across runs as of 2026-05-25 · roughly the same set
as 2026-05-23 with `yargitay (HTTP_ERROR)` newly joining. Several of
these overlap with the gov-source gap above. Non-gov failures:

* `milliyet` · RSS parse error (mismatched tag)
* `cumhuriyet` · RSS returns empty
* `halk_tv` · 301 redirect to broken feed
* `medyascope` · 403 forbidden
* `dunya` · RSS parse error
* `mahfi` · RSS parse error
* `voa_tr` · 404 (URL rot · API endpoint changed)
* `reuters_tr` · DNS failure (`feeds.reuters.com` gone)
* `bianet` · 301 redirect, then 0 entries
* `tcmb` · timeout (slow site, may need longer per-source timeout)

Plus `reddit_turkey` SKIPPED because credentials aren't configured ·
either provision Reddit API creds or remove from sources.

Each is an individual fix: update URL, switch to HTML scraping, change
parser, add retry, or drop. Probably 2-3 hours total across a few sessions.

## ❯ Remaining build steps

### Step 17 · Liveness probe + failure alert path

Per ADR-007. Task Scheduler fires the pipeline at 01:00. If no briefing
is on disk by 07:00, a Windows toast notification fires alerting the
operator. Catches "MÜŞAHİT silently broke overnight."

Components:
* Scheduled task running a small Python check at 07:00
* Read latest briefing date · compare to expected · compute staleness
* Windows toast via PowerShell or `winrt-Windows.UI.Notifications`
* Optional · SMTP alert if email is configured in settings

Estimated scope: 1-2 sessions.

### Step 18 · Task Scheduler XML + Windows power plan

Production deployment. The pipeline becomes autonomous.

Components:
* Task Scheduler XML file checked into repo
* Registration script (PowerShell)
* Power plan adjustment so the laptop stays awake during 01:00-07:00
* Documentation of the install procedure
* Test the actual nightly schedule against the running laptop

Estimated scope: 1 session.

### Step 19 · FastAPI dashboard

The PoI-themed UI. HTMX + Jinja templates + dark monospace aesthetic.

Components:
* FastAPI app reading from DuckDB
* Briefing renderer (markdown → HTML)
* Audio player with timestamps
* Calendar of past briefings
* Per-arc timeline view
* DEFCON distribution chart
* Source health dashboard

Estimated scope: 3-5 sessions. Intentionally deferred to last so it
reads real production data.

## ❯ Longer-term work

### Audio QA cycles

Hearing the briefing every morning will surface pronunciation issues,
pacing problems, English-loanword respelling needs, awkward chunk
transitions. Each is a small fix. Expected to be ongoing for weeks
after deployment.

### MEMORY.md conventions to file

Several patterns surfaced during the 2026-05-23 → 2026-05-24 work that
deserve to live in MEMORY.md:
* "Defense in depth at stage boundaries" · cap-at-source + per-stage
  resilience for any bounded-input assumption
* "Validator-as-safety-net" · the validator is becoming the operator's
  last line of defense against model misbehavior
* "One symptom, multiple roots" · investigate before drafting fixes
* "Spec drift requires ADR amendment" · don't code around stale specs

Estimated scope: 30 minutes. Small but real discipline.

### Investigation pipeline improvements

The DEFCON-3 and source-tuning work both need ad-hoc data exploration
scripts that don't exist yet. Worth building a small `scripts/explore/`
directory with reusable query helpers.

## ❯ Parked ideas

### AĞ · corruption network graph

Notes in personal Obsidian vault. Separate sibling project that would
ingest MÜŞAHİT's data alongside UYAP, KAP, Resmî Gazete, investigative
journalism. Graph of politicians, businesses, contracts, cases.
Inspired by Brazilian OSINT projects.

Hard problems flagged: entity disambiguation · truth-status taxonomy
(claimed/reported/charged/convicted) · legal exposure (TCK 125/267/299) ·
source-bias selection · curation workflow.

Realistic scope: 6-12 month side project. Phase 0 (design) before any
code · 1-2 weeks at minimum.

Status: parked. Don't start until MÜŞAHİT is shipping and stable.

## ❯ Operator daily routine (when deployed)

1. Wake at 07:00 · check phone for liveness alert (should be none)
2. Open dashboard at `localhost:8001` · read briefing markdown
3. Listen to briefing.mp3 while making coffee · note pronunciation issues
4. After listening · spend 5 minutes reviewing AÇIK GELİŞMELER for arc
   continuity
5. Once per week · review source health dashboard, fix one failing source
6. Once per month · review DEFCON distribution, audit a sample of
   classifications

This routine is the actual product. Everything else is infrastructure.

## ❯ Notes for future sessions

* Don't pick up DEFCON-3 and Trendyol in the same session. They're
  entangled (DEFCON-3 collapse may partially be the writer being unable
  to surface DEFCON-3 content) but they're separate diagnoses.
* The audit-by-Claude-Code pattern worked well for the pipeline
  lifecycle bug · use it again when manual diagnosis is going in circles.
* The "investigate first, draft after" discipline caught real things
  multiple times. Don't skip the investigation phase even when the fix
  feels obvious.
* When pulling a code file for diagnosis, always read it before
  reasoning about it. Memory of the structure is unreliable.
* Government sources are blind spots that compound. Whenever a major
  story is missed, check whether a gov source should have caught it
  first.
