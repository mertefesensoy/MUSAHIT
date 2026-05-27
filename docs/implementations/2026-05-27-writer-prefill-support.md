# Implementation: Writer Response Prefill Support

**Date** · 2026-05-27
**Author** · Claude Code
**ADR refs** · ADR-009, ADR-012

---

## ❯ Problem / Motivation

Angle A (template-at-end prompt reorder) improved attempt-0 content quality but Trendyol-LLM 7B still reliably skips the H1 document title and flattens the section hierarchy by promoting the first `##` section to `#` on heavy-day prompts. The model's opening tokens determine the structural trajectory of the entire generation — if it starts wrong, no amount of downstream instruction saves the output.

Response prefilling seeds the model's first tokens with a known-good prefix (document title + first section header), forcing the model to continue from the correct structural starting point.

---

## ❯ What Changed

| File | Description |
|---|---|
| `musahit/score/llm_client.py` | Added `generate_with_prefill` to `LlmClient` Protocol, `OllamaLlmClient` (via `/api/chat`), and `FakeLlmClient` (delegates to `generate` using `user` as prompt). |
| `musahit/writer/prompt.py` | Split `build_writer_prompt` into `build_writer_system()` and `build_writer_user(payload)`. Original function kept as deprecated thin wrapper. |
| `musahit/writer/briefer.py` | Migrated `_compose` from `generate()` to `generate_with_prefill()`. Prefill = `DOCUMENT_TITLE + "\n\n" + TEMPLATE_SECTIONS[0].marker + "\n\n"`. Continuation is concatenated with prefill before validation. |
| `tests/test_writer/test_briefer.py` | Adapted all existing tests to use continuations instead of full canned briefings. Added `TestPrefillWiring` with 2 tests. |
| `tests/test_writer/test_prompt.py` | Added `TestPromptSplit` with 3 tests verifying system/user split and backward-compatible wrapper. |

---

## ❯ Implementation Approach

**Response prefilling** is Ollama's chat-API equivalent of Anthropic's `assistant` prefill: a partial `assistant` message in the messages array causes the model to continue from that text. The Ollama `/api/chat` endpoint accepts this natively.

The `OllamaLlmClient.generate_with_prefill` method POSTs to `/api/chat` with three messages:
- `system`: the writer's role description
- `user`: the data payload + template + instructions
- `assistant`: the prefill (document title + first section marker)

The model's response is the continuation text only. The caller (briefer) prepends the prefill to reconstruct the full markdown before validation.

The existing `generate()` method and `/api/generate` endpoint are untouched — the score worker continues to use the simpler single-prompt API.

**Prompt split:** `build_writer_prompt` was monolithic. The new `build_writer_system()` / `build_writer_user()` split allows the briefer to pass system and user as separate messages in the chat API. The original function is preserved as a deprecated wrapper so existing prompt tests don't regress.

---

## ❯ Design Decisions

**`/api/chat` vs `/api/generate` with `system` parameter:** Ollama's `/api/generate` supports a `system` parameter but does not support assistant prefill. `/api/chat` with a messages array is the only way to inject a partial assistant response. A separate `_call_chat` method keeps the two HTTP call shapes isolated.

**FakeLlmClient delegation:** `generate_with_prefill` delegates to `generate(user)` rather than duplicating matching logic. This keeps the existing `_responses` / `_responder` patterns working unchanged. A `prefill_log` list enables test assertions on what prefill was passed.

**Continuation extraction in tests:** The test helper `_canned_continuation` finds the first section marker in the fallback briefing and returns everything after it. When the briefer prepends the prefill (title + marker), the combined text contains all sections in order and passes the validator.

---

## ❯ Verification

```powershell
# All writer + score tests pass (83 writer + 35 score = 118)
python -m pytest tests/test_writer/ tests/test_score/ -v

# Full suite (718 tests)
python -m pytest tests/ -v

# ruff
python -m ruff check musahit/score/llm_client.py musahit/writer/prompt.py musahit/writer/briefer.py
```

Operator smoke test (post-merge):
```powershell
python -m musahit.pipeline run --date 2026-05-27 --stage write --force
```

---

## ❯ Related Docs

- ADR-009 (writer discipline rules)
- ADR-012 (stage 6 writer always-ships)
- `docs/implementations/2026-05-27-writer-prompt-reorder.md`
