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
    "Same harness, limits and sandbox for every model (30 model calls, 1800 s per attempt, 4096 tokens per reply). "
    "mistral-small-latest was also planned but the API answered 429 for the whole session, so it is not shown. "
    "ollama/qwen3:4b-16k ran on 3 of the 11 tasks: in all 3 it spent every reply's 4096 tokens thinking and never "
    "called the tool (16 to 21 minutes per task on a shared laptop), so the run was stopped there."
)
NOTE_ES = (
    "Mismo arnés, límites y sandbox para todos los modelos (30 llamadas, 1800 s por intento, 4096 tokens por respuesta). "
    "También estaba previsto mistral-small-latest, pero la API respondió 429 toda la sesión y no aparece. "
    "ollama/qwen3:4b-16k corrió 3 de las 11 tareas: en las 3 gastó los 4096 tokens de cada respuesta pensando y nunca "
    "llamó a la herramienta (de 16 a 21 minutos por tarea en un portátil compartido), así que la ejecución se paró ahí."
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
