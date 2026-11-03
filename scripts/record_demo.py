"""Bundle real run directories into the recorded demo (src/commitarena/demo).

Usage: uv run python scripts/record_demo.py RUN_DIR [RUN_DIR ...]
Only results produced by `commitarena run` are copied; nothing is edited.
"""

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "src" / "commitarena" / "demo"
TRAJ = ROOT / "docs" / "demo-run" / "trajectories"


NOTE_EN = (
    "Same harness, limits and sandbox for every model: 30 model calls and 1800 s per attempt, 4096 tokens per "
    "reply, temperature 0. Codestral repeated an earlier command verbatim in 170 of its 304 tool calls, despite "
    "163 repeat notices from the harness, and ran out of steps in 9 of 11 attempts; it never edited a test file. "
    "Full trajectories: github.com/diegoperezg7/commitarena/tree/main/docs/demo-run/trajectories"
)
NOTE_ES = (
    "Mismo arnés, límites y sandbox para todos: 30 llamadas y 1800 s por intento, 4096 tokens por respuesta, "
    "temperatura 0. Codestral repitió literalmente un comando anterior en 170 de sus 304 llamadas, pese a 163 "
    "avisos de repetición del arnés, y agotó los pasos en 9 de 11 intentos; no tocó ningún test. "
    "Trayectorias completas: github.com/diegoperezg7/commitarena/tree/main/docs/demo-run/trajectories"
)


def main(run_dirs: list[Path]) -> None:
    results, models, cutoffs, metas = [], set(), {}, []
    for d in run_dirs:
        meta = json.loads((d / "run.json").read_text())
        metas.append(meta)
        models.update(
            json.loads(x)["model"] for x in (d / "results.jsonl").read_text().splitlines() if x.strip()
        )
        cutoffs.update(meta.get("cutoffs", {}))
        results += [line for line in (d / "results.jsonl").read_text().splitlines() if line.strip()]
        TRAJ.mkdir(parents=True, exist_ok=True)
        for f in (d / "trajectories").glob("*.json"):
            shutil.copy(f, TRAJ / f.name)
    base = dict(metas[0])
    base.update(
        {
            "recorded": True,
            "repo": "https://github.com/andialbrecht/sqlparse",
            "repo_license": "BSD-3-Clause",
            "models": sorted(models),
            "cutoffs": cutoffs,
            "created_at": min(m["created_at"] for m in metas),
            "updated_at": max(m["updated_at"] for m in metas),
            "note_en": NOTE_EN,
            "note_es": NOTE_ES,
        }
    )
    DEMO.mkdir(parents=True, exist_ok=True)
    (DEMO / "run.json").write_text(json.dumps(base, indent=1))
    (DEMO / "results.jsonl").write_text("\n".join(results) + "\n")
    shutil.copy(run_dirs[0] / "tasks.json", DEMO / "tasks.json")
    print(f"{len(results)} results from {len(run_dirs)} run dirs -> {DEMO}")


if __name__ == "__main__":
    main([Path(p) for p in sys.argv[1:]])
