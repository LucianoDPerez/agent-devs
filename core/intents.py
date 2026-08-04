from enum import Enum


class Intent(str, Enum):
    ANALYZE = "analyze"
    PLAN = "plan"
    EXECUTE = "execute"
    REVIEW = "review"
    CHAT = "chat"