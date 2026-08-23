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
# e.g. 🔧 read_file{"path":"..."}  or  read_file{"path":"..."}
_INLINE_JSON_RE = re.compile(
    r"(?:🔧\s*)?(?P<name>[a-zA-Z_][\w]*)\{(?P<args>[^{}]*)\}",
)


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

    for match in _INLINE_JSON_RE.finditer(content):
        name = match.group("name")
        raw_args = "{" + match.group("args") + "}"
        try:
            args = json.loads(raw_args)
        except json.JSONDecodeError:
            continue
        if not isinstance(args, dict):
            continue
        calls.append({
            "id": f"call_txt_{uuid.uuid4().hex[:8]}",
            "name": name,
            "args": args,
            "type": "tool_call",
        })

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


class _UsageTracker:
    """Contador de tokens compartido entre instancias (sobrevive a model_copy)."""

    def __init__(self):
        self.turn = {"prompt": 0, "completion": 0, "cached": 0}
        self.session = {"prompt": 0, "completion": 0, "cached": 0}

    def record(self, prompt: int, completion: int, cached: int = 0):
        for acc in (self.turn, self.session):
            acc["prompt"] = max(acc["prompt"], prompt)
            acc["completion"] += completion
            acc["cached"] = max(acc["cached"], cached)

    def reset_turn(self):
        self.turn = {"prompt": 0, "completion": 0, "cached": 0}

    def report(self) -> dict:
        return {"turn": dict(self.turn), "session": dict(self.session)}


_USAGE = _UsageTracker()


def reset_turn_usage():
    _USAGE.reset_turn()


def get_usage() -> dict:
    return _USAGE.report()


class LocalLLM(BaseChatModel):
    """Chat model que conecta a un llama-server OpenAI-compatible con soporte de reasoning_content."""

    base_url: str = "http://localhost:8080/v1"
    model_name: str = "agents-a1-4b"
    temperature: float = 0.85
    max_tokens: int = 4096
    api_key: str = "not-needed"
    max_reasoning_tokens: int = 0
    force_tool_calls: bool = False
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
            self._client = AsyncOpenAI(
                base_url=self.base_url,
                api_key=self.api_key,
            )
        return self._client

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        new = self.model_copy(deep=False)
        new._tools = list(tools)
        new._tool_choice = tool_choice
        return new

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
        if self.max_reasoning_tokens > 0 or self.chat_template_kwargs:
            body = {**kwargs.get("extra_body", {})}
            if self.max_reasoning_tokens > 0:
                body["max_reasoning_tokens"] = self.max_reasoning_tokens
            if self.chat_template_kwargs:
                body["chat_template_kwargs"] = self.chat_template_kwargs
            params["extra_body"] = body
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
        client = self._get_client()
        params = self._extract_kwargs(stop, **kwargs)
        openai_msgs = self._convert_messages(messages)

        response = await client.chat.completions.create(
            **params,
            messages=openai_msgs,
            stream=False,
        )

        choice = response.choices[0]
        msg = choice.message
        # NUNCA mezclar reasoning en content: el agente lo toma como respuesta final.
        content = msg.content or ""
        tool_calls = _openai_tool_calls_to_lc(msg)
        if not tool_calls:
            tool_calls = parse_text_tool_calls(content)

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

    async def _astream(self, messages: list[BaseMessage], **kwargs) -> AsyncIterator[ChatGenerationChunk]:
        client = self._get_client()
        params = self._extract_kwargs(**kwargs)
        openai_msgs = self._convert_messages(messages)

        stream = await client.chat.completions.create(
            **params,
            messages=openai_msgs,
            stream=True,
            stream_options={"include_usage": True},
        )

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

        # Fallback: modelo escribió tools como XML/texto en content
        if not saw_native_tool:
            recovered = parse_text_tool_calls("".join(content_parts))
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

    base = base_url.split("/v1")[0]
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

    base = base_url.split("/v1")[0]
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
