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
import re
import shutil
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
TURN_TIMEOUT = 2400  # override: --turn-timeout

DEFAULT_BANK = "medicos"
BANKS_DIR = ROOT / "benchmarks"
MODEL_LABEL = "unknown"
RESTORE_AFTER = False

# ── Métricas por caso (4B vs 9B study) ─────────────────────────────────────
_ROLE_RE = re.compile(r"\[(📋 Planificación|🔍 Análisis|🛠️\s*Ejecución|🔎\s*Review)\]")
_TOOL_RE = re.compile(r"🔧\s*(\w+)")
_SESSION_RE = re.compile(
    r"Sesión:\s*([\d.,]+)s\s*\|\s*([\d.,]+)\s*tokens?\s*"
    r"\(in\s+([\d,]+)\s*\+\s*out\s+([\d,]+)",
    re.IGNORECASE,
)


def _num(s: str) -> int:
    return int((s or "0").replace(",", "").replace(".", "") or 0)


def extract_metrics(log_text: str) -> dict:
    """Métricas objetivas de un run.log del agente.

    role_routed, tool_calls (total + por nombre), retries, timeouts de
    razonamiento, budget agotado y tokens/tiempo de sesión (del resumen final).
    """
    from collections import Counter

    tools = _TOOL_RE.findall(log_text)
    banners = _ROLE_RE.findall(log_text)
    role = re.search(r"[^\n]*$", "")  # placeholder no-op
    role_first = banners[0] if banners else None
    sess = _SESSION_RE.search(log_text)
    return {
        "role_initial": role_first,
        "role_final": banners[-1].strip() if banners else None,
        "role_switches": max(0, len(banners) - 1),
        "tool_calls": len(tools),
        "tools_by_name": dict(Counter(tools)),
        "retries": log_text.count("Reintentando"),
        "reasoning_timeouts": log_text.count("sin producir output"),
        "budget_exhausted": "agotado" in log_text.lower(),
        "session_secs": _num(sess.group(1)) if sess else None,
        "tokens_in": _num(sess.group(3)) if sess else None,
        "tokens_out": _num(sess.group(4)) if sess else None,
    }


def load_tasks() -> list[dict]:
    return json.loads(TASKS_FILE.read_text(encoding="utf-8"))["tasks"]


def resolve_bank(bank: str | None) -> tuple[Path, Path, str]:
    """Resuelve tasks/results/repo para un banco.
    Banco 'medicos' (default) → benchmarks/tasks.json + benchmarks/results.
    Banco custom → benchmarks/<bank>/tasks.json + benchmarks/<bank>/results.
    El repo se lee del campo 'repo' del tasks.json (override: env BENCH_REPO)."""
    global TASKS_FILE, RESULTS_DIR, SUMMARY, REPO
    import os
    if bank is None or bank == DEFAULT_BANK:
        TASKS_FILE = ROOT / "benchmarks" / "tasks.json"
        RESULTS_DIR = ROOT / "benchmarks" / "results"
    else:
        bank_dir = BANKS_DIR / bank
        TASKS_FILE = bank_dir / "tasks.json"
        RESULTS_DIR = bank_dir / "results"
    SUMMARY = RESULTS_DIR / "summary.jsonl"
    try:
        data = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
        REPO = os.environ.get("BENCH_REPO") or data.get("repo") or REPO
    except (OSError, json.JSONDecodeError):
        pass
    return TASKS_FILE, RESULTS_DIR, REPO


def git_capture(repo: str, *args: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True, text=True, timeout=60,
        )
        return out.stdout.strip() + ("\n" + out.stderr.strip() if out.stderr.strip() else "")
    except Exception as e:
        return f"(git error: {e})"


