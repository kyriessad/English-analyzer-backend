from __future__ import annotations

from typing import Any

from app.providers.ai_provider import CloudAIProviderNotConfigured


class CloudAIProvider:
    name = "cloud"

    def __init__(self, *, base_url: str, api_key: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    def _not_configured(self) -> None:
        raise CloudAIProviderNotConfigured(
            "Cloud AI provider boundary exists, but no vendor adapter is configured yet."
        )

    def generate(self, payload: dict[str, Any], *, timeout_seconds: float) -> Any:
        self._not_configured()

    def stream(self, payload: dict[str, Any], *, timeout_seconds: float) -> Any:
        self._not_configured()

    def health_check(self, *, timeout_seconds: float = 5.0) -> bool:
        self._not_configured()
