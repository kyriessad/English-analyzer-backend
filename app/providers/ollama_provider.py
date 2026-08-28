from __future__ import annotations

from typing import Any, Callable

import requests


class OllamaProvider:
    name = "ollama"

    def __init__(
        self,
        *,
        base_url: str,
        http_post: Callable[..., Any] | None = None,
        http_get: Callable[..., Any] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._post = http_post or requests.post
        self._get = http_get or requests.get

    def generate(self, payload: dict[str, Any], *, timeout_seconds: float) -> Any:
        return self._post(
            f"{self.base_url}/api/generate",
            json=payload,
            timeout=timeout_seconds,
        )

    def stream(self, payload: dict[str, Any], *, timeout_seconds: float) -> Any:
        return self._post(
            f"{self.base_url}/api/generate",
            json=payload,
            stream=True,
            timeout=timeout_seconds,
        )

    def health_check(self, *, timeout_seconds: float = 5.0) -> bool:
        try:
            response = self._get(f"{self.base_url}/api/tags", timeout=timeout_seconds)
        except requests.exceptions.RequestException:
            return False
        return 200 <= response.status_code < 300