def run_criteria(repo: str, criterio: list[dict], timeout: int = VERIFY_TIMEOUT) -> list[dict]:
    """Ejecuta los criterios de verificación y devuelve resultados detallados."""
    results = []
    for crit in criterio:
        label = crit["label"]
        cmd_v = crit["cmd"]
        try:
            vp = subprocess.run(cmd_v, shell=True, cwd=repo, capture_output=True, text=True, timeout=timeout)
            results.append({
                "label": label,
                "cmd": cmd_v,
                "exit": vp.returncode,
                "ok": vp.returncode == 0,
                "tail": (vp.stdout or "")[-1500:] + (vp.stderr or "")[-1500:],
            })
        except subprocess.TimeoutExpired:
            results.append({"label": label, "cmd": cmd_v, "exit": "TIMEOUT", "ok": False, "tail": ""})
    return results


_ABSENCE_MARKERS = (
    "no such file", "no tests found", "does not exist", "no such directory",
    "command not found", "cannot access", "cannot find module", "no go files",
    "directory not found",
)
_BUILD_MARKERS = ("build failed", "compilation failed", "cannot find symbol", "error:")


def _failure_kind(tail: str) -> str:
    """Clasifica un tail de fallo: 'absence' (el artefacto/test NO EXISTE),
    'build' (compilación/lint), 'test' (tests que fallan) u 'other'."""
    t = (tail or "").lower()
    if any(m in t for m in _ABSENCE_MARKERS):
        return "absence"
    if any(m in t for m in _BUILD_MARKERS):
        return "build"
    if " failed" in t or "--- fail" in t:
        return "test"
    return "other"


