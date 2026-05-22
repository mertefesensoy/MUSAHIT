# MÜŞAHİT · BOOTSTRAP

**Status** · Foundation locked · 2026-05-22
**Owner** · Mert Efe Şensoy
**Read this before any other file in the repository**

---

## ❯ What this is

MÜŞAHİT is a personal OSINT system that tracks Turkish political, economic, judicial,
security, diplomatic, regulatory, and social events from open sources. It runs once a night
between 01:00 and 07:00 on a Windows laptop, synthesizes the previous day's signal across
ideologically-banded sources, and produces a Turkish-language briefing for the operator
at 07:00 · delivered as a local web dashboard and a Piper-voiced audio file.

The system is inspired by *Person of Interest*. The Machine sees the whole. MÜŞAHİT sees
the whole of Turkey, every night, while the operator sleeps.

---

## ❯ Hard constraints

These are not negotiable without an ADR revision.

- **Runtime** · Windows laptop · iGPU only · CPU inference · Python 3.11+
- **Local LLMs** · Qwen2.5 7B Instruct (worker) + Trendyol-LLM 7B v4 (writer) · both via Ollama · GGUF Q4_K_M
- **Embedding** · `bge-m3` via Ollama
- **TTS** · Piper (`tr_TR-dfki-medium`) · fully local · CPU
- **Storage** · DuckDB · single file at `data/musahit.duckdb`
- **Scheduler** · Windows Task Scheduler · 01:00 trigger · wake-on-task enabled · 06:45 liveness probe
- **Dashboard** · FastAPI + Jinja2 + HTMX · dark monospace aesthetic
- **Legal envelope** · OSINT only · public HTML and RSS · no paywall circumvention · no login walls · personal consumption only

---

## ❯ Pipeline at a glance

```
01:00  ingest         RSS · HTML · gov primary · KAP · Reddit · X (deferred)
02:00  normalize      article extraction · language detect · entity tagging
02:30  embed+cluster  bge-m3 · cross-outlet collapse · ideological band tagging
03:30  score+classify Qwen2.5 7B · DEFCON 0-5 · category · confidence
04:30  arc linking    new clusters into ongoing arcs · OPEN/WATCH/RESOLVED transitions
05:00  writer pass    Trendyol-LLM 7B · Turkish briefing · attribution discipline
06:00  voice synth    Piper · DEFCON 1-3 + open arc updates only · ~5 min audio
06:30  artifact lock  briefing.html · briefing.mp3 · briefing.md written
06:45  liveness       probe checks artifacts exist · if not, fallback alert
07:00  delivered      dashboard serves · audio queued for playback
```

---

## ❯ Locked decisions index

Every decision in this project lives in an ADR. Do not invent decisions that contradict
an ADR. If an ADR needs to change, write a follow-up ADR that supersedes it.

| ADR | Subject |
|---|---|
| ADR-001 | Architecture overview |
| ADR-002 | LLM stack |
| ADR-003 | Source registry and bands |
| ADR-004 | DEFCON schema |
| ADR-005 | Bias promotion rules |
| ADR-006 | Storage |
| ADR-007 | Scheduler and liveness |
| ADR-008 | Story arc model |
| ADR-009 | Briefing template |
| ADR-010 | TTS and delivery |
| ADR-011 | Dashboard |
| ADR-012 | Failure and retention |

---

## ❯ Repository layout

```
musahit/
├── adr/                    # ADRs · locked decisions
├── docs/                   # design notes · glossary · operator guide
├── src/
│   ├── ingest/             # source pollers · RSS · HTML · primary feeds
│   │   ├── sources.py      # source registry · FILE-PROTECTED
│   │   ├── poller.py       # ingestion orchestrator · FILE-PROTECTED
│   │   ├── rss.py
│   │   ├── html_scrape.py
│   │   ├── resmi_gazete.py
│   │   ├── kap.py
│   │   ├── reddit.py
│   │   └── x_stub.py       # placeholder · ingestion deferred
│   ├── normalize/          # article extraction · entity tagging
│   ├── cluster/            # bge-m3 embedding · clustering · dedup
│   ├── score/
│   │   ├── defcon.py       # DEFCON schema constants · FILE-PROTECTED
│   │   ├── classify.py     # Qwen2.5 7B classification
│   │   └── promotion.py    # bias promotion rules
│   ├── arcs/               # story arc linking · state transitions
│   ├── writer/             # Trendyol-LLM Turkish briefing generation
│   ├── tts/                # Piper synthesis
│   ├── api/                # FastAPI routes for dashboard
│   ├── common/             # shared types · db · logging · config
│   └── pipeline.py         # top-level orchestrator · entrypoint
├── dashboard/
│   ├── templates/          # Jinja2 templates · PoI aesthetic
│   ├── static/             # CSS · monospace fonts · HTMX
│   └── routes.py
├── data/
│   ├── musahit.duckdb      # primary store
│   └── backups/            # nightly rotated copies
├── briefings/
│   ├── 2026/05/22/
│   │   ├── briefing.html
│   │   ├── briefing.md
│   │   └── briefing.mp3
│   └── ...
├── logs/                   # structured JSON · rotated daily
├── tests/
├── scripts/
│   ├── install_windows.ps1
│   ├── pull_models.ps1     # ollama pull qwen2.5 + trendyol + bge-m3
│   └── task_scheduler.xml  # importable Task Scheduler definition
├── pyproject.toml
├── BOOTSTRAP.md            # this file
├── README.md
├── .env.example            # only Reddit credentials for v0.1
└── .gitignore
```

