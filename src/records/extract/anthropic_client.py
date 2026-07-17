"""Anthropic adapter — the only module in the tree that imports the SDK.

BYO key: reads ANTHROPIC_API_KEY (the SDK's own convention). Model is
RECORDS_MODEL or a sensible default. Retries rate limits with backoff,
mirroring the old repo's _invoke_with_retry.

Not imported by `records.extract.__init__` on purpose: offline tests (and
CI) must never need the SDK importable, only `FakeLLMClient`.
"""

from __future__ import annotations

import os
import time

from records.extract.llm import LLMResponse

DEFAULT_MODEL = "claude-sonnet-5"


class AnthropicClient:
    def __init__(self, model: str | None = None, max_retries: int = 3):
        import anthropic  # lazy: only production paths pay this import

        self._anthropic = anthropic
        self._client = anthropic.Anthropic()  # key from ANTHROPIC_API_KEY
        self.model = model or os.environ.get("RECORDS_MODEL", DEFAULT_MODEL)
        self.max_retries = max_retries

    def complete(
        self,
        *,
        system: str,
        user_content: str,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> LLMResponse:
        for attempt in range(self.max_retries):
            try:
                response = self._client.messages.create(
                    model=self.model,
                    max_tokens=max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": user_content}],
                )
                break
            except self._anthropic.RateLimitError:
                if attempt == self.max_retries - 1:
                    raise
                time.sleep(2**attempt)
        text = "".join(block.text for block in response.content if block.type == "text")
        return LLMResponse(
            text=text,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            model=self.model,
        )
