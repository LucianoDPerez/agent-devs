"""Idioma del harness (UI + respuestas del modelo).

Alcance v1: `es` y `en`. El chino queda para una segunda vuelta (los strings
son fáciles, pero el 4B local rinde mal en chino; en nube sí funciona).

Resolución (de mayor a menor prioridad):
  1. override explícito (/language, --language)
  2. config file (~/.config/agent-devs/config.json, key "language")
  3. env AGENT_DEVS_LANGUAGE / LANG / LC_ALL / LANGUAGE
  4. default: "en" (el repo apunta a devs globales; README.md es inglés)

La UI usa t(key, lang). Las respuestas del modelo se fuerzan con
language_directive(lang), que se inyecta al system prompt en build_agent
(sin duplicar los 7 prompts/*.md, que siguen en español como instrucciones).
"""
from __future__ import annotations

import os

SUPPORTED = ("en", "es")
DEFAULT = "en"

_ALIASES = {
    "es": "es", "esp": "es", "spanish": "es", "español": "es", "espanol": "es",
    "castellano": "es",
    "en": "en", "eng": "en", "english": "en", "ingles": "en", "inglés": "en",
}


def normalize_lang(raw: str | None) -> str | None:
    """'ES_AR.UTF-8' -> 'es'. None si no es soportado/vacío."""
    if not raw:
        return None
    token = str(raw).strip().lower().replace("_", "-").split(".")[0]
    token = token.split("-")[0].split("@")[0]
    return _ALIASES.get(token)


def detect_language() -> str:
    """Idioma desde el entorno, o DEFAULT (inglés)."""
    for var in ("AGENT_DEVS_LANGUAGE", "LANGUAGE", "LC_ALL", "LANG"):
        got = normalize_lang(os.environ.get(var, ""))
        if got:
            return got
    return DEFAULT


def resolve_language(override: str | None = None) -> str:
    """Override > config file > env > default."""
    got = normalize_lang(override)
    if got:
        return got
    try:
        from llm_config import load_file_config

        got = normalize_lang(load_file_config().get("language"))
        if got:
            return got
    except Exception:
        pass
    try:
        from llm_config import load_config

        got = normalize_lang(load_config().get("language"))
        if got:
            return got
    except Exception:
        pass
    return detect_language()


def set_language(lang: str) -> str:
    """Persiste el idioma en el config file. Lanza ValueError si no soportado."""
    got = normalize_lang(lang)
    if not got:
        raise ValueError(f"Idioma no soportado: {lang!r} (usá es o en)")
    from llm_config import load_file_config, save_config

    cfg = load_file_config()
    cfg["language"] = got
    save_config(cfg)
    return got


def language_directive(lang: str | None) -> str:
    """Instrucción al modelo sobre el idioma de RESPUESTA (no de artefactos)."""
    if (lang or DEFAULT) == "es":
        return (
            "\n\n[IDIOMA] Respondé al usuario en español rioplatense. "
            "El código, identificadores, comentarios técnicos y artefactos "
            "generados siguen en inglés."
        )
    return (
        "\n\n[LANGUAGE] Respond to the user in English. "
        "Code, identifiers, technical comments and generated artifacts "
        "stay in English."
    )