---

## ❯ File protection list

Once these files reach their first locked version, modifications require an ADR amendment
and explicit operator override. This convention is carried over from SuperconducTED.

- `src/ingest/sources.py` · source registry and bands · changes alter the data fabric
- `src/ingest/poller.py` · ingestion orchestrator · rate limits and retry policy
- `src/score/defcon.py` · DEFCON schema · severity calibration
- `src/score/promotion.py` · bias promotion rules · core editorial discipline
- `adr/*.md` · all ADRs once status is Accepted

---

## ❯ Coding conventions

- **Language** · Python 3.11+ · type hints required · `from __future__ import annotations`
- **Style** · `ruff` for lint and format · 100 char lines
- **Imports** · stdlib · third-party · local · separated by blank lines
- **Logging** · `structlog` · JSON output · one event per line · UTC timestamps
- **Config** · `pydantic-settings` · `.env` for secrets · `config.toml` for everything else
- **Tests** · `pytest` · parsers must have unit tests · pipeline has one integration test with fixture data
- **PowerShell** · development environment is Windows PowerShell · scripts written with this in mind · no bash assumptions

---

## ❯ Naming conventions

- **System** · `MÜŞAHİT` in headers · `musahit` in code/paths (ASCII-safe)
- **Briefings** · `briefing-YYYYMMDD.{html,md,mp3}`
- **Arc IDs** · `arc_YYYYMMDD_NNNN` (e.g. `arc_20260522_0007`)
- **Cluster IDs** · `cl_YYYYMMDD_NNNN`
- **Source IDs** · slug form · `anadolu` · `cumhuriyet` · `resmi_gazete` · `kap`
- **Section headings in docs** · prefix with `❯`
- **Separator in prose** · middle dot `·` · never em or en dashes

---

## ❯ Build order for Claude Code

When scaffolding from cold, walk the modules in this order. Each step must produce a
green test suite before moving to the next.

1. `src/common/` · types · db connection · logging · config · settings
2. DuckDB schema migration · `scripts/init_db.py`
3. `src/ingest/sources.py` · static registry of sources with bands
4. `src/ingest/rss.py` · RSS ingestion · feedparser · stored to `raw_articles`
5. `src/ingest/resmi_gazete.py` · pdfplumber · daily PDF
6. `src/ingest/kap.py` · KAP scraping
7. `src/ingest/reddit.py` · PRAW
8. `src/ingest/poller.py` · orchestrates 4-7 with per-source isolation
9. `src/normalize/` · trafilatura · entity tagging
10. `src/cluster/` · bge-m3 via Ollama · clustering
11. `src/score/defcon.py` · constants · enums
12. `src/score/classify.py` · Qwen2.5 calls
13. `src/score/promotion.py` · band ceiling rules
14. `src/arcs/` · linking and state transitions
15. `src/writer/` · Trendyol-LLM briefing generation
16. `src/tts/` · Piper synthesis
17. `dashboard/` · FastAPI + Jinja + HTMX
18. `src/pipeline.py` · top-level entrypoint that runs all stages
19. `scripts/task_scheduler.xml` · Task Scheduler definition
20. Liveness probe and failure alert path

---

## ❯ Bootstrap period

The first 7 days of operation are a calibration window. The arc linker has no history,
the band corroboration rules need volume to activate, and the writer model is uncalibrated
on the operator's actual reading preferences.

During the bootstrap period:
- All DEFCON ceiling rules still apply
- All confidence tags are issued one level lower than computed (`YÜKSEK` becomes `ORTA`)
- Manual override is encouraged · the operator promotes/demotes via the dashboard
- After 7 days, the system reads its own override history and recalibrates

---

## ❯ Out of scope for v0.1

- Telegram ingestion (deferred indefinitely · per operator decision)
- X/Twitter ingestion (deferred · band slot reserved · ADR-003)
- Multi-language briefings (Turkish only)
- Push notifications outside the dashboard
- Cloud deployment of any kind
- Multi-user access
- Real-time alerts during the day (the system is strictly a nightly batch)

---

## ❯ Operator handover

When this scaffold is complete and the first nightly run succeeds, the operator owns:
- Curating the Reddit subreddit list and X strategy (when chosen)
- Reviewing and resolving open arcs daily via dashboard
- Sign-off on writer prose tone after the first week
- Tuning DEFCON anchors if real-world calibration drifts

MÜŞAHİT does not make decisions about Turkey. It synthesizes signal and presents it.
The operator decides what matters.
