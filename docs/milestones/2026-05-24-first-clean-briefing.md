# ❯ Milestone · first clean nightly briefing · 2026-05-24

On 2026-05-24 at 13:03 TR-local, MÜŞAHİT produced its first end-to-end
clean briefing. Status `COMPLETED`. Briefing markdown 38 KB. Audio MP3
4.4 MB · roughly 4-5 minutes of synthesized Turkish speech reading
today's news. Status flipped cleanly. Date folder matched the run date.
All seven pipeline stages exited green.

This was the first time the system did what it was designed to do.

## ❯ The arc · what changed in 24 hours

On 2026-05-23 at 00:50 TR-local · roughly 36 hours before this milestone ·
MÜŞAHİT shipped a 44 KB silent MP3 placeholder. The pipeline ran, the
stages logged success, the briefing markdown was 77 KB of deterministic
fallback prose dumping all 222 open arcs into AÇIK GELİŞMELER. The audio
was one second of WAV-shaped silence dressed up as MP3. The
`pipeline_runs` row got stuck at status `RUNNING` because the orchestrator's
final state transition never fired. The Trendyol writer model failed all
four retry attempts and produced increasingly garbled text. Categories
were mis-classified because Qwen2.5 returned `DIPLOMASİ` instead of
`DİPLOMASİ` and Pydantic rejected the enum, forcing the cluster to
`UNCLASSIFIED · AMBIENT · low`.

By 2026-05-24 at 13:03, all of that was fixed:

* **Arc-link cascade** · 305 arcs created across two days, zero errors,
  cascade rollback semantics validated under real load.
* **Category normalization** · 89 clusters scored, zero fallbacks. The
  Turkish-character fold-then-map handled every variant.
* **Template placeholder leak** · the `[içerik buraya · şablon
  talimatlarına bak]` echo never appeared. The fallback writer's
  per-section instructions and the validator's content-check both
  worked.
* **TTS silent MP3** · AÇIK GELİŞMELER cap of 10 arcs in Öne Çıkanlar
  produced manageable chunks. Per-chunk synthesis resilience meant a
  single overrun couldn't kill the whole stage. Audio: 4.4 MB, real
  Turkish speech, all five chunks synthesized.
* **Date propagation** · `target_date` as a first-class parameter
  propagated CLI → orchestrator → writer → payload. Briefing landed
  in `briefings/2026/05/24/`, not yesterday's folder.
* **Pipeline lifecycle** · try/finally guarantees terminal status.
  Auto-recovery for stuck-at-RUNNING rows. Merge-on-write defense so
  the poller's destructive reset can't wipe stages_done after the
  orchestrator has populated it.

Test count went from 558 to 627. Six implementation docs filed. One
ADR amendment (ADR-009 § TTS scope). Zero regressions across all six
fixes.

## ❯ What the system now does

The pipeline runs nightly. It fetches Turkish news from 37 sources
across four kinds (RSS, HTML scraping, PDF parsing, Reddit). It
normalizes, embeds, clusters, classifies on a six-level DEFCON ladder,
links related clusters into multi-day arcs, writes a structured
Turkish-language briefing, and synthesizes the voiced portions as
MP3 audio. The operator wakes up at 07:00 and reads/listens to what
happened in Turkey overnight.

When components fail · and they do · the system degrades rather than
breaking. The writer model fails: deterministic fallback fires. A TTS
chunk times out: other chunks still synthesize. A stage raises: the
next stage runs anyway. The final state transition is wrapped in
try/finally so the row never stays at RUNNING. The briefing always
ships.

## ❯ Patterns that emerged

Five fixes in a 19-hour window surfaced patterns worth noting beyond
any single bug:

**One visible symptom, multiple root causes.** The arc-link cascade was
"missing FK + counter advancement + no rollback." The template echo was
"placeholder design + missing data + validator gap." The pipeline
lifecycle bug was "orchestrator transition + poller destructive reset +
silent _read_stages_done fallback." Each one had to be fully traced
before a fix was drafted. A single-cause fix would have left the next
manifestation in place.

