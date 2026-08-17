#!/usr/bin/env python3
"""Runner de benchmark: ejecuta tareas del agente (main.py) contra un repo
externo, captura evidencia (logs, git status/diff, verificación externa) y
registra resultados. NUNCA modifica el repo externo directamente: todo cambio
lo hace el agente; este runner solo LEE (git status/diff) y EJECUTA los
criterios de verificación (npm test/lint/build) para probar el resultado.

Uso:
    python benchmarks/run.py --list
    python benchmarks/run.py <task_id>          # corre una tarea
    python benchmarks/run.py --all              # corre todas en serie
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = "/Users/luchop/PROYECTOS IA/Medicos"
RESULTS_DIR = ROOT / "benchmarks" / "results"
TASKS_FILE = ROOT / "benchmarks" / "tasks.json"
SUMMARY = RESULTS_DIR / "summary.jsonl"

VERIFY_TIMEOUT = 300


def load_tasks() -> list[dict]:
    return json.loads(TASKS_FILE.read_text(encoding="utf-8"))["tasks"]


def git_capture(repo: str, *args: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True, text=True, timeout=60,
        )
        return out.stdout.strip() + ("\n" + out.stderr.strip() if out.stderr.strip() else "")
    except Exception as e:
        return f"(git error: {e})"


def run_task(task: dict, force: bool = False) -> dict:
    tid = task["id"]
    out_dir = RESULTS_DIR / tid
    out_dir.mkdir(parents=True, exist_ok=True)
    log_file = out_dir / "run.log"
    if log_file.exists() and not force:
        print(f"⏭  {tid} ya tiene resultado. Usá --force para re-correr.")
        return {"id": tid, "skipped": True}

    record = {
        "id": tid,
        "nivel": task["nivel"],
        "titulo": task["titulo"],
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    print(f"\n{'=' * 70}\n▶ {tid} [{task['nivel']}] {task['titulo']}\n{'=' * 70}")

    record["git_pre"] = git_capture(REPO, "status", "--short")

    prompt = task["prompt"]
    cmd = f'echo {json.dumps(prompt)} | {sys.executable} main.py "{REPO}"'
    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=ROOT, capture_output=True, text=True, timeout=2400,
        )
        record["exit_code"] = proc.returncode
        record["stdout_tail"] = proc.stdout[-4000:]
        record["stderr_tail"] = proc.stderr[-2000:]
        (out_dir / "run.log").write_text(proc.stdout + "\n=== STDERR ===\n" + proc.stderr, encoding="utf-8")
    except subprocess.TimeoutExpired:
        record["exit_code"] = "TIMEOUT(2400s)"
        record["stdout_tail"] = ""
        record["stderr_tail"] = ""
        (out_dir / "run.log").write_text("TIMEOUT: el turno del agente excedió 2400s", encoding="utf-8")
    record["duration_s"] = round(time.monotonic() - started, 1)

    record["git_post"] = git_capture(REPO, "status", "--short")
    diff_stat = git_capture(REPO, "diff", "--stat")
    record["diff_stat"] = diff_stat
    (out_dir / "git_pre.txt").write_text(record["git_pre"], encoding="utf-8")
    (out_dir / "git_post.txt").write_text(record["git_post"] + "\n\n=== DIFF STAT ===\n" + diff_stat, encoding="utf-8")

    # Verificación externa: criterios de éxito (solo ejecución, no modifica archivos)
    verify_results = []
    for crit in task.get("criterio", []):
        label = crit["label"]
        cmd_v = crit["cmd"]
        try:
            vp = subprocess.run(cmd_v, shell=True, cwd=REPO, capture_output=True, text=True, timeout=VERIFY_TIMEOUT)
            verify_results.append({
                "label": label,
                "cmd": cmd_v,
                "exit": vp.returncode,
                "ok": vp.returncode == 0,
                "tail": (vp.stdout or "")[-1500:] + (vp.stderr or "")[-1500:],
            })
        except subprocess.TimeoutExpired:
            verify_results.append({"label": label, "cmd": cmd_v, "exit": "TIMEOUT", "ok": False, "tail": ""})
    record["verify"] = verify_results
    (out_dir / "verify.json").write_text(json.dumps(verify_results, ensure_ascii=False, indent=2), encoding="utf-8")

    passed = all(v["ok"] for v in verify_results) and "TIMEOUT" not in str(record.get("exit_code"))
    record["passed"] = passed
    record["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    with SUMMARY.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    status = "✅ PASÓ" if passed else "❌ FALLÓ"
    print(f"{status} — {record['duration_s']}s")
    for v in verify_results:
        print(f"   verify[{v['label']}]: exit={v['exit']} {'✅' if v['ok'] else '❌'}")
    return record


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("task_id", nargs="?", default=None)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--step", metavar="PROMPT", default=None,
                    help="Corre UN turno libre del agente con el prompt dado (micro-paso)")
    ap.add_argument("--step-label", default="step", help="Nombre del micro-paso")
    args = ap.parse_args()

    if args.step:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        out_dir = RESULTS_DIR / "steps"
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        label = args.step_label.replace(" ", "_")
        log_file = out_dir / f"{ts}-{label}.log"
        print(f"\n{'=' * 70}\n▶ STEP [{label}]\n{'=' * 70}")
        cmd = f'echo {json.dumps(args.step)} | {sys.executable} -u main.py "{REPO}"'
        started = time.monotonic()
        import os
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        # Stream directo al archivo: visibilidad en vivo mientras el turno corre
        # (communicate() bloquea y solo escribía el log al final).
        with log_file.open("w", encoding="utf-8") as fh:
            proc = subprocess.Popen(
                cmd, shell=True, cwd=ROOT, env=env,
                stdout=fh, stderr=subprocess.STDOUT, text=True,
            )
            try:
                proc.wait(timeout=2400)
                exit_code = proc.returncode
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                exit_code = "TIMEOUT"
                fh.write("\nTIMEOUT 2400s\n")
        print(f"exit={exit_code} — {round(time.monotonic() - started, 1)}s — log: {log_file}")
        print("git post:")
        print(git_capture(REPO, "status", "--short"))
        return

    tasks = load_tasks()
    if args.list:
        for t in tasks:
            print(f"{t['id']:<6} [{t['nivel']:<11}] {t['titulo']}")
        return

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    if args.all:
        for t in tasks:
            try:
                run_task(t, force=args.force)
            except KeyboardInterrupt:
                print("\nInterrumpido por el usuario.")
                break
        return

    task = next((t for t in tasks if t["id"] == args.task_id), None)
    if not task:
        print(f"Tarea no encontrada: {args.task_id}. Usá --list.")
        sys.exit(1)
    run_task(task, force=args.force)


if __name__ == "__main__":
    main()
