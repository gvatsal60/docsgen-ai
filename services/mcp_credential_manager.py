"""Secret storage for MCP servers (tokens, API keys, secret headers, secret env vars, OAuth secrets).

Keys: ``mcp/<server_id>/<kind>[:<name>]``. Values never leave this module except when building a
client connection; the UI only receives masked values.
"""

from __future__ import annotations

from services.credential_manager import CredentialManager

BEARER = "bearer_token"
API_KEY = "api_key"
OAUTH_SECRET = "oauth_client_secret"


class MCPCredentialManager:
    def __init__(self, credentials: CredentialManager) -> None:
        self._credentials = credentials

    @staticmethod
    def namespace(server_id: str) -> str:
        return f"mcp/{server_id}"

    def set(self, server_id: str, name: str, value: str, persist: bool) -> None:
        self._credentials.set_secret(
            self.namespace(server_id), name, value, persist=persist
        )

    def get(self, server_id: str, name: str) -> str | None:
        return self._credentials.get_secret(self.namespace(server_id), name)

    def has(self, server_id: str, name: str) -> bool:
        return self._credentials.has_secret(self.namespace(server_id), name)

    def masked(self, server_id: str, name: str) -> str:
        return self._credentials.masked(self.namespace(server_id), name)

    def source(self, server_id: str, name: str) -> str | None:
        return self._credentials.secret_source(self.namespace(server_id), name)

    def delete(self, server_id: str, name: str) -> None:
        self._credentials.delete_secret(self.namespace(server_id), name)

    def clear(self, server_id: str) -> int:
        return self._credentials.clear_namespace(self.namespace(server_id))

    # Typed helpers ---------------------------------------------------------------------------
    @staticmethod
    def header_key(header: str) -> str:
        return f"header:{header.lower()}"

    @staticmethod
    def env_key(env_name: str) -> str:
        return f"env:{env_name}"

    def secret_headers(self, server_id: str, names: list[str]) -> dict[str, str]:
        result = {}
        for name in names:
            value = self.get(server_id, self.header_key(name))
            if value:
                result[name] = value
        return result

    def secret_env(self, server_id: str, names: list[str]) -> dict[str, str]:
        result = {}
        for name in names:
            value = self.get(server_id, self.env_key(name))
            if value:
                result[name] = value
        return result
