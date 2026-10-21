"""Resolución del LLM activo: local (llama.cpp) u OpenCode Zen.

Prioridad (de menor a mayor):
  1. defaults (local, mismos que config.py)
  2. archivo global ~/.config/agent-devs/config.json
     (legacy: ~/.agent-devs.json — se sigue leyendo si existe)
  3. .env del repo actual / del harness (solo lectura, sin dependencias)
  4. variables de entorno
  5. overrides CLI (--provider / --model)

El config vive FUERA del repo de instalación (~/.agent-devs es un checkout
git con `pull --ff-only`: guardar la key ahí la expondría a un commit/push
accidental). La API key vive SOLO en el config (chmod 600) o en env.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

OPENCODE_DEFAULT_BASE_URL = "https://opencode.ai/zen/v1"

def _config_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else (Path.home() / ".config")
    return base / "agent-devs"


CONFIG_FILE = _config_dir() / "config.json"
LEGACY_CONFIG_FILE = Path.home() / ".agent-devs.json"


def active_config_file() -> Path:
    """Ruta del config en uso: la nueva, o la legacy si es la que existe."""
    if CONFIG_FILE.exists():
        return CONFIG_FILE
    if LEGACY_CONFIG_FILE.exists():
        return LEGACY_CONFIG_FILE
    return CONFIG_FILE
CACHE_FILE = Path.home() / ".agent-cache" / "opencode_models.json"
MODELS_CACHE_TTL = 24 * 3600


def _read_dotenv(path: Path) -> dict[str, str]:
    """Parser mínimo de .env (KEY=valor, sin dependencias)."""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip("'").strip('"')
        if key and val:
            out[key] = val
    return out


def load_file_config() -> dict[str, Any]:
    try:
        return json.loads(active_config_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def load_config() -> dict[str, Any]:
    """Config efectiva mezclando archivo + .env + entorno."""
    cfg: dict[str, Any] = dict(load_file_config())

    # .env del CWD y del harness (el .env ya está gitignored).
    dotenv: dict[str, str] = {}
    for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parent / ".env"):
        dotenv.update(_read_dotenv(candidate))

    def pick(*names: str) -> str | None:
        for name in names:
            if os.environ.get(name):
                return os.environ[name]
            if dotenv.get(name):
                return dotenv[name]
        return None

    provider = pick("AGENT_DEVS_PROVIDER")
    if provider:
        cfg["provider"] = provider
    key = pick("OPENCODE_API_KEY")
    if key:
        cfg["opencode_api_key"] = key
    model = pick("OPENCODE_MODEL", "AGENT_DEVS_MODEL")
    if model:
        cfg["opencode_model"] = model
    base = pick("OPENCODE_BASE_URL", "AGENT_DEVS_BASE_URL")
    if base:
        cfg["opencode_base_url"] = base
    reasoning = pick("OPENCODE_REASONING", "AGENT_DEVS_REASONING")
    if reasoning:
        cfg["opencode_reasoning"] = reasoning
    language = pick("AGENT_DEVS_LANGUAGE")
    if language:
        cfg["language"] = language
    return cfg


def save_config(data: dict[str, Any]) -> Path:
    """Guarda en la ruta nueva con permisos 600 (solo el dueño lo lee)."""
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except OSError:
        pass
    return CONFIG_FILE


REASONING_EFFORTS = ("low", "medium", "high")
DEFAULT_REASONING_EFFORT = "medium"


def resolve_reasoning(reasoning_override: str | None = None) -> str:
    """Nivel de razonamiento para transporte Responses (low/medium/high)."""
    cfg = load_config()
    level = (reasoning_override or cfg.get("opencode_reasoning") or DEFAULT_REASONING_EFFORT)
    level = str(level).strip().lower()
    return level if level in REASONING_EFFORTS else DEFAULT_REASONING_EFFORT


# ── Transporte por modelo: chat vs responses ─────────────────────────────
# Zen sirve cada modelo en UN protocolo (tabla Endpoints de sus docs):
#  - @ai-sdk/openai-compatible → /v1/chat/completions (deepseek, qwen-max,
#    glm, minimax, …): lo que el harness ya habla.
#  - @ai-sdk/openai → /v1/responses (gpt-*, muse-spark-*, grok-*): chat da
#    ModelProtocolUnsupported (verificado en vivo).
#  - @ai-sdk/anthropic (claude-*, qwen-flash/plus chicos): otro protocolo,
#    sin soporte en este harness (mensaje claro, no traceback).
# La lista es semilla: lo que no esté se prueba por chat y ante
# ModelProtocolUnsupported se conmuta a responses y se cachea el resultado.

RESPONSES_PREFIXES = ("gpt-", "muse-spark", "grok-")

# Prefijos que piden la API de Anthropic Messages (sin soporte acá).
ANTHROPIC_PREFIXES = ("claude-",)

TRANSPORT_CACHE_FILE = Path.home() / ".agent-cache" / "opencode_transport.json"


def _transport_cache() -> dict[str, str]:
    try:
        data = json.loads(TRANSPORT_CACHE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def remember_transport(model: str, transport: str) -> None:
    """Cachea el transporte detectado para no pagar un 400 en cada arranque."""
    try:
        data = _transport_cache()
        data[model] = transport
        TRANSPORT_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        TRANSPORT_CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def transport_for(model: str) -> str:
    """'responses' si el modelo lo exige (semilla + caché), si no 'chat'."""
    cached = _transport_cache().get(model)
    if cached in ("chat", "responses"):
        return cached
    low = model.lower()
    if low.startswith(RESPONSES_PREFIXES):
        return "responses"
    return "chat"


def needs_messages_api(model: str) -> bool:
    """True si el modelo solo habla la API de Anthropic (sin soporte)."""
    return model.lower().startswith(ANTHROPIC_PREFIXES)


def resolve_llm(
    provider_override: str | None = None,
    model_override: str | None = None,
    reasoning_override: str | None = None,
) -> dict[str, str]:
    """Devuelve {provider, base_url, api_key, model, transport, reasoning}."""
    from config import LLM_BASE_URL, LLM_MODEL_NAME

    cfg = load_config()
    provider = (provider_override or cfg.get("provider") or "local").strip().lower()
    if provider not in ("local", "opencode"):
        provider = "local"

    if provider == "opencode":
        model = str(model_override or cfg.get("opencode_model") or "deepseek-v4.1-flash")
        return {
            "provider": "opencode",
            "base_url": str(cfg.get("opencode_base_url") or OPENCODE_DEFAULT_BASE_URL).rstrip("/"),
            "api_key": str(cfg.get("opencode_api_key") or ""),
            # Default verificado E2E (chat + tool calls nativos). Los 🆓 NO
            # sirven desde fuera de OpenCode (FreeTierError).
            "model": model,
            "transport": transport_for(model),
            "reasoning": resolve_reasoning(reasoning_override),
        }
    return {
        "provider": "local",
        "base_url": str(cfg.get("local_base_url") or LLM_BASE_URL).rstrip("/"),
        "api_key": "not-needed",
        "model": str(model_override or cfg.get("local_model") or LLM_MODEL_NAME),
        "transport": "chat",
        "reasoning": resolve_reasoning(reasoning_override),
    }


def is_local_url(base_url: str) -> bool:
    return "localhost" in base_url or "127.0.0.1" in base_url or base_url.startswith("http://localhost")


def is_cloud_url(base_url: str | None) -> bool:
    """True si hay un endpoint remoto (nube). Vacío/desconocido → False (tight)."""
    base = (base_url or "").strip()
    return bool(base) and not is_local_url(base)


# ── Catálogo de modelos OpenCode (gratis vs pago) ──────────────────────────


def _cache_valid() -> bool:
    try:
        return time.time() - CACHE_FILE.stat().st_mtime < MODELS_CACHE_TTL
    except OSError:
        return False


def fetch_opencode_models(
    api_key: str,
    base_url: str = OPENCODE_DEFAULT_BASE_URL,
    timeout: float = 15.0,
    force: bool = False,
) -> list[dict[str, Any]]:
    """GET {base_url}/models. Cachea 24h en ~/.agent-cache/.

    Lanza la excepción original si falla (el caller decide el mensaje).
    """
    if not force and _cache_valid():
        try:
            cached = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            if isinstance(cached, list) and cached:
                return cached
        except (OSError, json.JSONDecodeError):
            pass

    import urllib.request

    req = urllib.request.Request(
        base_url.rstrip("/") + "/models",
        headers={
            "Authorization": f"Bearer {api_key}",
            # Sin User-Agent propio, el edge de opencode.ai responde 403.
            "User-Agent": "agent-devs/1.0",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)

    items = data.get("data") or data.get("models") or []
    models: list[dict[str, Any]] = []
    for m in items:
        if not isinstance(m, dict):
            continue
        mid = m.get("id") or m.get("name") or ""
        if not mid:
            continue
        models.append({
            "id": mid,
            "context": m.get("context_window") or m.get("context_length") or m.get("context"),
            "pricing": m.get("pricing") or {},
            "supports_tools": m.get("supports_tools"),
        })
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(models, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return models


# El endpoint /models de Zen solo devuelve {id, object, created, owned_by}:
# sin pricing ni contexto. Los gratis se detectan por sufijo -free más esta
# lista de excepciones documentadas como gratuitas (ver docs de Zen).
KNOWN_FREE_MODELS = frozenset({
    "big-pickle",
})


def is_free_model(entry: dict[str, Any]) -> bool | None:
    """True = gratis, False = pago, None = desconocido.

    Heurística: id terminado en -free / :free, o en KNOWN_FREE_MODELS.
    Sin pricing en la respuesta, un id sin marca se asume pago.
    """
    mid = str(entry.get("id", ""))
    low = mid.lower()
    if low.endswith("-free") or low.endswith(":free") or "-free-" in low:
        return True
    if low in KNOWN_FREE_MODELS:
        return True
    pricing = entry.get("pricing") or {}
    for key in ("prompt", "input", "completion", "output"):
        if key in pricing:
            try:
                if float(str(pricing[key])) == 0:
                    return True
                return False
            except (ValueError, TypeError):
                pass
    if "free" in low:
        return True
    # Sin marca ni pricing, Zen lo sirve con balance pay-per-use → pago.
    return False


def format_model_list(models: list[dict[str, Any]], limit: int = 45) -> str:
    """Tabla compacta: modelo + costo 🆓/💰 + protocolo chat/responses."""
    lines = [f"{'MODELO':<36} {'COSTO':<9} {'API':<10} TOOLS"]
    lines.append("─" * 80)
    for m in models[:limit]:
        free = is_free_model(m)
        badge = "🆓 gratis" if free is True else ("💰 pago" if free is False else "? s/d")
        tools = m.get("supports_tools")
        tools_s = "sí" if tools is True else ("no" if tools is False else "-")
        lines.append(f"{m['id']:<36} {badge:<9} {m.get('proto', '-'):<10} {tools_s}")
    if len(models) > limit:
        lines.append(f"… y {len(models) - limit} más (mostrando {limit})")
    return "\n".join(lines)
