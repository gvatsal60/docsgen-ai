"""Persistent, non-secret configuration (``config.json`` in the data directory).

Layout::

    {
      "llm": {"active": ProviderConfig | null, "saved": {provider_id: ProviderConfig}},
      "mcp_servers": [MCPServerConfig, ...],
      "builtin_tool_states": {tool_name: bool},
      "agent_permissions": {agent_role: [tool_name, ...]},
      "agent_permissions_customized": bool,
      "settings": {setting_name: value}
    }
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, ClassVar

logger = logging.getLogger(__name__)

_SECRET_KEY_HINTS = ("api_key", "secret", "token", "password", "authorization")


class ConfigurationStore:
    _locks: ClassVar[dict[str, threading.Lock]] = {}

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = self._locks.setdefault(str(path.resolve()), threading.Lock())

    def load(self) -> dict[str, Any]:
        with self._lock:
            return self._load_unlocked()

    def _load_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("Configuration file is unreadable; starting with defaults.")
            return {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.load().get(key, default)

    def update(self, key: str, value: Any) -> None:
        with self._lock:
            data = self._load_unlocked()
            data[key] = value
            self._assert_no_secrets(value, path=key)
            self._save_unlocked(data)

    def _save_unlocked(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".cfg-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, default=str, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    @staticmethod
    def _assert_no_secrets(value: Any, path: str) -> None:
        """Defensive check: secret-looking keys must never be persisted in plain text."""
        if isinstance(value, dict):
            for k, v in value.items():
                key = str(k).lower()
                if (
                    any(h in key for h in _SECRET_KEY_HINTS)
                    and isinstance(v, str)
                    and v
                    and not key.endswith("_name")
                    and key not in {"secret_env_keys", "secret_header_names"}
                ):
                    raise ValueError(
                        f"Refusing to store secret-like value '{path}.{k}' in plain"
                        " configuration."
                    )
                ConfigurationStore._assert_no_secrets(v, f"{path}.{k}")
        elif isinstance(value, list):
            for i, item in enumerate(value):
                ConfigurationStore._assert_no_secrets(item, f"{path}[{i}]")
