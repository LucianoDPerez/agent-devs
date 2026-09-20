#!/usr/bin/env python3
"""Adapter Polyglot-225 (primeras 25) para AgentDevs con SLM local.

Copia fresca por tarea desde Aider-AI/polyglot-benchmark (/tmp), git init+commit
de setup, Session EXECUTE (Spark 4B) implementa el stub, verificación externa
con el comando del stack. 1 trial por tarea. Sin judge LLM.

Uso:
    python benchmarks/polyglot-225/run_225.py --list
    python benchmarks/polyglot-225/run_225.py PY01
    python benchmarks/polyglot-225/run_225.py --all
"""
from __future__ import annotations

import argparse
import json
import os as _os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
BANK = Path(__file__).resolve().parent
TASKS_FILE = BANK / "tasks25.json"
RESULTS = BANK / "results"
SUMMARY = RESULTS / "summary.jsonl"

# Parametrizable por CLI (ver main) o env — para correr en otra máquina con
# otro modelo/server sin tocar código. Defaults = bench box original.
MODEL = _os.environ.get("POLY225_MODEL", "spark2.5-4B")
BASE_URL = _os.environ.get("POLY225_BASE_URL", "http://localhost:8080/v1")
DATASET = Path(_os.environ.get("POLY225_DATASET", "/tmp/polyglot-benchmark"))
WORKDIR = Path(_os.environ.get("POLY225_WORKDIR", "/tmp/poly225"))
LANG_DIR = {"python": "python", "javascript": "javascript", "go": "go",
            "java": "java", "cpp": "cpp"}


def load_tasks() -> list[dict]:
    return json.loads(TASKS_FILE.read_text(encoding="utf-8"))["tasks"]


def _sh(cwd: Path, cmd: str, timeout: int = 600) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, shell=True, cwd=str(cwd), capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr)[-3000:]
    except Exception as e:  # noqa: BLE001
        return 99, f"SETUP-ERROR: {e}"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, check=True)


def setup_repo(task: dict) -> tuple[Path, Path, str]:
    """Copia el ejercicio a repo/<exercise>/ (NOMBRE ORIGINAL preservado:
    el CMakeLists de C++ deriva los filenames del nombre del directorio).
    Git init en el repo; setup y verify corren dentro del subdir."""
    src = DATASET / LANG_DIR[task["lang"]] / "exercises" / "practice" / task["exercise"]
    assert src.is_dir(), f"dataset ausente: {src}"
    repo = WORKDIR / task["id"]
    exdir = repo / task["exercise"]
    shutil.rmtree(repo, ignore_errors=True)
    shutil.copytree(src, exdir, ignore=shutil.ignore_patterns(".git"))
    (exdir / "gradlew").chmod(0o755) if (exdir / "gradlew").exists() else None
    setup_out = ""
    for cmd in task.get("setup", []):
        rc, out = _sh(exdir, cmd, timeout=600)
        setup_out += f"$ {cmd} -> {rc}\n"
        assert rc == 0, f"setup falló en {task['id']}: {cmd}\n{out}"
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")
    instr = (exdir / ".docs" / "instructions.md").read_text(encoding="utf-8")
    return repo, exdir, instr


def run_task(task: dict) -> dict:
    sys.path.insert(0, str(ROOT))
    from llm_wrapper import LocalLLM
    from orchestration.session import Session

    repo, exdir, instr = setup_repo(task)
    res_dir = RESULTS / task["id"]
    shutil.rmtree(res_dir, ignore_errors=True)
    res_dir.mkdir(parents=True)
    tid = task["id"]
    stubs = ", ".join(f"{task['exercise']}/{s}" for s in task["stub"])
    prompt = (
        f"implementar ({task['lang']}): en {exdir} "
        f"leé .docs/instructions.md y los tests; completá el stub ({stubs}) para "
        f"que `{task['test_cmd']}` (corrido dentro de {task['exercise']}/) pase "
        f"en verde. Verificá con las tools de verificación al final.\n\n"
        f"INSTRUCCIONES DEL EJERCICIO:\n{instr[:6000]}"
    )
    t0 = time.time()
    llm = LocalLLM(base_url=BASE_URL, model_name=MODEL, temperature=0.2,
                    max_tokens=2048, api_key="not-needed")
    s = Session(llm, str(repo), cached_analysis="")
    try:
        s.start()
    except Exception as e:  # noqa: BLE001
        print(f"[{tid}] START-ERROR: {e}", flush=True)
    s._called_tools = set()
    try:
        s.run_turn(prompt)
        finished = True
    except Exception as e:  # noqa: BLE001
        s._last_response = f"TASK-ERROR: {e}"
        finished = False
    secs = round(time.time() - t0, 1)
    rc, verify_out = _sh(exdir, task["test_cmd"], timeout=600)
    record = {"id": tid, "lang": task["lang"], "exercise": task["exercise"],
              "passed": rc == 0 and finished, "finished": finished,
              "secs": secs, "tools": sorted(s._called_tools), "model": MODEL}
    (res_dir / "record.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    (res_dir / "verify.txt").write_text(verify_out, encoding="utf-8")
    (res_dir / "response.txt").write_text(s._last_response or "", encoding="utf-8")
    with SUMMARY.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    print(f"[{tid}] {'PASS' if record['passed'] else 'FAIL'} secs={secs} tools={record['tools']}",
          flush=True)
    return record


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("task_id", nargs="?", default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--model", default=None,
                    help="Override del modelo (default: env POLY225_MODEL o spark2.5-4B)")
    ap.add_argument("--base-url", default=None,
                    help="Override del server (default: env POLY225_BASE_URL o :8080)")
    ap.add_argument("--dataset", default=None)
    ap.add_argument("--workdir", default=None)
    args = ap.parse_args()
    global MODEL, BASE_URL, DATASET, WORKDIR
    if args.model:
        MODEL = args.model
    if args.base_url:
        BASE_URL = args.base_url
    if args.dataset:
        DATASET = Path(args.dataset)
    if args.workdir:
        WORKDIR = Path(args.workdir)
    print(f"model={MODEL} base={BASE_URL} dataset={DATASET} workdir={WORKDIR}", flush=True)
    tasks = load_tasks()
    if args.list:
        for t in tasks:
            print(f"{t['id']}  {t['lang']:10s} {t['exercise']}")
        return
    RESULTS.mkdir(parents=True, exist_ok=True)
    if args.all:
        recs = [run_task(t) for t in tasks]
        n = sum(1 for r in recs if r["passed"])
        print(f"TOTAL: {n}/{len(recs)} pass", flush=True)
    elif args.task_id:
        run_task(next(x for x in tasks if x["id"] == args.task_id))
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
