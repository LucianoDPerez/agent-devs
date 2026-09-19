#!/usr/bin/env python3
"""Pilot Polyglot (12 ejercicios Python) para AgentDevs con SLM local.

Metodologia (ver README.md del banco):
- Repo fresco por tarea en /tmp/poly-pilot/<id>, tests pre-creados y commiteados.
- El agente (Session EXECUTE, modelo fijado) implementa el modulo.
- Runner solo lee y verifica: python -m pytest tests/ -q (exit 0 = pass).
- 1 trial por tarea. Log completo por tarea en results/<id>/run.log.

Uso:
    python benchmarks/polyglot-pilot/run_pilot.py --list
    python benchmarks/polyglot-pilot/run_pilot.py P01
    python benchmarks/polyglot-pilot/run_pilot.py --all
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
BANK = Path(__file__).resolve().parent
TASKS_FILE = BANK / "tasks.json"
RESULTS = BANK / "results"
SUMMARY = RESULTS / "summary.jsonl"

MODEL = "spark2.5-4B"
BASE_URL = "http://localhost:8080/v1"
TASK_TIMEOUT_S = 900  # 15 min por tarea (el 4B tarda ~5-8 min/turno)


def load_tasks() -> list[dict]:
    return json.loads(TASKS_FILE.read_text(encoding="utf-8"))["tasks"]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, check=True)


def setup_repo(task: dict) -> Path:
    repo = Path(f"/tmp/poly-pilot/{task['id']}")
    shutil.rmtree(repo, ignore_errors=True)
    (repo / "tests").mkdir(parents=True)
    stem = Path(task["module"]).stem
    (repo / "tests" / f"test_{stem}.py").write_text(task["tests"], encoding="utf-8")
    (repo / "README.md").write_text(f"# {task['id']} {task['titulo']}\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init tests")
    return repo


def run_task(task: dict) -> dict:
    sys.path.insert(0, str(ROOT))
    from llm_wrapper import LocalLLM
    from orchestration.session import Session

    repo = setup_repo(task)
    res_dir = RESULTS / task["id"]
    shutil.rmtree(res_dir, ignore_errors=True)
    res_dir.mkdir(parents=True)
    tid = task["id"]
    t0 = time.time()
    llm = LocalLLM(base_url=BASE_URL, model_name=MODEL, temperature=0.2,
                    max_tokens=2048, api_key="not-needed")
    s = Session(llm, str(repo), cached_analysis="")
    boot = ""
    try:
        boot = str(s.start())
    except Exception as e:  # noqa: BLE001
        boot = f"START-ERROR: {e}"
    s._called_tools = set()
    try:
        s.run_turn(task["prompt"])
        finished = True
    except Exception as e:  # noqa: BLE001
        s._last_response = f"TASK-ERROR: {e}"
        finished = False
    secs = round(time.time() - t0, 1)
    tools = sorted(s._called_tools)
    # Verificación externa objetiva: pytest en el repo (lo que el agente dejó).
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "-q"],
            cwd=str(repo), capture_output=True, text=True, timeout=120,
        )
        passed = proc.returncode == 0
        verify_out = (proc.stdout + proc.stderr)[-2000:]
    except Exception as e:  # noqa: BLE001
        passed = False
        verify_out = f"VERIFY-ERROR: {e}"
    record = {
        "id": tid, "passed": passed, "finished": finished,
        "secs": secs, "tools": tools,
        "tokens": getattr(s, "_session_time", None),
        "model": MODEL,
    }
    (res_dir / "record.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    (res_dir / "verify.txt").write_text(verify_out, encoding="utf-8")
    (res_dir / "response.txt").write_text(s._last_response or "", encoding="utf-8")
    try:
        diff = subprocess.run(["git", "diff", "HEAD", "--stat"], cwd=str(repo),
                              capture_output=True, text=True, timeout=30).stdout
        (res_dir / "diffstat.txt").write_text(diff, encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    with SUMMARY.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    print(f"[{tid}] {'PASS' if passed else 'FAIL'} secs={secs} tools={tools}", flush=True)
    _ = boot
    return record


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("task_id", nargs="?", default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    tasks = load_tasks()
    if args.list:
        for t in tasks:
            print(f"{t['id']}  {t['titulo']}")
        return
    RESULTS.mkdir(parents=True, exist_ok=True)
    if args.all:
        recs = [run_task(t) for t in tasks]
        n = sum(1 for r in recs if r["passed"])
        print(f"TOTAL: {n}/{len(recs)} pass", flush=True)
    elif args.task_id:
        t = next(x for x in tasks if x["id"] == args.task_id)
        run_task(t)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
