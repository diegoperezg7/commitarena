# Changelog

## 0.1.0 - 2026-11-03

First release.

- `commitarena mine`: finds commits that change source and tests (Python + pytest), validates in Docker that
  the new tests fail without the fix and pass with it (FAIL_TO_PASS) without breaking others (PASS_TO_PASS).
  The task prompt is built from the failing tests only; the commit message is never used.
- `commitarena run`: one minimal agent loop with a single `bash` tool for every model, via one
  OpenAI-compatible client (OpenRouter, OpenAI, Mistral, Ollama). Every command runs in a `--network none`
  container with CPU, memory and time limits. Test files are restored before scoring. `--max-cost`
  (default 1 USD) is checked before every call. Resumable run directories.
- `commitarena report`: static HTML report (EN/ES, light/dark, mobile): leaderboard, task x model matrix,
  pass rate before vs after each model's training cutoff, model diff next to the human fix.
- `commitarena demo`: recorded real run on sqlparse (BSD-3-Clause), no keys or Docker needed.
- Python API (`run_task`, `load_tasks`, `ContextFile`, ...) with context-file injection for AGENTS.md /
  CLAUDE.md experiments.
