"""
Custom ChatOpenAI wrapper que captura `reasoning_content` del llama-server.

El llama-server con `--jinja` envía la salida del modelo a `reasoning_content`
en vez de `content` durante streaming. LangChain's ChatOpenAI no lo lee.
Este wrapper resuelve ese problema.
"""

import asyncio
import json
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


class _UsageTracker:
    """Contador de tokens compartido entre instancias (sobrevive a model_copy).

    prompt_tokens y cached_tokens usan max() en vez de += porque cada API call
    incluye el contexto COMPLETO (no solo tokens nuevos). Sumar los
    prompt_tokens de multiples calls infla el conteo (cuenta el system prompt
    N veces). El max() representa el tamano real del contexto.

    completion_tokens si se suma: cada call genera tokens nuevos.
    """

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
        if self._tool_choice is not None:
            params["tool_choice"] = self._tool_choice
        if stop:
            params["stop"] = stop
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
        content = msg.content or ""
        reasoning = getattr(msg, "reasoning_content", "") or ""

        all_content = content
        if reasoning and not content:
            all_content = reasoning

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
            message=AIMessage(content=all_content),
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

            combined = delta.content or ""
            reasoning = getattr(delta, "reasoning_content", "") or ""
            is_reasoning = bool(reasoning and not combined)
            if is_reasoning:
                combined = reasoning

            tool_call_chunks = []
            for tc in delta.tool_calls or []:
                entry = {"index": tc.index}
                if tc.id:
                    entry["id"] = tc.id
                if tc.function and tc.function.name:
                    entry["name"] = tc.function.name
                if tc.function and tc.function.arguments:
                    entry["args"] = tc.function.arguments
                if entry:
                    tool_call_chunks.append(entry)

            if combined or tool_call_chunks:
                extra = {"is_reasoning": True} if is_reasoning else {}
                yield ChatGenerationChunk(
                    message=AIMessageChunk(
                        content=combined,
                        tool_call_chunks=tool_call_chunks,
                        additional_kwargs=extra,
                    )
                )