**Bounded-input assumptions are everywhere and they all fail eventually.**
Synthesizer assumed manageable chunks. Validator assumed model outputs
contained no placeholders. Orchestrator's resume logic assumed the row
state hadn't been silently rewritten. Each assumption was reasonable at
build time. Each broke when real production data showed up.

**Defense in depth at stage boundaries matters more than any single
fix.** The TTS fix has two layers · cap at source (Öne Çıkanlar) AND
per-chunk resilience (synthesizer). Either alone would have left the
system fragile. Both together mean the audio ships whether the input is
small or unexpectedly large.

**Spec drift is real and ADRs need amendments.** ADR-009's TTS scope
needed amendment to define the Öne Çıkanlar / Diğer Açık Hikayeler
split. The implementation reflected this; the ADR didn't, until we
amended it. Without the amendment the spec would have drifted from
reality silently.

**The visualizer-as-safety-net pattern.** The validator started as a
structural check (eight sections, exact markers). It now checks content
too (no echoed placeholders). It's becoming the operator's last line of
defense against model misbehavior. Worth recognizing as architecture,
not just defensive code.

## ❯ What's still rough

Trendyol-LLM-7B can't reliably follow the template prompt. Four retries
with validator feedback produced increasingly garbled output. The
system currently ships every briefing via the deterministic fallback
writer. The output is structurally correct and information-dense but
mechanical · no narrative discipline, no cross-source framing. Real fix
requires either model swap (qwen2.5:14b? a different Turkish-aligned
model?), prompt restructuring, or formal acceptance that the fallback
is permanent.

Qwen2.5 systematically under-rates Turkish news severity. Of 89 scored
clusters today, only one reached DEFCON 3 (the MİT Syria operation, an
arc continuing from yesterday). New high-impact stories · the Adana
earthquake, the CHP HQ tension, the US-Iran ceasefire negotiations ·
all classified as DEFCON 4. Yesterday's Fethiye mayor shooting and 168
children killed in an airstrike still sit at AMBIENT. The DEFCON ladder
is collapsing toward 4 and 5. This is a model/prompt issue we haven't
touched yet.

Eleven sources still failing reliably (feedparser errors, SSL certs,
URL rot, 403/404s). First-month tuning territory, not blockers.

Two empty-headline arcs leak into the briefing as `(başlıksız) ·
SINIFLANDIRILMADI`. The fallback writer should suppress these. Small
fix.

## ❯ Where this leaves MÜŞAHİT

The system is operationally capable but not yet deployed. The remaining
build steps are deployment infrastructure (liveness probe, Task
Scheduler XML, Windows power plan) and the dashboard UI. The
investigations (Trendyol, DEFCON-3 promotion) are real engineering work
that needs its own time.

For now: the milestone is real. The path from "44 KB silent
placeholder" to "4.4 MB Turkish audio briefing dated today" took five
implementation fixes, one architectural audit, two ADR-shaped
decisions, and 627 passing tests. None of it was clever. It was
disciplined diagnosis followed by bounded changes with tests. That's
what working software looks like.

## ❯ For future-me reading this

If you're reading this in three months because something broke, the
fixes from today are the canary set:

* If the briefing is empty audio · check TTS chunking and Piper
  timeouts first
* If the status sticks at RUNNING · the try/finally in
  `Orchestrator.run` should prevent this, but check what's wrapped
* If categories are wrong · the `_CATEGORY_NORMALIZATION_MAP` is the
  first thing to check
* If briefings land on the wrong date · `target_date` propagation in
  `payload.py::build_payload`
* If arc-link explodes · check the FK workaround in
  `_update_cluster_arc_id` for new tables referencing `clusters.id`

The bigger lesson · never trust a stage boundary. Components hold
resources, produce unexpectedly large outputs, fail in correlated
ways. The orchestrator should always assume the stage it just ran
might have left things in a weird state. The merge-on-write defense
exists for a reason.

This was a good day's work. The system is real now.
