"""Evidencia RUNTIME para el agente: probe HTTP local + captura de dev server.

Sin estas tools, un "la pantalla queda en blanco" se volvía adivinanza de
inspección estática: el modelo inventaba causas (E2E real: creó variables.css
y reemplazó el import de globals.css sin demostrar NADA). La regla pasa a ser:
causa raíz DEMOSTRADA con evidencia runtime antes de escribir un cambio.
"""

import contextlib
import json
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from langchain_core.tools import tool

# Solo localhost: el agente diagnostica la app local del usuario, nunca URLs
# arbitrarias (evita exfiltración/SSRF desde un prompt malicioso del repo).
_LOCALHOST_RE = re.compile(r"^https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0)(?::\d+)?(?:/|$)")
_MAX_BODY = 2500
_MAX_LOG = 4000
_PORT_RE = re.compile(r"Local:\s+(https?://[^\s]+)")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Bloquea redirects: un 302 a 169.254.169.254 (metadata cloud) vía app
    local con open-redirect sería SSRF. Se reporta el redirect sin seguirlo."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_no_redirect_opener = urllib.request.build_opener(_NoRedirect)


@tool
def probe_http(url: str, timeout_s: float = 5.0) -> str:
    """GET un recurso LOCAL (solo localhost/127.0.0.1) y devuelve status HTTP +
    las primeras líneas del body. Para diagnosticar 'pantalla en blanco':
    probeá la URL de la app (ej. http://localhost:5173/) y las APIs que consume:
    si el HTML llega pero la app no renderiza, el problema es runtime JS; si la
    API devuelve 500, el problema está en el backend. No funciona con URLs
    externas a propósito."""
    if not _LOCALHOST_RE.match(url):
        return f"⛔ probe_http SOLO acepta localhost/127.0.0.1 (recibí: {url})."
    try:
        timeout_s = max(1.0, min(float(timeout_s), 10.0))
    except (TypeError, ValueError):
        timeout_s = 5.0
    try:
        with _no_redirect_opener.open(url, timeout=timeout_s) as resp:
            body = resp.read(_MAX_BODY).decode("utf-8", errors="replace")
            return f"HTTP {resp.status} — {url}\n\n{body[: _MAX_BODY]}"
    except urllib.error.HTTPError as e:
        # Con _NoRedirect, un 301/302 llega acá como HTTPError: no seguirlo.
        if e.code in (301, 302, 303, 307, 308):
            loc = e.headers.get("Location", "?") if e.headers else "?"
            return f"HTTP {e.code} redirect — {url} → {loc} (no seguido por seguridad)"
        body = ""
        with contextlib.suppress(Exception):
            body = e.read(_MAX_BODY).decode("utf-8", errors="replace")
        return f"HTTP {e.code} — {url}\n\n{body[: _MAX_BODY]}"
    except Exception as e:
        return (
            f"❌ No se pudo conectar a {url}: {type(e).__name__}: {e}\n"
            "¿El server está levantado? Si no, corré el dev server con "
            "capture_dev_server o revisá docker-compose."
        )


@tool
def capture_dev_server(path: str, script: str = "dev", wait_s: int = 25) -> str:
    """Corre el dev server del proyecto (npm run <script>, p.ej. 'dev') unos
    segundos, CAPTURA los logs de arranque (errores de compilación reales de
    vite/webpack aparecen en los primeros segundos) y lo DETIENE. Devuelve la
    salida capturada + la URL local detectada (ej. http://localhost:5173/).

    Combinala con probe_http: primero captura_dev_server para ver errores de
    arranque, después probe_http(url) para ver si el HTML sirve. Si el output
    dice 'Port X is already in use', el server YA está corriendo: probá la URL
    con probe_http directamente.
    """
    root = Path(path)
    pkg_path = root / "package.json"
    if not pkg_path.is_file():
        return f"No hay package.json en {path} — no es un proyecto Node."
    try:
        scripts = (json.loads(pkg_path.read_text(encoding="utf-8")) or {}).get("scripts") or {}
    except Exception as e:
        return f"No se pudo leer package.json: {e}"
    if script not in scripts:
        return (
            f"No existe el script '{script}'. Disponibles: "
            f"{', '.join(sorted(scripts.keys())) or '(ninguno)'}"
        )
    try:
        wait_s = max(5, min(int(wait_s), 60))
    except (TypeError, ValueError):
        wait_s = 25
    if not script.replace("-", "").replace("_", "").replace(":", "").isalnum():
        return f"⛔ script inválido: {script!r}"

    proc = subprocess.Popen(
        ["npm", "run", script],
        cwd=str(root),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    lines: list[str] = []

    def pump():
        try:
            for ln in proc.stdout:
                lines.append(ln)
        except Exception:
            pass

    th = threading.Thread(target=pump, daemon=True)
    th.start()
    time.sleep(wait_s)
    try:
        # Matar el process group (npm + hijos vite/webpack), no solo npm
        import os as _os
        import signal as _signal

        try:
            _os.killpg(proc.pid, _signal.SIGTERM)
        except (OSError, ProcessLookupError):
            proc.terminate()
    except Exception:
        with contextlib.suppress(Exception):
            proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        try:
            import os as _os2

            try:
                _os2.killpg(proc.pid, _signal.SIGKILL)
            except Exception:
                proc.kill()
        except Exception:
            pass
    th.join(timeout=2)

    text = "".join(lines)
    if len(text) > _MAX_LOG:
        text = "…(log recortado)…\n" + text[-_MAX_LOG:]
    m = _PORT_RE.search(text)
    if m:
        url = m.group(1)
        text += f'\n\n🌐 URL local detectada: {url}\n→ Usá probe_http("{url}") para ver si responde.'
    elif not text:
        text = "(sin salida capturada en los primeros segundos)"
    return text
