"""LLMClient port — the one seam between this system and any LLM.

Everything upstream of this protocol is deterministic and offline-testable:
tests use `FakeLLMClient`; production uses the Anthropic adapter
(`records.extract.anthropic_client`, the only module that imports the SDK).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class LLMResponse:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""


class LLMClient(Protocol):
    def complete(
        self,
        *,
        system: str,
        user_content: str,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> LLMResponse: ...


@dataclass
class FakeLLMClient:
    """Canned-response client for offline tests. Pops responses in order and
    records every call for assertion."""

    responses: list[str]
    calls: list[dict] = field(default_factory=list)

    def complete(
        self,
        *,
        system: str,
        user_content: str,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> LLMResponse:
        self.calls.append({"system": system, "user_content": user_content})
        return LLMResponse(self.responses.pop(0), input_tokens=100, output_tokens=50, model="fake")


def strip_fences(text: str) -> str:
    """Remove markdown code fences some model versions wrap JSON in."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0]
    return text.strip()
