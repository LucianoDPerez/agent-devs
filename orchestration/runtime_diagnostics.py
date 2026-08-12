"""Diagnóstico determinístico de runtime: quién escucha los puertos de dev.

El modelo chico no puede descubrir problemas de ENTORNO (un container Docker
con código viejo pisando el puerto del dev server, un backend caído, etc.)
— no tiene shell y el error handler del backend esconde la causa real
("Error interno del servidor" sin detalle, E2E real: POST 500 por schema
viejo en el container, mientras el código del repo era correcto).

El SISTEMA compara los puertos de dev del stack detectado con quién los
escucha (lsof) y reporta: container Docker vs proceso local vs nadie.

Escalable: funciona para cualquier repo/stack (los puertos se detectan de
los archivos de config: vite.config, server.ts, app.py, main.go...).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

# Puertos de dev típicos por tipo de proceso (defaults si no se detectan)
_DEFAULT_PORTS = [3000, 5173, 8000, 8080, 5000]

_PORT_PATTERNS = [
    # vite.config: port: 5173 / server.port = 3000
    re.compile(r"(?:port\s*[:=]\s*)(\d{4,5})"),
    # server.ts / app.ts: listen(PORT, ...) / .listen(3000)
    re.compile(r"\.listen\s*\(\s*(?:\w+\s*,\s*)?(\d{4,5})"),
    # app.run(port=8000) / uvicorn.run(port=...)
    re.compile(r"(?:port\s*=\s*)(\d{4,5})"),
    # Go: :8080
    re.compile(r"(?:\":|:)(\d{4,5})\""),
]


def _detect_ports(repo_path: str) -> list[int]:
    """Puertos de dev del stack: regex sobre archivos de config + defaults."""
    found: list[int] = []
    root = Path(repo_path)
    if not root.is_dir():
        return list(_DEFAULT_PORTS)
    # Configs típicos: raíz + subdirs frontend/backend/web/server/api
    config_names = (
        "vite.config.ts", "vite.config.js", "vite.config.mjs",
        "server.ts", "app.ts", "app.py", "main.py", "main.go",
        "src/server.ts", "src/app.py",
    )
    targets: list[Path] = []
    for sub in ("", "frontend", "web", "client", "backend", "server", "api", "src"):
        for name in config_names:
            p = root / sub / name
            if p.is_file():
                targets.append(p)
    for p in targets[:12]:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for pat in _PORT_PATTERNS:
            for m in pat.finditer(text):
                port = int(m.group(1))
                if 1000 <= port <= 65535:
                    found.append(port)
    return sorted(set(found)) or list(_DEFAULT_PORTS)


def _parse_lsof(text: str) -> list[dict]:
    """Parsea la salida de `lsof -nP -iTCP:<port> -sTCP:LISTEN`.
    Separado para unit tests."""
    rows: list[dict] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] != "COMMAND":
            rows.append({"pid": parts[1], "command": parts[0]})
    return rows


def _lsof_listeners(port: int) -> list[dict]:
    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=10,
        )
        return _parse_lsof(out.stdout or "")
    except Exception:
        return []


def _is_docker(row: dict) -> bool:
    return "docke" in row["command"].lower()


def detect_runtime_issues(
    repo_path: str,
    *,
    ports: list[int] | None = None,
    lsof_fn=None,
) -> str:
    """Compara los puertos de dev del stack con quién los escucha.

    Devuelve '' si todo está normal, o un bloque de hallazgos con el fix
    sugerido (frenar container, arrancar el dev server...).
    """
    if ports is None:
        ports = _detect_ports(repo_path)
    lsof = lsof_fn or _lsof_listeners

    findings: list[str] = []
    for port in ports:
        rows = lsof(port)
        if not rows:
            findings.append(
                f"  - Puerto {port}: NADIE lo escucha — el dev server de ese "
                f"servicio no está corriendo. Arrancalo (npm run dev / "
                f"uvicorn / go run)."
            )
            continue
        dockers = [r for r in rows if _is_docker(r)]
        locals_ = [r for r in rows if not _is_docker(r)]
        if dockers and locals_:
            findings.append(
                f"  - Puerto {port}: CONFLICTO — un container Docker "
                f"({dockers[0].get('command')}) y un proceso local "
                f"({locals_[0].get('command')}) compiten. El tráfico puede ir "
                f"al container con código viejo. Frená el container: "
                f"docker stop <nombre>."
            )
        elif dockers:
            findings.append(
                f"  - Puerto {port}: lo ocupa un container DOCKER "
                f"({dockers[0].get('command')}) — el dev server local NO pudo "
                f"bindear. Todo el tráfico va a código viejo dentro del "
                f"container. Frenalo: docker stop <nombre>."
            )
        # proceso local normal → ok, no reportar

    if not findings:
        return ""
    return (
        "⚠️ RUNTIME DIAGNÓSTICO (comparación automática de puertos — el "
        "código puede estar bien pero el entorno roto):\n"
        + "\n".join(findings)
        + "\nResolvé el entorno ANTES de tocar el código."
    )


def _health_check(port: int, paths: tuple[str, ...]) -> str:
    """GET a los endpoints de salud del API. Devuelve descripción o ''."""
    import urllib.request
    for p in paths:
        url = f"http://127.0.0.1:{port}{p}"
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                if resp.status < 500:
                    return f"puerto {port} responde ({url} → HTTP {resp.status})"
        except Exception:
            continue
    return ""


def runtime_status(
    repo_path: str,
    *,
    ports: list[int] | None = None,
    lsof_fn=None,
    health_checks: dict[int, tuple[str, ...]] | None = None,
) -> str:
    """Reporte de estado de runtime SIEMPRE (a diferencia de
    detect_runtime_issues que devuelve '' cuando está todo bien).

    El modelo necesita saber CUANDO el entorno está sano: si el diagnóstico
    solo inyecta hallazgos, el modelo no distingue "entorno roto" de
    "entorno chequeado y OK" — y sigue buscando bugs de código inexistentes
    (E2E real: 434s, 5 lecturas de paths inventados, 0 writes).
    """
    issues = detect_runtime_issues(repo_path, ports=ports, lsof_fn=lsof_fn)
    if issues:
        return issues

    if ports is None:
        ports = _detect_ports(repo_path)
    health = health_checks or {
        3000: ("/api/health", "/health"),
        8000: ("/api/health", "/health", "/"),
        8080: ("/health", "/api/health"),
        5000: ("/health", "/api/health"),
    }
    ok_parts = []
    for port in ports:
        check = _health_check(port, health.get(port, ("/health",)))
        if check:
            ok_parts.append(f"  - {check}")
        else:
            ok_parts.append(f"  - puerto {port} escuchando (proceso local)")
    return (
        "🩺 RUNTIME: entorno SANO (proceso local en cada puerto, sin "
        "containers Docker):\n"
        + "\n".join(ok_parts)
        + "\nEl problema que reporta el usuario puede estar en el código o "
        "ya estar resuelto. Verificá la cadena completa antes de escribir."
    )