_STRINGS: dict[str, dict[str, str]] = {
    "cmd_new": {"es": "Inicia una nueva sesión (limpia historial y panel)",
                "en": "Start a new session (clears history and panel)"},
    "cmd_compact": {"es": "Resume el historial para liberar contexto",
                    "en": "Summarize history to free context"},
    "cmd_history": {"es": "Muestra los últimos turnos guardados con su id",
                    "en": "Show recent saved turns with their id"},
    "cmd_resume": {"es": "Retoma una sesión anterior: /resume <id>",
                   "en": "Resume a previous session: /resume <id>"},
    "cmd_autoapprove": {"es": "Aprueba escrituras sin preguntar: /autoapprove [on|off]",
                        "en": "Approve writes without asking: /autoapprove [on|off]"},
    "cmd_verify": {"es": "Corre lint/tests/build del repo ahora",
                   "en": "Run the repo lint/tests/build now"},
    "cmd_tasks_pool": {"es": "Pool autónomo: /tasks-pool T001-T010 [archivo] [--commit] [--continue]",
                       "en": "Autonomous pool: /tasks-pool T001-T010 [file] [--commit] [--continue]"},
    "cmd_commit": {"es": "Lista pendientes o commitea: /commit | /commit todo|sesion [mensaje|ai]",
                   "en": "List pending or commit: /commit | /commit todo|sesion [message|ai]"},
    "cmd_push": {"es": "Pushea la rama actual: /push [remote]",
                 "en": "Push the current branch: /push [remote]"},
    "cmd_pr": {"es": "Abre PR de la rama actual con gh: /pr [base]",
               "en": "Open a PR for the current branch with gh: /pr [base]"},
    "cmd_model": {"es": "LLM en caliente: /model | /model <id> [low|medium|high] | /model local|opencode",
                  "en": "Live LLM: /model | /model <id> [low|medium|high] | /model local|opencode"},
    "cmd_log": {"es": "Vuelca el panel a un archivo: /log [N líneas]",
                "en": "Dump the panel to a file: /log [N lines]"},
    "cmd_help": {"es": "Muestra esta ayuda de comandos",
                 "en": "Show this command help"},
    "cmd_language": {"es": "Idioma UI + respuestas: /language | /language es|en",
                     "en": "UI + answer language: /language | /language es|en"},
    "ctx_title": {"es": "⛔ Se quedó sin contexto",
                  "en": "⛔ Ran out of context"},
    "ctx_no_truncate": {"es": "No recorto nada automáticamente para no perder calidad (el output grande suele SER la respuesta). Decidí vos:",
                        "en": "I don't truncate anything automatically to avoid losing quality (the large output usually IS the answer). You decide:"},
    "ctx_opt_compact": {"es": "· /compact — resume el historial y seguí en esta sesión",
                        "en": "· /compact — summarize history and keep going in this session"},
    "ctx_opt_new": {"es": "· /new — turno fresco sin historial",
                    "en": "· /new — fresh turn without history"},
    "ctx_opt_retry": {"es": "· reintentá más acotado (p. ej. cm__get_architecture con aspects=['packages'] o leé 1 archivo por vez)",
                      "en": "· retry narrower (e.g. cm__get_architecture with aspects=['packages'] or read 1 file at a time)"},
    "ctx_opt_provider": {"es": "· subí el -c de llama-server (GET /props) o pasate a la nube: /model muse-spark-1.3",
                         "en": "· raise llama-server -c (GET /props) or switch to cloud: /model muse-spark-1.3"},
    "pool_start": {"es": "Pool inicio",
                   "en": "Pool start"},
    "pool_task_start": {"es": "inicio",
                        "en": "start"},
    "pool_task_end": {"es": "fin",
                      "en": "end"},
    "lang_current": {"es": "Idioma actual",
                     "en": "Current language"},
    "lang_set": {"es": "Idioma cambiado",
                 "en": "Language switched"},
    "lang_usage": {"es": "Uso: /language | /language es|en",
                   "en": "Usage: /language | /language es|en"},
    "lang_invalid": {"es": "Idioma no soportado (usá es o en)",
                     "en": "Unsupported language (use es or en)"},
    "lang_rebuild": {"es": "El próximo turno ya responde en el nuevo idioma.",
                     "en": "The next turn will answer in the new language."},
}


def t(key: str, lang: str | None = None) -> str:
    """Texto UI para key en lang (fallback inglés, luego la key)."""
    entry = _STRINGS.get(key)
    if not entry:
        return key
    lang = (lang or DEFAULT).strip().lower()
    return entry.get(lang, entry.get(DEFAULT, key))
