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


def main(run_dirs: list[Path]) -> None:
    results, models, cutoffs, metas = [], set(), {}, []
    for d in run_dirs:
        meta = json.loads((d / "run.json").read_text())
        metas.append(meta)
        models.update(meta["models"])
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
        }
    )
    DEMO.mkdir(parents=True, exist_ok=True)
    (DEMO / "run.json").write_text(json.dumps(base, indent=1))
    (DEMO / "results.jsonl").write_text("\n".join(results) + "\n")
    shutil.copy(run_dirs[0] / "tasks.json", DEMO / "tasks.json")
    print(f"{len(results)} results from {len(run_dirs)} run dirs -> {DEMO}")


if __name__ == "__main__":
    main([Path(p) for p in sys.argv[1:]])
