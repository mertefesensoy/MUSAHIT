# MÜŞAHİT · operator tasks

Findings surfaced by the operator during real-world runs. The list is
the working backlog between formal build steps · tasks land here as
discovered, not as scheduled.

Three buckets:

* **Pending** — must address before the next build step (step 17+
  blocked on these)
* **First-month tuning** — operational refinements that can wait until
  enough run-history exists to inform the change
* **Resolved** — moved here when fixed, with the resolution date so the
  history is auditable

Add items as discovered. Keep entries one-liner short; if more context
is needed, link to a docs/implementations/ or docs/operator/ file.

---

## Pending · step-17 blockers

- [ ] Arc-link counter bug · 240 clusters all collided on arc_20260523_0001
- [ ] DEFCON-3 promotion broken · all 239 classified stories landed in
      DEFCON 4 · investigate ADR-005 promotion ceiling against real model
      outputs · was working in tests, fails on real data
- [ ] Trendyol left literal template placeholders in two sections
      ("[içerik buraya · şablon talimatlarına bak]") · validator missed
      it · either tighten validator to reject the placeholder string OR
      fix the writer prompt so the model doesn't echo template
      instructions
- [ ] Category enum has no buckets for natural disasters, weather,
      accidents · Qwen2.5 defaults to EKONOMİ for all of them · either
      add categories (DOĞAL_AFET, KAZA, HAVA, SAĞLIK) or document that
      mis-categorization to EKONOMİ is the known fallback
- [ ] DIPLOMASİ enum case-folding · Qwen2.5 returns undotted-I form

## First-month tuning

- [ ] 11 source failures in this run · file individually with the actual
      error message from SİSTEM LOG
- [ ] Resmi Gazete and tcmb timing out at 180s · either increase timeout
      or split fetch
- [ ] Reddit creds still not configured · this run's SİSTEM LOG doesn't
      show reddit at all (expected · SKIPPED path)


## Resolved

_(empty — populated as Pending / Tuning items get closed)_

<!--
Example shape:

- [x] 2026-05-23 · t24 RSS URL was wrong port · fixed in
      `musahit/ingest/sources.py` · verified next night
-->
