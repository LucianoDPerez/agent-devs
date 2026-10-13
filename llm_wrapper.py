"""
Custom ChatOpenAI wrapper que captura `reasoning_content` del llama-server.

El llama-server con `--jinja` envía la salida del modelo a `reasoning_content`
en vez de `content` durante streaming. LangChain's ChatOpenAI no lo lee.
Este wrapper resuelve ese problema.

También:
- NO mezcla reasoning en `content` (si no, el grafo del agente cree que el
  turno terminó en texto y no ejecuta tools).
- Recupera tool calls emitidos como XML/texto (fallo típico del modelo 4B).
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from openai import AsyncOpenAI
from pydantic import PrivateAttr

# XML / pseudo-tool formats that small local models emit instead of native tool_calls
_FUNCTION_XML_RE = re.compile(
    r"<function=(?P<name>[\w.-]+)>\s*(?P<body>.*?)\s*</function>",
    re.DOTALL,
)
_PARAM_XML_RE = re.compile(
    r"<parameter=(?P<key>[\w.-]+)>\s*(?P<val>.*?)\s*</parameter>",
    re.DOTALL,
)
# DSML (DeepSeek Markup Language): el dialecto interno de DeepSeek filtrado
# como texto (<｜DSML｜ invoke name="write_file"> + bloques <｜DSML｜ parameter
# name="k">v</｜DSML｜ parameter>). Visto en vivo: el modelo emite el call
# COMPLETO y bien formado pero con pipes fullwidth, y el harness lo pierde
# (E2E: tasks.json entero evaporado tras agotar exploración). Se acepta
# variante fullwidth (｜, la observada) y ASCII (|).
_DSML_INVOKE_RE = re.compile(
    r"<[｜|]DSML[｜|]\s*invoke\s+name=[\"'](?P<name>[\w.-]+)[\"']\s*>",
)
_DSML_PARAM_RE = re.compile(
    r"<[｜|]DSML[｜|]\s*parameter\s+name=[\"'](?P<key>[\w.-]+)[\"'][^>]*>"
    r"(?P<val>.*?)</[｜|]DSML[｜|]\s*parameter\s*>",
    re.DOTALL,
)
# e.g. 🔧 read_file{"path":"..."}  or  read_file{"path":"..."}
_INLINE_JSON_RE = re.compile(
    r"(?:🔧\s*)?(?P<name>[a-zA-Z_][\w]*)\{(?P<args>[^{}]*)\}",
)
# ```tool\n{"name": "read_file", "args": {...}}\n``` — el 4B envuelve el call
# en fence de código en vez de emitir function-calling nativo.
_FENCED_TOOL_RE = re.compile(
    r"```(?:tool|json)?\s*(?P<body>\{.*?\})\s*```",
    re.DOTALL,
)
# <tool_call>{"name": ...}</tool_call> / <tool_call_start>[Read(...)]<tool_call_end>
# (estilo pythonico LFM2) / [TOOL] name({...}).
_TOOL_TAG_RE = re.compile(
    r"<tool_call(?:_start)?>\s*(?P<body>.*?)\s*</?tool_call(?:_end)?>",
    re.DOTALL,
)
_PYTHONIC_CALL_RE = re.compile(
    r"(?P<name>[a-zA-Z_][\w]*)\s*\((?P<args>[^()]*)\)",
)
_BRACKET_TOOL_RE = re.compile(
    r"\[(?:TOOL|tool)\]\s*(?P<name>[a-zA-Z_][\w]*)\s*\((?P<args>.*?)\)\s*$",
    re.DOTALL | re.MULTILINE,
)

_KWARG_RE = re.compile(
    r"(?P<key>[a-zA-Z_][\w]*)\s*=\s*(?P<val>'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|[^,]+)",
)


def _parse_pythonic_args(raw: str) -> dict[str, Any]:
    """Parsea kwargs estilo Python: path='/x', recursive=True."""
    args: dict[str, Any] = {}
    for m in _KWARG_RE.finditer(raw):
        args[m.group("key")] = _coerce_value(m.group("val"))
    return args


# JSON pelado {"name": ..., "args": {...}} con un nivel de anidado.
_BARE_JSON_RE = re.compile(
    r"(?P<obj>\{(?:[^{}]|\{[^{}]*\})*\"name\"\s*:(?:[^{}]|\{[^{}]*\})*\})",
)


def _repair_json(raw: str) -> dict | None:
    """Parseo tolerante de args: trailing commas y single quotes.

    Los SLM chicos emiten JSON casi-válido (comas colgando, comillas simples).
    Solo se usa para ARGUMENTOS de tool calls, nunca para archivos del repo.
    """
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        fixed = re.sub(r",\s*([}\]])", r"\1", raw).strip()
        if (
            len(fixed) >= 2
            and fixed.startswith("{")
            and fixed.endswith("}")
            and "'" in fixed
        ):
            fixed = fixed.replace("'", '"')
        try:
            parsed = json.loads(fixed)
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None


def _norm_bare_call(name: str, args: Any) -> dict[str, Any] | None:
    """Normaliza {"name":..., "args"|"parameters"|"arguments":...} a tool call."""
    if not name or not isinstance(name, str):
        return None
    if not isinstance(args, dict):
        return None
    return {
        "id": f"call_bare_{uuid.uuid4().hex[:8]}",
        "name": name,
        "args": args,
        "type": "tool_call",
    }


def _coerce_value(raw: str) -> Any:
    text = raw.strip()
    low = text.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "none"):
        return None
    try:
        if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
            return int(text)
        return float(text)
    except ValueError:
        pass
    if (text.startswith('"') and text.endswith('"')) or (
        text.startswith("'") and text.endswith("'")
    ):
        return text[1:-1]
    return text


def parse_text_tool_calls(content: str) -> list[dict[str, Any]]:
    """Extrae tool calls de XML/texto cuando el modelo no usa function-calling nativo."""
    if not content or not content.strip():
        return []

    calls: list[dict[str, Any]] = []

    for match in _FUNCTION_XML_RE.finditer(content):
        name = match.group("name")
        body = match.group("body") or ""
        args: dict[str, Any] = {}
        for pm in _PARAM_XML_RE.finditer(body):
            args[pm.group("key")] = _coerce_value(pm.group("val"))
        calls.append({
            "id": f"call_xml_{uuid.uuid4().hex[:8]}",
            "name": name,
            "args": args,
            "type": "tool_call",
        })

    if calls:
        return calls

    # Bloques DSML (ver _DSML_*_RE): cada invoke con sus parameters hasta el
    # próximo invoke (o fin del texto: el cierre </invoke> suele faltar).
    _invokes = list(_DSML_INVOKE_RE.finditer(content))
    for i, match in enumerate(_invokes):
        end = _invokes[i + 1].start() if i + 1 < len(_invokes) else len(content)
        args: dict[str, Any] = {}
        for pm in _DSML_PARAM_RE.finditer(content, match.end(), end):
            args[pm.group("key")] = _coerce_value(pm.group("val"))
        calls.append({
            "id": f"call_dsml_{uuid.uuid4().hex[:8]}",
            "name": match.group("name"),
            "args": args,
            "type": "tool_call",
        })

    if calls:
        return calls

    for match in _INLINE_JSON_RE.finditer(content):
        name = match.group("name")
        raw_args = "{" + match.group("args") + "}"
        args = _repair_json(raw_args)
        if args is None:
            continue
        calls.append({
            "id": f"call_txt_{uuid.uuid4().hex[:8]}",
            "name": name,
            "args": args,
            "type": "tool_call",
        })

    if calls:
        return calls

    # Bloques fenceados ```tool {...}``` / ```json {...}```.
    for match in _FENCED_TOOL_RE.finditer(content):
        parsed = _repair_json(match.group("body"))
        if not isinstance(parsed, dict):
            continue
        norm = _norm_bare_call(
            parsed.get("name"),
            parsed.get("args", parsed.get("parameters", parsed.get("arguments"))),
        )
        if norm is not None:
            calls.append(norm)

    if calls:
        return calls

    # Tags <tool_call>…</tool_call> (JSON o pythonico Read(path='…')).
    for match in _TOOL_TAG_RE.finditer(content):
        body = match.group("body").strip()
        parsed = _repair_json(body)
        if isinstance(parsed, dict):
            norm = _norm_bare_call(
                parsed.get("name"),
                parsed.get("args", parsed.get("parameters", parsed.get("arguments"))),
            )
            if norm is not None:
                calls.append(norm)
                continue
        builtin = _BRACKET_TOOL_RE.search(body) or _BRACKET_TOOL_RE.search(content)
        if builtin is not None:
            args = _repair_json(builtin.group("args"))
            norm = _norm_bare_call(builtin.group("name"), args if args else {})
            if norm is not None:
                calls.append(norm)
                continue
        # Pythonico dentro del tag: Read(path='/x', recursive=True).
        py = _PYTHONIC_CALL_RE.search(body)
        if py is not None:
            norm = _norm_bare_call(py.group("name"), _parse_pythonic_args(py.group("args")))
            if norm is not None:
                calls.append(norm)

    if calls:
        return calls

    # JSON pelado {"name": "...", "args": {...}} en el texto (un nivel de
    # anidado para el objeto de args).
    for match in _BARE_JSON_RE.finditer(content):
        parsed = _repair_json(match.group("obj"))
        if not isinstance(parsed, dict):
            continue
        norm = _norm_bare_call(
            parsed.get("name"),
            parsed.get("args", parsed.get("parameters", parsed.get("arguments"))),
        )
        if norm is not None:
            calls.append(norm)

    if calls:
        return calls

    # [TOOL] name(...) y pythonico Name(k='v') a nivel de texto (sin tags).
    for match in _BRACKET_TOOL_RE.finditer(content):
        raw = match.group("args").strip()
        args = _repair_json(raw if raw.startswith("{") else "{" + raw + "}")
        if args is None:
            args = _parse_pythonic_args(raw)
        norm = _norm_bare_call(match.group("name"), args)
        if norm is not None and norm["args"]:
            calls.append(norm)

    if calls:
        return calls

    for match in _PYTHONIC_CALL_RE.finditer(content):
        norm = _norm_bare_call(match.group("name"), _parse_pythonic_args(match.group("args")))
        if norm is not None and norm["args"]:
            calls.append(norm)

    return calls


def _openai_tool_calls_to_lc(msg) -> list[dict[str, Any]]:
    """Convierte tool_calls de la API OpenAI al formato LangChain AIMessage."""
    raw = getattr(msg, "tool_calls", None) or []
    out: list[dict[str, Any]] = []
    for tc in raw:
        fn = getattr(tc, "function", None)
        if fn is None and isinstance(tc, dict):
            fn = tc.get("function") or {}
            name = fn.get("name") if isinstance(fn, dict) else None
            arguments = fn.get("arguments") if isinstance(fn, dict) else "{}"
            tc_id = tc.get("id") or f"call_{uuid.uuid4().hex[:8]}"
        else:
            name = getattr(fn, "name", None)
            arguments = getattr(fn, "arguments", None) or "{}"
            tc_id = getattr(tc, "id", None) or f"call_{uuid.uuid4().hex[:8]}"
        if not name:
            continue
        try:
            args = json.loads(arguments) if isinstance(arguments, str) else (arguments or {})
        except json.JSONDecodeError:
            args = {}
        out.append({"id": tc_id, "name": name, "args": args, "type": "tool_call"})
    return out


def is_protocol_error(exc: BaseException) -> bool:
    """True si el gateway dice que el modelo no habla ese protocolo.

    Zen responde 400 ModelProtocolUnsupported cuando se usa chat/completions
    con un modelo de Responses (gpt-*, muse-spark-*, grok-*) y viceversa.
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if getattr(cur, "code", None) == "ModelProtocolUnsupported":
            return True
        if "modelprotocolunsupported" in str(cur).lower().replace(" ", ""):
            return True
        if "does not support this protocol" in str(cur).lower():
            return True
        nxt = getattr(cur, "__cause__", None) or getattr(cur, "__context__", None)
        cur = nxt if isinstance(nxt, BaseException) else None
    return False


