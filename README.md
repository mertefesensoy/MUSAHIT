# MÜŞAHİT

> *"The Machine sees the whole. MÜŞAHİT sees the whole of Turkey."*

A personal OSINT system that tracks Turkish political, economic, judicial, security,
diplomatic, regulatory, and social events from open sources. Runs nightly between
01:00 and 07:00 on a Windows laptop. Produces a Turkish-language briefing at 07:00
as a local web dashboard and a Piper-voiced audio file.

Inspired by *Person of Interest*.

---

## ❯ Status

Foundation locked · scaffold pending · ADRs complete.

## ❯ Start here

1. Read `BOOTSTRAP.md` first
2. Read `adr/ADR-001-architecture-overview.md`
3. Walk the remaining ADRs in order
4. Then look at `src/` and `dashboard/`

## ❯ One-line summary

`musahit` ingests roughly 30 Turkish OSINT sources nightly, clusters them across
ideological bands, scores them on a Turkey-calibrated DEFCON 0-5 scale, links them into
ongoing story arcs, and writes a single Turkish briefing the operator reads with morning
coffee.

## ❯ Local stack

- Python 3.11+
- Ollama with Qwen2.5 7B + Trendyol-LLM 7B + bge-m3
- DuckDB
- FastAPI + Jinja2 + HTMX
- Piper TTS
- Windows Task Scheduler

## ❯ License

Personal use · not for redistribution · OSINT envelope only.
