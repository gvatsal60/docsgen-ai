"""Pydantic models for MCP server configuration, runtime state and discovered tools."""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
_HEADER_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,128}$")
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


class TransportType(str, Enum):
    STDIO = "stdio"
    STREAMABLE_HTTP = "streamable_http"
    SSE = "sse"


TRANSPORT_LABELS = {
    TransportType.STDIO: "stdio (local process)",
    TransportType.STREAMABLE_HTTP: "Streamable HTTP",
    TransportType.SSE: "SSE (legacy HTTP)",
}


class AuthType(str, Enum):
    NONE = "none"
    BEARER = "bearer"
    API_KEY_HEADER = "api_key_header"
    CUSTOM_HEADERS = "custom_headers"
    OAUTH_CLIENT_CREDENTIALS = "oauth_client_credentials"


AUTH_LABELS = {
    AuthType.NONE: "None",
    AuthType.BEARER: "Bearer token",
    AuthType.API_KEY_HEADER: "API key header",
    AuthType.CUSTOM_HEADERS: "Custom headers only",
    AuthType.OAUTH_CLIENT_CREDENTIALS: (
        "OAuth 2.0 client credentials (Streamable HTTP only)"
    ),
}


class ConnectPolicy(str, Enum):
    MANUAL = "manual"
    ON_STARTUP = "on_startup"


class MCPServerConfig(BaseModel):
    """Persisted (non-secret) configuration of an MCP server.

    Secret values (tokens, API keys, secret headers, secret environment variables, OAuth client
    secrets) are stored separately by :class:`services.mcp_credential_manager.MCPCredentialManager`.
    Only their *names* are kept here.
    """

    id: str
    name: str
    description: str = ""
    enabled: bool = True
    transport: TransportType = TransportType.STDIO

    # stdio
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    secret_env_keys: list[str] = Field(default_factory=list)
    cwd: str | None = None

    # HTTP-based
    url: str | None = None
    auth_type: AuthType = AuthType.NONE
    api_key_header_name: str = "X-API-Key"
    headers: dict[str, str] = Field(default_factory=dict)
    secret_header_names: list[str] = Field(default_factory=list)
    oauth_client_id: str | None = None
    oauth_scopes: list[str] = Field(default_factory=list)

    # Execution
    startup_timeout: int = Field(default=30, ge=5, le=300)
    tool_timeout: int = Field(default=60, ge=5, le=900)
    connect_policy: ConnectPolicy = ConnectPolicy.MANUAL
    tool_prefix: str | None = None

    # Tool selection: tool original name -> enabled
    tool_states: dict[str, bool] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        value = (value or "").strip().lower()
        if not _ID_RE.match(value):
            raise ValueError(
                "Server identifier must be 2–32 characters: lowercase letters, digits"
                " and underscores, starting with a letter."
            )
        return value

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise ValueError("Server name is required.")
        return value[:80]

    @field_validator("env")
    @classmethod
    def _env(cls, value: dict[str, str]) -> dict[str, str]:
        for key in value:
            if not _ENV_RE.match(key):
                raise ValueError(f"Invalid environment variable name: {key!r}")
        return value

    @field_validator("secret_env_keys")
    @classmethod
    def _secret_env(cls, value: list[str]) -> list[str]:
        for key in value:
            if not _ENV_RE.match(key):
                raise ValueError(f"Invalid environment variable name: {key!r}")
        return sorted(set(value))

    @field_validator("headers")
    @classmethod
    def _headers(cls, value: dict[str, str]) -> dict[str, str]:
        for key, val in value.items():
            if not _HEADER_RE.match(key):
                raise ValueError(f"Invalid HTTP header name: {key!r}")
            if "\n" in val or "\r" in val:
                raise ValueError(f"Header {key!r} contains a line break.")
        return value

    @field_validator("secret_header_names")
    @classmethod
    def _secret_headers(cls, value: list[str]) -> list[str]:
        for key in value:
            if not _HEADER_RE.match(key):
                raise ValueError(f"Invalid HTTP header name: {key!r}")
        return sorted(set(value))

    @field_validator("api_key_header_name")
    @classmethod
    def _api_header(cls, value: str) -> str:
        value = (value or "X-API-Key").strip()
        if not _HEADER_RE.match(value):
            raise ValueError("Invalid API key header name.")
        return value

    @field_validator("tool_prefix")
    @classmethod
    def _prefix(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        if not re.match(r"^[A-Za-z][A-Za-z0-9_]{0,23}$", value):
            raise ValueError("Tool prefix must be 1–24 letters, digits or underscores.")
        return value

    @model_validator(mode="after")
    def _transport_fields(self) -> MCPServerConfig:
        if self.transport == TransportType.STDIO:
            if not self.command:
                raise ValueError("stdio servers require a command.")
            if self.auth_type not in (AuthType.NONE,):
                # Authentication for stdio servers is provided through environment variables.
                self.auth_type = AuthType.NONE
        else:
            if not self.url:
                raise ValueError("HTTP-based servers require a URL.")
            if self.auth_type == AuthType.OAUTH_CLIENT_CREDENTIALS:
                if self.transport != TransportType.STREAMABLE_HTTP:
                    raise ValueError(
                        "OAuth client credentials are only supported with Streamable"
                        " HTTP."
                    )
                if not self.oauth_client_id:
                    raise ValueError("OAuth client credentials require a client ID.")
        return self

    @property
    def effective_prefix(self) -> str:
        """Prefix applied to tool names so tools from different servers never collide."""
        return self.tool_prefix or self.id

    def qualified_tool_name(self, original: str) -> str:
        return f"{self.effective_prefix}_{original}"


ServerStatus = Literal["disconnected", "connecting", "connected", "error", "disabled"]


class MCPServerState(BaseModel):
    server_id: str
    status: ServerStatus = "disconnected"
    tool_count: int = 0
    last_connected_at: datetime | None = None
    last_attempt_at: datetime | None = None
    last_error: str | None = None
    server_name_reported: str | None = None


class MCPToolInfo(BaseModel):
    """A tool discovered from an MCP server."""

    qualified_name: str
    original_name: str
    server_id: str
    server_name: str
    title: str | None = None
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    read_only: bool | None = None
    destructive: bool | None = None
    open_world: bool | None = None
    requires_approval: bool = False
    approval_reason: str | None = None


class MCPTestResult(BaseModel):
    ok: bool
    message: str
    tool_count: int = 0
    tool_names: list[str] = Field(default_factory=list)
    latency_ms: int | None = None
    diagnostics: list[str] = Field(default_factory=list)


class ToolTestResult(BaseModel):
    ok: bool
    status: Literal["success", "error", "blocked"]
    output: str
    latency_ms: int | None = None
