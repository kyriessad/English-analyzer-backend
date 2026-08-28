from __future__ import annotations

from typing import Any, Protocol


class AIProvider(Protocol):
    name: str

    def generate(self, payload: dict[str, Any], *, timeout_seconds: float) -> Any:
        """Run one non-streaming provider request."""

    def stream(self, payload: dict[str, Any], *, timeout_seconds: float) -> Any:
        """Open one streaming provider request."""

    def health_check(self, *, timeout_seconds: float = 5.0) -> bool:
        """Return whether the configured provider endpoint is reachable."""


class CloudAIProviderNotConfigured(RuntimeError):
    pass