class _UsageTracker:
    """Contador de tokens compartido entre instancias (sobrevive a model_copy)."""

    def __init__(self):
        import threading as _threading

        self._lock = _threading.Lock()
        self.turn = {"prompt": 0, "completion": 0, "cached": 0}
        self.session = {"prompt": 0, "completion": 0, "cached": 0}

    def record(self, prompt: int, completion: int, cached: int = 0):
        with self._lock:
            for acc in (self.turn, self.session):
                acc["prompt"] = max(acc["prompt"], prompt)
                acc["completion"] += completion
                acc["cached"] = max(acc["cached"], cached)

    def reset_turn(self):
        with self._lock:
            self.turn = {"prompt": 0, "completion": 0, "cached": 0}

    def report(self) -> dict:
        with self._lock:
            return {"turn": dict(self.turn), "session": dict(self.session)}


_USAGE = _UsageTracker()


def reset_turn_usage():
    _USAGE.reset_turn()


def get_usage() -> dict:
    return _USAGE.report()


class LocalLLM(BaseChatModel):
    """Chat model que conecta a un llama-server OpenAI-compatible con soporte de reasoning_content.

    Transporte dual para la nube (OpenCode Zen):
    - "chat": POST /v1/chat/completions (default; único que habla llama.cpp).
    - "responses": POST /v1/responses (modelos que lo exigen: gpt-*, muse-spark-*,
      grok-*). Conmutación automática ante ModelProtocolUnsupported + caché en
      disco para no pagar el 400 dos veces. El nivel de razonamiento de estos
      modelos se controla con `reasoning_effort` (low/medium/high, nativo de
      la API Responses); los params llama-only (max_reasoning_tokens,
      chat_template_kwargs) NUNCA viajan a la nube.
    """

    base_url: str = "http://localhost:8080/v1"
    model_name: str = "agents-a1-4b"
    temperature: float = 0.85
    max_tokens: int = 4096
    api_key: str = "not-needed"
    max_reasoning_tokens: int = 0
    force_tool_calls: bool = False
    transport: str = "chat"
    reasoning_effort: str | None = None
    # Control de thinking vía chat_template_kwargs (ej. {"enable_thinking": False}
    # para Gemma 4/Qwen3 en llama.cpp). El retry desactiva el thinking: con
    # tool_choice="required" + thinking activo, el modelo razona sin converger
    # y nunca emite la tool call (turnos colgados de 90s+ en E2E).
    chat_template_kwargs: dict | None = None
    _client: AsyncOpenAI | None = PrivateAttr(default=None)
    _tools: list[BaseTool] = PrivateAttr(default_factory=list)
    _tool_choice: Any | None = PrivateAttr(default=None)

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            try:
                self._client = AsyncOpenAI(
                    base_url=self.base_url,
                    api_key=self.api_key,
                )
            except ImportError:
                # Carrera de imports en openai 3.x (visto en vivo: primer
                # AsyncOpenAI() del proceso falla con "cannot import name
                # 'materialize_input_tools'" y envenena todos los turnos).
                # Reimportar explícito completa la inicialización y el retry
                # suele sanar. Si re-falla, que suba con contexto.
                import importlib

                importlib.import_module("openai.lib._parsing")
                importlib.import_module("openai.resources.chat.completions")
                self._client = AsyncOpenAI(
                    base_url=self.base_url,
                    api_key=self.api_key,
                )
        return self._client

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        new = self.model_copy(deep=False)
        new._tools = list(tools)
        new._tool_choice = tool_choice
        # Copiar dict mutable: si no, mutar chat_template_kwargs en una copia
        # contamina a todas (model_copy deep=False comparte referencia).
        if self.chat_template_kwargs is not None:
            new.chat_template_kwargs = dict(self.chat_template_kwargs)
        return new

    def _is_local(self) -> bool:
        base = self.base_url or ""
        return "localhost" in base or "127.0.0.1" in base

    def _sync_transport(self) -> None:
        """Si el caché en disco ya sabe que el modelo es responses, conmutar
        sin pagar un 400 (las copias via model_copy pueden traerlo viejo)."""
        if self.transport == "chat" and not self._is_local():
            try:
                from llm_config import transport_for

                if transport_for(self.model_name) == "responses":
                    self.transport = "responses"
            except Exception:
                pass

    def _known_text_tool_calls(self, content: str) -> list[dict[str, Any]]:
        """Recovery de tool calls en texto FILTRADO a tools reales.

        Sin filtro, la prosa de un modelo fuerte (snippets citados como
        `if (rule.type === ...)`, conteos "0 matches") se parsea como calls
        fantasma (if/casero/matches) → error de tool inexistente → el modelo
        reintenta repitiendo el análisis → loop hasta el corte. Los calls del
        4B (read_file/cm__* con nombres reales) pasan igual; sin tools
        bindeadas no hay nada válido que recuperar.
        """
        recovered = parse_text_tool_calls(content)
        if not recovered or not self._tools:
            return []
        known = {getattr(t, "name", "") for t in self._tools}
        return [tc for tc in recovered if tc.get("name") in known]

    def _convert_messages(self, messages: list[BaseMessage]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for msg in messages:
            if isinstance(msg, HumanMessage):
                result.append({"role": "user", "content": msg.content})
            elif isinstance(msg, AIMessage):
                entry: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
                if msg.tool_calls:
                    entry["tool_calls"] = [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": json.dumps(tc["args"]),
                            },
                        }
                        for tc in msg.tool_calls
                    ]
                result.append(entry)
            elif isinstance(msg, ToolMessage):
                result.append({"role": "tool", "content": msg.content, "tool_call_id": msg.tool_call_id})
            elif isinstance(msg, SystemMessage):
                result.append({"role": "system", "content": msg.content})
            else:
                result.append({"role": "user", "content": str(msg.content)})
        return result

    def _extract_kwargs(self, stop: list[str] | None = None, **kwargs) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": self.model_name,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if self._tools:
            params["tools"] = [convert_to_openai_tool(t) for t in self._tools]
            # Force tool calling: el 4B a veces responde TEXTO plano en vez de
            # tool calls (monólogo circular). tool_choice="required" obliga al
            # modelo a emitir un tool call siempre. Solo se usa en el retry
            # write-only (force_tool_calls=True).
            if self.force_tool_calls:
                params["tool_choice"] = "required"
        if self._tool_choice is not None:
            params["tool_choice"] = self._tool_choice
        if stop:
            params["stop"] = stop
        # Params llama-only: SOLO contra llama-server. La nube (Zen) los
        # reenvía al proveedor real y un campo desconocido puede dar 400.
        if self._is_local() and (self.max_reasoning_tokens > 0 or self.chat_template_kwargs):
            body = {**kwargs.get("extra_body", {})}
            if self.max_reasoning_tokens > 0:
                body["max_reasoning_tokens"] = self.max_reasoning_tokens
            if self.chat_template_kwargs:
                body["chat_template_kwargs"] = self.chat_template_kwargs
            params["extra_body"] = body
        elif kwargs.get("extra_body"):
            params["extra_body"] = kwargs["extra_body"]
        return params

    @property
    def _llm_type(self) -> str:
        return "local-llm"

    def _generate(self, messages, stop=None, **kwargs):
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(self._agenerate(messages, stop, **kwargs))
        finally:
            loop.close()
        return result

    async def _agenerate(self, messages, stop=None, **kwargs):
        self._sync_transport()
        if self.transport == "responses":
            return await self._agenerate_responses(messages)
        client = self._get_client()
        params = self._extract_kwargs(stop, **kwargs)
        openai_msgs = self._convert_messages(messages)

        try:
            response = await client.chat.completions.create(
                **params,
                messages=openai_msgs,
                stream=False,
            )
        except Exception as e:
            if not self._is_local() and is_protocol_error(e):
                self._switch_to_responses()
                return await self._agenerate_responses(messages)
            raise

        choice = response.choices[0]
        msg = choice.message
        # NUNCA mezclar reasoning en content: el agente lo toma como respuesta final.
        content = msg.content or ""
        tool_calls = _openai_tool_calls_to_lc(msg)
        if not tool_calls:
            tool_calls = self._known_text_tool_calls(content)

        usage = response.usage
        prompt_tokens = getattr(usage, "prompt_tokens", 0) if usage else 0
        completion_tokens = getattr(usage, "completion_tokens", 0) if usage else 0
        cached_tokens = 0
        if usage and getattr(usage, "prompt_tokens_details", None):
            cached_tokens = getattr(usage.prompt_tokens_details, "cached_tokens", 0) or 0
        _USAGE.record(prompt_tokens, completion_tokens, cached_tokens)

        token_usage = {
            "input_tokens": prompt_tokens,
            "output_tokens": completion_tokens,
        }

        generation = ChatGeneration(
            message=AIMessage(content=content, tool_calls=tool_calls),
            generation_info=token_usage,
        )
        return ChatResult(generations=[generation])

    # ── Transporte Responses (/v1/responses) ─────────────────────────────

    def _switch_to_responses(self) -> None:
        """Conmuta a responses y lo cachea (no pagar el 400 dos veces)."""
        self.transport = "responses"
        try:
            from llm_config import remember_transport

            remember_transport(self.model_name, "responses")
        except Exception:
            pass

    def _responses_tools(self) -> list[dict[str, Any]] | None:
        """Tools al formato function de Responses API (plano, sin strict)."""
        if not self._tools:
            return None
        out: list[dict[str, Any]] = []
        for t in self._tools:
            chat_tool = convert_to_openai_tool(t)
            fn = chat_tool.get("function", chat_tool)
            out.append({
                "type": "function",
                "name": fn.get("name", t.name),
                "description": fn.get("description") or "",
                "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
            })
        return out

    def _convert_to_responses_input(self, messages: list[BaseMessage]) -> list[dict[str, Any]]:
        """Mensajes LangChain → items de Responses API (stateless: input full).

        Los tool_calls del assistant se reinyectan como function_call y sus
        resultados como function_call_output (matcheados por call_id).
        """
        items: list[dict[str, Any]] = []
        for msg in messages:
            if isinstance(msg, HumanMessage):
                items.append({"role": "user", "content": str(msg.content or "")})
            elif isinstance(msg, SystemMessage):
                items.append({"role": "system", "content": str(msg.content or "")})
            elif isinstance(msg, AIMessage):
                if msg.content:
                    items.append({"role": "assistant", "content": str(msg.content)})
                for tc in msg.tool_calls or []:
                    items.append({
                        "type": "function_call",
                        "call_id": tc.get("id", ""),
                        "name": tc.get("name", ""),
                        "arguments": json.dumps(tc.get("args", {})),
                    })
            elif isinstance(msg, ToolMessage):
                items.append({
                    "type": "function_call_output",
                    "call_id": msg.tool_call_id or "",
                    "output": str(msg.content or ""),
                })
            else:
                items.append({"role": "user", "content": str(msg.content)})
        return items

    def _responses_kwargs(self) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": self.model_name,
            "max_output_tokens": self.max_tokens,
        }
        tools = self._responses_tools()
        if tools:
            params["tools"] = tools
            if self.force_tool_calls:
                params["tool_choice"] = "required"
        if self._tool_choice is not None:
            params["tool_choice"] = self._tool_choice
        # Modo de razonamiento particular del modelo (low/medium/high).
        # Es el reemplazo cloud de max_reasoning_tokens de llama.cpp.
        if self.reasoning_effort:
            params["reasoning"] = {"effort": self.reasoning_effort}
        return params

    @staticmethod
    def _responses_text(output_items: list) -> str:
        parts: list[str] = []
        for item in output_items or []:
            if getattr(item, "type", None) != "message":
                continue
            for block in getattr(item, "content", None) or []:
                if getattr(block, "type", None) == "output_text":
                    parts.append(getattr(block, "text", "") or "")
        return "".join(parts)

    @staticmethod
    def _responses_tool_calls(output_items: list) -> list[dict[str, Any]]:
        calls: list[dict[str, Any]] = []
        for item in output_items or []:
            if getattr(item, "type", None) != "function_call":
                continue
            name = getattr(item, "name", "") or ""
            if not name:
                continue
            raw_args = getattr(item, "arguments", "") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
            except json.JSONDecodeError:
                args = {}
            calls.append({
                "id": getattr(item, "call_id", "") or f"call_resp_{uuid.uuid4().hex[:8]}",
                "name": name,
                "args": args if isinstance(args, dict) else {},
                "type": "tool_call",
            })
        return calls

    async def _agenerate_responses(self, messages) -> ChatResult:
        client = self._get_client()
        params = self._responses_kwargs()
        params["input"] = self._convert_to_responses_input(messages)
        # Sin temperature primero: varios reasoning models la rechazan si no
        # es el default. Si el 400 la menciona, reintentar CON temperature.
        # (Al revés que lo intuitivo: el default ya es el del proveedor.)
        response = await client.responses.create(**params, stream=False)

        output_items = list(getattr(response, "output", None) or [])
        content = self._responses_text(output_items)
        tool_calls = self._responses_tool_calls(output_items)
        if not tool_calls:
            tool_calls = self._known_text_tool_calls(content)

        usage = getattr(response, "usage", None)
        prompt_tokens = getattr(usage, "input_tokens", 0) if usage else 0
        completion_tokens = getattr(usage, "output_tokens", 0) if usage else 0
        _USAGE.record(prompt_tokens, completion_tokens)

        return ChatResult(generations=[ChatGeneration(
            message=AIMessage(content=content, tool_calls=tool_calls),
            generation_info={"input_tokens": prompt_tokens, "output_tokens": completion_tokens},
        )])

    async def _astream_responses(self, messages: list[BaseMessage]) -> AsyncIterator[ChatGenerationChunk]:
        client = self._get_client()
        params = self._responses_kwargs()
        params["input"] = self._convert_to_responses_input(messages)

        stream = await client.responses.create(**params, stream=True)

        saw_native_tool = False
        content_parts: list[str] = []
        # item_id → índice estable para los tool_call_chunks del merger.
        item_index: dict[str, int] = {}
        item_meta: dict[str, dict] = {}

        async for event in stream:
            etype = getattr(event, "type", "") or ""
            if etype == "response.completed":
                resp = getattr(event, "response", None)
                usage = getattr(resp, "usage", None) if resp else None
                if usage:
                    _USAGE.record(
                        getattr(usage, "input_tokens", 0) or 0,
                        getattr(usage, "output_tokens", 0) or 0,
                    )
                continue
            if etype == "response.output_text.delta":
                delta = getattr(event, "delta", "") or ""
                if delta:
                    content_parts.append(delta)
                    yield ChatGenerationChunk(
                        message=AIMessageChunk(content=delta)
                    )
            elif etype == "response.output_item.added":
                item = getattr(event, "item", None)
                if item is not None and getattr(item, "type", None) == "function_call":
                    iid = getattr(event, "item_id", "") or getattr(item, "id", "") or ""
                    if iid and iid not in item_index:
                        item_index[iid] = len(item_index)
                        item_meta[iid] = {
                            "call_id": getattr(item, "call_id", "") or iid,
                            "name": getattr(item, "name", "") or "",
                        }
                        saw_native_tool = True
            elif etype == "response.function_call_arguments.delta":
                iid = getattr(event, "item_id", "") or ""
                delta = getattr(event, "delta", "") or ""
                if iid not in item_index:
                    item_index[iid] = len(item_index)
                    item_meta[iid] = {"call_id": iid, "name": ""}
                    saw_native_tool = True
                if delta:
                    meta = item_meta[iid]
                    chunk_entry: dict[str, Any] = {"index": item_index[iid], "args": delta}
                    # id+name en el primer chunk del item para que el merger
                    # de LangChain lo ensamble (igual que el path chat).
                    if not meta.get("sent"):
                        if meta.get("call_id"):
                            chunk_entry["id"] = meta["call_id"]
                        if meta.get("name"):
                            chunk_entry["name"] = meta["name"]
                        meta["sent"] = True
                    yield ChatGenerationChunk(
                        message=AIMessageChunk(
                            content="",
                            tool_call_chunks=[chunk_entry],
                        )
                    )
            elif etype == "response.function_call_arguments.done":
                iid = getattr(event, "item_id", "") or ""
                if iid in item_meta and not item_meta[iid].get("name") and getattr(event, "name", ""):
                    item_meta[iid]["name"] = event.name
            elif etype in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
                delta = getattr(event, "delta", "") or ""
                if delta:
                    yield ChatGenerationChunk(
                        message=AIMessageChunk(
                            content="",
                            additional_kwargs={"is_reasoning": True, "reasoning_content": delta},
                        )
                    )

        # Fallback filtrado a tools reales (sin filtro, la prosa con código
        # genera calls fantasma → loop). Ver _known_text_tool_calls.
        if not saw_native_tool:
            recovered = self._known_text_tool_calls("".join(content_parts))
            for i, tc in enumerate(recovered):
                yield ChatGenerationChunk(
                    message=AIMessageChunk(
                        content="",
                        tool_call_chunks=[{
                            "index": i,
                            "id": tc["id"],
                            "name": tc["name"],
                            "args": json.dumps(tc["args"]),
                        }],
                    )
                )

    async def _astream(self, messages: list[BaseMessage], **kwargs) -> AsyncIterator[ChatGenerationChunk]:
        self._sync_transport()
        if self.transport == "responses":
            async for chunk in self._astream_responses(messages):
                yield chunk
            return
        client = self._get_client()
        params = self._extract_kwargs(**kwargs)
        openai_msgs = self._convert_messages(messages)

        try:
            stream = await client.chat.completions.create(
                **params,
                messages=openai_msgs,
                stream=True,
                stream_options={"include_usage": True},
            )
        except Exception as e:
            if not self._is_local() and is_protocol_error(e):
                self._switch_to_responses()
                async for chunk in self._astream_responses(messages):
                    yield chunk
                return
            raise

        saw_native_tool = False
        content_parts: list[str] = []

        async for chunk in stream:
            if chunk.usage:
                prompt_tokens = getattr(chunk.usage, "prompt_tokens", 0) or 0
                completion_tokens = getattr(chunk.usage, "completion_tokens", 0) or 0
                cached_tokens = 0
                if getattr(chunk.usage, "prompt_tokens_details", None):
                    cached_tokens = getattr(chunk.usage.prompt_tokens_details, "cached_tokens", 0) or 0
                _USAGE.record(prompt_tokens, completion_tokens, cached_tokens)
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta

            content = delta.content or ""
            reasoning = getattr(delta, "reasoning_content", "") or ""

            tool_call_chunks = []
            for tc in delta.tool_calls or []:
                entry: dict[str, Any] = {"index": tc.index}
                if tc.id:
                    entry["id"] = tc.id
                if tc.function and tc.function.name:
                    entry["name"] = tc.function.name
                    saw_native_tool = True
                if tc.function and tc.function.arguments:
                    entry["args"] = tc.function.arguments
                if len(entry) > 1:
                    tool_call_chunks.append(entry)

            # Reasoning: solo UI — content vacío para no contaminar el AIMessage del grafo
            if reasoning and not content:
                yield ChatGenerationChunk(
                    message=AIMessageChunk(
                        content="",
                        additional_kwargs={
                            "is_reasoning": True,
                            "reasoning_content": reasoning,
                        },
                    )
                )

            if content:
                content_parts.append(content)

            if content or tool_call_chunks:
                yield ChatGenerationChunk(
                    message=AIMessageChunk(
                        content=content,
                        tool_call_chunks=tool_call_chunks,
                    )
                )

        # Fallback filtrado a tools reales (sin filtro, la prosa con código
        # genera calls fantasma → loop). Ver _known_text_tool_calls.
        if not saw_native_tool:
            recovered = self._known_text_tool_calls("".join(content_parts))
            for i, tc in enumerate(recovered):
                yield ChatGenerationChunk(
                    message=AIMessageChunk(
                        content="",
                        tool_call_chunks=[{
                            "index": i,
                            "id": tc["id"],
                            "name": tc["name"],
                            "args": json.dumps(tc["args"]),
                        }],
                    )
                )


