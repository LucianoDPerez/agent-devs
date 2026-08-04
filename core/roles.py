from __future__ import annotations

from enum import Enum
from pathlib import Path

from core.intents import Intent
from tools import get_tools_for


class Role(str, Enum):
    ANALYZE = "analyzer"
    PLAN = "planner"
    EXECUTE = "executor"
    REVIEW = "reviewer"
    CHAT = "chat"


_INTENT_TO_ROLE: dict[Intent, Role] = {
    Intent.ANALYZE: Role.ANALYZE,
    Intent.PLAN: Role.PLAN,
    Intent.EXECUTE: Role.EXECUTE,
    Intent.REVIEW: Role.REVIEW,
    Intent.CHAT: Role.CHAT,
}

_ROLE_TO_PROMPT_FILE: dict[Role, str] = {
    Role.ANALYZE: "analyze",
    Role.PLAN: "plan",
    Role.EXECUTE: "execute",
    Role.REVIEW: "review",
    Role.CHAT: "chat",
}


def role_for_intent(intent: Intent) -> Role:
    return _INTENT_TO_ROLE[intent]


def tools_for_role(role: Role) -> list:
    if role == Role.CHAT:
        return []
    return get_tools_for(role.value)


def load_prompt(role: Role) -> str:
    filename = _ROLE_TO_PROMPT_FILE[role]
    prompt_path = Path(__file__).resolve().parent.parent / "prompts" / f"{filename}.md"
    return prompt_path.read_text(encoding="utf-8")