# Changelog

## 0.1.1 - 2026-11-03

- Report: the leaderboard fits at 1280 px (the "Ended by" column wraps instead of pushing "Cutoff" out of the
  card), and the page no longer overflows horizontally at 390 px; wide tables scroll inside their card.

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
- `commitarena demo`: recorded real run of 11 sqlparse (BSD-3-Clause) fixes with Mistral Large 4, Ministral 8B
  and Codestral, no keys or Docker needed.
- Agent loop details: older tool outputs are elided to fit a 16K window, exact repeated commands get a
  notice, truncated outputs say how to read specific lines, commands with a null byte are a failed step,
  empty or cut-off replies get up to two nudges.
- Python API (`run_task`, `load_tasks`, `ContextFile`, ...) with context-file injection for AGENTS.md /
  CLAUDE.md experiments.