# ── Detección del modelo real cargado en llama-server ──────────────────────


def pretty_model_id(raw: str) -> str:
    """Normaliza el id/path que reporta llama-server a un nombre legible.

    '/models/qwen3.6-35b-a3b-Q4_K_M.gguf' → 'qwen3.6-35b-a3b-Q4_K_M'.
    Si ya es un alias limpio, lo deja intacto.
    """
    name = (raw or "").strip().rstrip("/").split("/")[-1]
    low = name.lower()
    for ext in (".gguf", ".bin", ".safetensors"):
        if low.endswith(ext):
            name = name[: -len(ext)]
            break
    return name


def detect_server_model(base_url: str, timeout: float = 1.5) -> str | None:
    """Modelo realmente cargado en el server (GET /v1/models), o None.

    El header de la TUI muestra ESTE valor y no el LLM_MODEL_NAME del config:
    el config puede quedar viejo si el usuario carga otro modelo en
    llama-server. Fail-open: si no hay server, None y el caller hace fallback.
    """
    import json
    import urllib.request

    base = base_url.split("/v1", 1)[0]
    try:
        with urllib.request.urlopen(base + "/v1/models", timeout=timeout) as resp:
            data = json.load(resp)
        # llama-server moderno: {"models":[...]}; compat OpenAI: {"data":[...]}
        items = data.get("data") or data.get("models") or []
        ids = [m.get("id") or m.get("name") for m in items if (m.get("id") or m.get("name"))]
        return pretty_model_id(ids[0]) if ids else None
    except Exception:
        return None


