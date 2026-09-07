"""AI Assistant Service for PipViper IDE.

Encapsulates local AI sidecar interaction and health monitoring (PRD O5, U2, S7).
Strictly local-only (localhost / 127.0.0.1). Zero telemetry or external endpoints.
"""

from __future__ import annotations

import logging
import urllib.parse
import urllib.request
from typing import Optional

from PySide6.QtCore import QObject, Signal

from ..pip_viper import ProcessError, query_local_llm

_LOGGER = logging.getLogger("src.services.ai")


class AIService(QObject):
    """Manages communication and health checks for the local AI assistant."""

    status_changed = Signal(bool, str)  # is_connected, message

    _instance: Optional[AIService] = None

    def __init__(
        self,
        endpoint_url: str = "http://127.0.0.1:11434/v1",
        default_model: str = "llama3",
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._endpoint_url = endpoint_url
        self._model = default_model
        self._is_connected = False

    @classmethod
    def get_instance(cls) -> AIService:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @property
    def endpoint_url(self) -> str:
        return self._endpoint_url

    def set_endpoint_url(self, url: str) -> None:
        self._endpoint_url = url.strip()

    @property
    def model_name(self) -> str:
        return self._model

    def set_model_name(self, model: str) -> None:
        self._model = model.strip()

    def check_health(self, timeout: float = 3.0) -> bool:
        """Check if local AI sidecar is reachable."""
        parsed = urllib.parse.urlparse(self._endpoint_url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        try:
            req = urllib.request.Request(f"{base_url}/api/tags", headers={"User-Agent": "PipViper-IDE/7.0.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                self._is_connected = resp.status == 200
                msg = "Connected to local Ollama sidecar." if self._is_connected else f"HTTP {resp.status}"
                self.status_changed.emit(self._is_connected, msg)
                return self._is_connected
        except Exception as exc:
            self._is_connected = False
            self.status_changed.emit(False, f"Local AI sidecar offline: {exc}")
            return False

    def query(
        self,
        system_prompt: str,
        user_prompt: str,
        timeout: float = 30.0,
    ) -> str:
        """Query the local AI sidecar with input validation."""
        return query_local_llm(
            api_url=self._endpoint_url,
            model_name=self._model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            timeout=timeout,
        )
