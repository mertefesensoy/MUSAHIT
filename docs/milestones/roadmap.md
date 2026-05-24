# ❯ MÜŞAHİT roadmap

Working document. Updated as work progresses. Last updated 2026-05-24.

## ❯ Status

The pipeline runs end-to-end and ships a complete briefing with audio.
First clean nightly briefing landed 2026-05-24. The remaining work is
roughly half "fix known issues from production" and half "complete the
deployment surface that was deferred during the build phase."

Test count: 627 passing, 2 skipped. Six implementation fixes shipped
since 2026-05-23. One ADR amendment (ADR-009).

## ❯ Pending findings from the smoke runs

These are bugs identified during production runs that haven't been
fixed yet. Listed in rough priority order.

### Trendyol writer unusable · architectural

Status: pending · biggest remaining bug · likely needs ADR-002 amendment

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

Estimated scope: 4-6 hours of investigation + experimentation. Probably
its own session.

### DEFCON-3 promotion collapse · model/prompt issue

Status: pending · second-deepest finding

Qwen2.5 systematically under-rates Turkish news severity. From the
2026-05-23 smoke run: 242 clusters scored, only 1 reached DEFCON 3
(MİT Syria operation). Cluster examples that should have been DEFCON 3:
* Fethiye mayor shooting (rated AMBIENT)
* 168 children killed in airstrike (rated AMBIENT)
* 25 arrested in major operation (rated AMBIENT)
* US-Iran ceasefire negotiations (rated DEFCON 4 today)

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
May need ADR-004 amendment if anchor definitions need adjustment.

### Empty-headline arcs in fallback briefing

Status: pending · small

Two arcs from 2026-05-23 leak into the briefing as `(başlıksız) ·
AMBİYANS · SINIFLANDIRILMADI`. The fallback writer should suppress
these. One-line fix in `_render_arc` to skip arcs with empty headlines
or missing categories.

Estimated scope: 30 minutes.

### Source registry tuning · first-month work

Status: pending · ongoing operational

11 sources fail reliably across runs:
* `milliyet` · RSS parse error (mismatched tag)
* `cumhuriyet` · RSS returns empty
* `halk_tv` · 301 redirect to broken feed
* `medyascope` · 403 forbidden
* `dunya` · RSS parse error
* `kap` · returns HTML 307 redirect, not parseable as RSS
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
