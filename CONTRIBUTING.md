# Contributing

Thanks for helping. Keep changes small and focused.

## Setup

```bash
git clone https://github.com/diegoperezg7/commitarena && cd commitarena
uv sync
uv run pytest -q -m "not docker"   # fast, no Docker, no models
uv run pytest -q -m docker         # end-to-end with Docker and a scripted model
uv run ruff check src tests && uv run ruff format src tests
```

## Rules

- Tests never call paid models. Use the scripted client in `tests/conftest.py`.
- Anything that executes model-written code runs in the sandbox (`--network none`, CPU, memory and time limits).
- Keys come from environment variables only.
- Direct dependencies are pinned with `==` and `uv.lock` is committed.
- The harness is the same for every model: changes to the agent loop or prompt affect every result, so
  call them out in the PR and in CHANGELOG.md.