def detect_context_limit(base_url: str, timeout: float = 1.5) -> int | None:
    """n_ctx REAL asignado al server (GET /props de llama-server), o None.

    El config puede quedar viejo: en esta máquina el default asumía -c 62000
    y el server tenía 36608 → la estimación de contexto nunca llegaba al umbral
    de summary y las requests reventaban con 400 contra n_ctx físico.
    """
    import json
    import urllib.request

    base = base_url.split("/v1", 1)[0]
    try:
        with urllib.request.urlopen(base + "/props", timeout=timeout) as resp:
            data = json.load(resp)
        # El contexto EFECTIVO es default_generation_settings.n_ctx: refleja el
        # -c configurado (por slot). n_ctx_train es el máximo TEÓRICO del
        # modelo (ej. 131072 para Qwen3) y NO lo que el server acepta: usarlo
        # inflaba el % de contexto del toolbar (nunca llegaba a 80% y el
        # /compact nunca aparecía). n_ctx_train SOLO como último recurso y
        # acotado a 4x el contexto configurado (heurística segura).
        v = data.get("default_generation_settings", {}).get("n_ctx")
        if isinstance(v, int) and v > 0:
            return v
        v = data.get("n_ctx")
        if isinstance(v, int) and v > 0:
            return v
        v = data.get("model", {}).get("n_ctx_train")
        if isinstance(v, int) and v > 0:
            return v
    except Exception:
        return None
    return None
