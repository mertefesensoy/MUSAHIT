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

## Pending (must address before step 17)

_(empty — populated by the first real smoke run)_

<!--
Example shape (delete this comment block once real items land):

- [ ] Anadolu Ajansı RSS URL returns 404 · `musahit/ingest/sources.py` ·
      placeholder URL needs operator verification
- [ ] Trendyol-LLM produces empty header section on dry inputs ·
      `musahit/writer/prompt.py` · prompt may need a header reminder
- [ ] cluster stage exceeded its 60-minute soft budget · ADR-007 revisit
-->


## First-month tuning (address during operation)

_(empty — populated as run-history accumulates)_

<!--
Example shape:

- [ ] tune Reddit min_score from 50 → 75 if too noisy · operator review
      after 14 nights
- [ ] consider adding a 10th category if "POLİTİKA" bucket grows past
      40% of clusters · per ADR-016 trigger
-->


## Resolved

_(empty — populated as Pending / Tuning items get closed)_

<!--
Example shape:

- [x] 2026-05-23 · t24 RSS URL was wrong port · fixed in
      `musahit/ingest/sources.py` · verified next night
-->