def _normalize_tail(tail: str) -> str:
    """Normaliza un tail para comparar fallos: minúsculas, sin dígitos
    (timestamps, líneas, duraciones varían) y whitespace colapsado."""
    t = (tail or "").lower()
    t = re.sub(r"\d+", "", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def _failing_tests(tail: str) -> frozenset[str]:
    """Extrae el set de tests fallidos del tail (gradle, pytest, go)."""
    t = (tail or "").lower()
    names = set()
    for m in re.finditer(r"(?m)^\s*[^>\n]+ > [^>\n]+? failed\s*$", t):
        names.add(m.group(0).strip())
    for m in re.finditer(r"---\s*fail:\s*([\w./]+)", t):
        names.add(m.group(1))
    for m in re.finditer(r"^failed\s+([\w./:\[\]()-]+::[\w.\[\]()-]+)", t):
        names.add(m.group(1))
    return frozenset(names)


def _same_failure(base: dict, now: dict) -> bool:
    """True si el fallo actual es el MISMO que el baseline (heredado).

    Nunca se hereda un fallo de tipo AUSENCIA ("no tests found", "no such
    file"): si el artefacto no existía antes y sigue sin existir, el agente no
    entregó → error del agente. Solo se heredan fallos CONDUCTUALES (build/test)
    idénticos en exit, clase y —cuando es extraíble— set de tests fallidos.
    Si no se puede clasificar, NO se hereda: el harness falla alto y el humano
    decide."""
    if base.get("exit") != now.get("exit"):
        return False
    b = base.get("tail") or ""
    n = now.get("tail") or ""
    bk = _failure_kind(b)
    nk = _failure_kind(n)
    if bk == "absence" or nk == "absence":
        return False
    if bk != nk:
        return False
    if bk in ("build", "test"):
        bt = _failing_tests(b)
        nt = _failing_tests(n)
        if bt or nt:
            return bool(bt) and bt == nt
        return _normalize_tail(b) == _normalize_tail(n)
    # "other": sin señales comparables (tails vacíos, greps de conteo...).
    # NO se hereda — el harness falla alto y el humano decide.
    return False


def baseline_block(results: list[dict]) -> str:
    if not results:
        return ""
    lines = ["BASELINE (estado del repo ANTES de que arranques — el sistema ya lo verificó):"]
    for r in results:
        state = "PASA" if r["ok"] else f"FALLA (exit={r['exit']})"
        lines.append(f"- {r['label']}: {state}")
    failed = [r for r in results if not r["ok"]]
    if failed:
        lines.append("")
        lines.append("⚠️  El baseline YA fallaba en estos criterios. Dos casos DISTINTOS:")
        lines.append("    - AUSENCIA (el tail dice 'No tests found', 'No such file', etc.): NO es heredado.")
        lines.append("      Es EXACTAMENTE lo que tu tarea debe crear. Si al final sigue fallando igual,")
        lines.append("      contará como error tuyo.")
        lines.append("    - Error CONDUCTUAL (build/test falla en código existente): es HEREDADO si el")
        lines.append("      verify final falla por lo MISMO y no tocaste el archivo del error. Documentalo")
        lines.append("      (archivo/clase) y NO entres en loop intentando arreglarlo.")
    return "\n".join(lines)


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
        "rol_esperado": task.get("rol"),
        "model": MODEL_LABEL,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    print(f"\n{'=' * 70}\n▶ {tid} [{task['nivel']}] {task['titulo']}\n{'=' * 70}")

    record["git_pre"] = git_capture(REPO, "status", "--short")

    # SNAPSHOT DE SEGURIDAD: contenido pre-tarea de todo archivo trackeado
    # modificado + lista untracked. --restore-after vuelve a ESTE estado
    # exacto (sin tocar HEAD ni el resto del working tree).
    pre_dir = out_dir / "pre_snapshot"
    dirty_pre = [ln[3:] for ln in record["git_pre"].splitlines() if ln.strip() and not ln.startswith("??")]
    untracked_pre = {ln[3:] for ln in record["git_pre"].splitlines() if ln.startswith("??")}
    for rel in dirty_pre:
        src = Path(REPO) / rel
        if src.is_file():
            dst = pre_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    # BASELINE: verificación del estado inicial ANTES de que el agente toque nada.
    # Permite distinguir fallos hereditarios (ya fallaban antes) de fallos del agente.
    baseline = run_criteria(REPO, task.get("criterio", []))
    record["baseline"] = baseline
    (out_dir / "baseline.json").write_text(json.dumps(baseline, ensure_ascii=False, indent=2), encoding="utf-8")
    baseline_ok = all(r["ok"] for r in baseline)
    print(f"BASELINE: {'✅ verde' if baseline_ok else '❌ ya fallaba — ver baseline.json'}")

    prompt = task["prompt"]
    base_block = baseline_block(baseline)
    if base_block:
        prompt = prompt + "\n\n" + base_block
    # ensure_ascii=False CRÍTICO: con el default, 'creá' llega al agente como
    # el literal 'cre\u00e1' → el router no matchea verbos con acentos y TODO
    # prompt de ejecución se enruta a ANALYZE (benchmark v1 completo invalidado
    # por esto: los 'execute' eran reintentos write-only sobre artefactos
    # heredados).
    cmd = f'echo {json.dumps(prompt, ensure_ascii=False)} | {sys.executable} main.py "{REPO}"'
    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=ROOT, capture_output=True, text=True, timeout=TURN_TIMEOUT,
        )
        record["exit_code"] = proc.returncode
        record["stdout_tail"] = proc.stdout[-4000:]
        record["stderr_tail"] = proc.stderr[-2000:]
        (out_dir / "run.log").write_text(proc.stdout + "\n=== STDERR ===\n" + proc.stderr, encoding="utf-8")
    except subprocess.TimeoutExpired as e:
        # En timeout, subprocess mata el proceso: lo que alcanzó a emitir vive
        # en e.stdout/e.stderr — capturarlo para no perder la evidencia.
        record["exit_code"] = f"TIMEOUT({TURN_TIMEOUT}s)"
        partial_out = (e.stdout or b"").decode("utf-8", errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        record["stdout_tail"] = partial_out[-4000:]
        record["stderr_tail"] = ""
        (out_dir / "run.log").write_text(partial_out + f"\n=== TIMEOUT {TURN_TIMEOUT}s ===\n", encoding="utf-8")
    record["duration_s"] = round(time.monotonic() - started, 1)
    record["metrics"] = extract_metrics(record.get("stdout_tail") or "")
    if record.get("rol_esperado"):
        esperado = {"analyze": "Análisis", "plan": "Planificación",
                    "execute": "Ejecución", "review": "Review"}.get(record["rol_esperado"], "")
        roles = [r for r in (record["metrics"].get("role_initial"),
                             record["metrics"].get("role_final")) if r]
        record["metrics"]["role_match"] = any(esperado.lower() in r.lower() for r in roles)

    record["git_post"] = git_capture(REPO, "status", "--short")
    diff_stat = git_capture(REPO, "diff", "--stat")
    record["diff_stat"] = diff_stat
    (out_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    (out_dir / "git_pre.txt").write_text(record["git_pre"], encoding="utf-8")
    (out_dir / "git_post.txt").write_text(record["git_post"] + "\n\n=== DIFF STAT ===\n" + diff_stat, encoding="utf-8")

    # Verificación externa POST-tarea
    verify_results = run_criteria(REPO, task.get("criterio", []))
    record["verify"] = verify_results
    (out_dir / "verify.json").write_text(json.dumps(verify_results, ensure_ascii=False, indent=2), encoding="utf-8")

    # AUTO-RETRY: si el agente terminó pero el verify falló en un criterio que
    # NO es un fallo heredado idéntico (regresión sobre baseline verde, o
    # criterio de AUSENCIA sin entregar), re-lanzamos UN intento inyectándole
    # el fallo exacto. Los heredados conductuales no se reintentan.
    failed_now = [r for r in verify_results if not r["ok"]]
    retriable = []
    for r in failed_now:
        base = next((b for b in baseline if b["label"] == r["label"] and not b["ok"]), None)
        if not (base and _same_failure(base, r)):
            retriable.append(r)
    if retriable and not record.get("retried"):
        print("🔄 Auto-retry: verify falló en criterios que el baseline tenía verde → re-lanzando con el fallo inyectado...")
        feedback = "\n\n".join(
            f"VERIFY FALLÓ (criterio: {r['label']}, exit={r['exit']}):\n{r['tail'][-1200:]}"
            for r in retriable
        )
        retry_prompt = (
            prompt
            + "\n\n⛔ RETRY: la verificación externa falló en lo que acabás de hacer.\n"
            + feedback
            + "\n\nCorregí el problema concreto (edit_file del archivo que falla) y verificá de nuevo "
            "con run_lint + run_tests + run_build. Si el error está en un archivo que NO tocaste, "
            "reportalo como HEREDADO y detenete — no lo arregles."
        )
        retry_cmd = f'echo {json.dumps(retry_prompt)} | {sys.executable} main.py "{REPO}"'
        try:
            rp = subprocess.run(retry_cmd, shell=True, cwd=ROOT, capture_output=True, text=True, timeout=2400)
            (out_dir / "retry.log").write_text(rp.stdout + "\n=== STDERR ===\n" + rp.stderr, encoding="utf-8")
            record["retry_exit"] = rp.returncode
        except subprocess.TimeoutExpired:
            record["retry_exit"] = "TIMEOUT"
            (out_dir / "retry.log").write_text("TIMEOUT en retry", encoding="utf-8")
        verify_results = run_criteria(REPO, task.get("criterio", []))
        record["verify"] = verify_results
        (out_dir / "verify.json").write_text(json.dumps(verify_results, ensure_ascii=False, indent=2), encoding="utf-8")
        record["retried"] = True

    # passed: todos los criterios verdes, salvo los que YA fallaban en baseline
    # con el MISMO error conductual (heredados → se documentan pero no cuentan
    # como fallo). Un criterio de AUSENCIA (el artefacto no existía y sigue sin
    # existir) NUNCA es heredado: es el entregable de la tarea → error del agente.
    failed_now = []
    for v in verify_results:
        if v["ok"]:
            continue
        base = next((b for b in baseline if b["label"] == v["label"] and not b["ok"]), None)
        if base and _same_failure(base, v):
            continue  # heredado: mismo error conductual que en baseline
        failed_now.append(v)
    passed = not failed_now and "TIMEOUT" not in str(record.get("exit_code"))
    record["passed"] = passed
    record["errores_heredados"] = [
        b["label"] for b in baseline
        if not b["ok"] and _failure_kind(b.get("tail") or "") in ("build", "test")
    ]
    record["criterios_sin_entregar"] = [
        b["label"] for b in baseline
        if not b["ok"] and _failure_kind(b.get("tail") or "") in ("absence", "other")
    ]
    record["errores_agente"] = [v["label"] for v in failed_now]
    record["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    with SUMMARY.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    status = "✅ PASÓ" if passed else "❌ FALLÓ"
    print(f"{status} — {record['duration_s']}s")
    for v in verify_results:
        if v["ok"]:
            mark = "✅"
        elif v["label"] in [x["label"] for x in failed_now]:
            mark = "❌ (error del agente)"
        else:
            mark = "⏭️ heredado (mismo error que baseline)"
        print(f"   verify[{v['label']}]: exit={v['exit']} {mark}")
    if RESTORE_AFTER:
        restored = []
        for rel in dirty_pre:
            snap = pre_dir / rel
            if snap.exists():
                shutil.copy2(snap, Path(REPO) / rel)
                restored.append(rel)
        post_untracked = {
            ln[3:] for ln in git_capture(REPO, "status", "--porcelain").splitlines()
            if ln.startswith("??")
        }
        removed = []
        for rel in sorted(post_untracked - untracked_pre):
            f = Path(REPO) / rel
            if f.is_file():
                f.unlink()
                removed.append(rel)
        record["restored"] = {"tracked": restored, "untracked_removed": removed}
        print(f"🧹 Working tree restaurado al estado pre-tarea ({len(restored)} tracked, {len(removed)} untracked).")

    return record


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("task_id", nargs="?", default=None)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--bank", default=None,                    help="Banco de tareas: 'medicos' (default, compat) o subcarpeta de benchmarks/")
    ap.add_argument("--step", metavar="PROMPT", default=None,
                    help="Corre UN turno libre del agente con el prompt dado (micro-paso)")
    ap.add_argument("--step-label", default="step", help="Nombre del micro-paso")
    ap.add_argument("--model", default=None,
                    help="Etiqueta del modelo para los resultados (default: detecta del server)")
    ap.add_argument("--fresh-analysis", action="store_true",
                    help="Limpia el análisis cacheado del repo antes de la corrida (regenera canónico)")
    ap.add_argument("--restore-after", action="store_true",
                    help="Restaura el working tree del repo al estado pre-tarea tras CADA tarea (destructivo con cambios propios no commiteados)")
    ap.add_argument("--turn-timeout", type=int, default=2400,
                    help="Timeout por turno del agente en segundos (default 2400)")
    args = ap.parse_args()

    resolve_bank(args.bank)

    global MODEL_LABEL, RESTORE_AFTER, TURN_TIMEOUT
    TURN_TIMEOUT = args.turn_timeout
    RESTORE_AFTER = args.restore_after
    if args.fresh_analysis:
        import sqlite3 as _sq
        from cache import CACHE_DB
        conn = _sq.connect(CACHE_DB)
        cur = conn.execute("DELETE FROM repos WHERE path = ?", (REPO,))
        conn.commit()
        print(f"🧹 Análisis cacheado limpiado para {REPO} ({cur.rowcount} fila(s))")
    if args.model:
        MODEL_LABEL = args.model
    elif MODEL_LABEL == "unknown":
        try:
            from llm_wrapper import detect_server_model
            from config import LLM_BASE_URL
            MODEL_LABEL = detect_server_model(LLM_BASE_URL) or "unknown"
        except Exception:
            pass

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
