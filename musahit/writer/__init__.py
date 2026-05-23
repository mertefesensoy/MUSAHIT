"""Briefing writer stage.

Per ADR-002 (amended 2026-05-23) the writer model is Trendyol-LLM 7B
chat v1.8 pulled from Ollama Hub as
``serkandyck/trendyol-llm-7b-chat-v1.8-gguf``. Per ADR-009 the writer
produces a Markdown briefing with explicit section markers that the
TTS and dashboard stages downstream parse. Per ADR-012 the writer
retries on template validation failure up to 3 times and then falls
through to a deterministic Python-rendered briefing — the operator
always gets *something*.
"""
