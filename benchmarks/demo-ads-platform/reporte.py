#!/usr/bin/env python3
"""Genera REPORTE.md comparativo (por modelo) desde results/summary.jsonl.

Uso: python benchmarks/demo-ads-platform/reporte.py
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

BANK = Path(__file__).resolve().parent
SUMMARY = BANK / "results" / "summary.jsonl"
TASKS = json.loads((BANK / "tasks.json").read_text())["tasks"]
TITULOS = {t["id"]: t["titulo"] for t in TASKS}
ROLES = {t["id"]: t.get("rol", "?") for t in TASKS}


def load():
    by_model = defaultdict(dict)
    for ln in SUMMARY.read_text().splitlines():
        if not ln.strip():
            continue
        r = json.loads(ln)
        model = r.get("model", "unknown")
        # última corrida por modelo+tarea gana
        by_model[model][r["id"]] = r
    return by_model


def fmt(v, suffix=""):
    return f"{v}{suffix}" if v is not None else "—"


def main():
    if not SUMMARY.exists():
        print("No hay summary.jsonl aún")
        sys.exit(1)
    data = load()
    models = sorted(data.keys())

    lines = ["# Reporte — banco demo-ads-platform (ueno-ads)", ""]
    lines.append(f"Modelos evaluados: {', '.join('`%s`' % m for m in models)}")
    lines.append("")
    lines.append("## Tabla comparativa")
    lines.append("")
    header = "| tarea | rol | modelo | veredicto | tools | retries | tokens in/out | tiempo |"
    lines += [header, "|" + "---|" * 8]
    for tid in [t["id"] for t in TASKS]:
        for m in models:
            r = data[m].get(tid)
            if not r:
                continue
            metrics = r.get("metrics", {})
            verify = r.get("verify") or []
            vok = all(x["ok"] for x in verify) if verify else None
            verdict = "✅" if vok else ("❌" if vok is False else "n/a")
            role = metrics.get("role_final") or metrics.get("role_initial") or "—"
            lines.append(
                f"| {tid} | {ROLES.get(tid,'?')} | `{m}` | {verdict} "
                f"| {fmt(metrics.get('tool_calls'))} "
                f"| {fmt(metrics.get('retries'))} "
                f"| {fmt(metrics.get('tokens_in'))}/{fmt(metrics.get('tokens_out'))} "
                f"| {fmt(r.get('duration_s'), 's')} |"
            )
    lines.append("")

    # Totales por modelo
    lines.append("## Totales por modelo")
    lines.append("")
    lines.append("""
> **Lectura honesta del éxito**: las tareas se separan por tipo. Análisis/Plan
> (SL1-SL5) no tienen criterio automático: se cuentan como completadas si el
> turno terminó con respuesta (exit 0, sin timeout/error). Ejecución (SL6-SL7)
> tiene criterio OBJETIVO (el archivo existe y valida). La columna 'completadas'
> suma ambas categorías.
""".strip())
    lines.append("")
    lines.append("| modelo | análisis | ejecución | completadas | tool calls Σ | retries Σ | tokens out Σ | tiempo Σ |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for m in models:
        rs = list(data[m].values())
        n = len(rs)
        an = sum(1 for r in rs if not r.get("verify") and r.get("exit_code") in (0, None))
        ex = sum(1 for r in rs if (r.get("verify") and all(x["ok"] for x in r["verify"])))
        tools = sum((r.get("metrics") or {}).get("tool_calls") or 0 for r in rs)
        retries = sum((r.get("metrics") or {}).get("retries") or 0 for r in rs)
        tokout = sum((r.get("metrics") or {}).get("tokens_out") or 0 for r in rs)
        secs = sum(r.get("duration_s") or 0 for r in rs)
        lines.append(
            f"| `{m}` | {an}/5 | {ex}/2 | {an + ex}/{n} | {tools} | {retries} | {tokout:,} | {secs/60:.1f} min |"
        )
    lines.append("")

    out = BANK / "REPORTE.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"reporte generado: {out}")
    print("\n".join(lines))


if __name__ == "__main__":
    main()