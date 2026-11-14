"""Create Strands ``MCPClient`` instances from :class:`MCPServerConfig`.

Transports (all via the official MCP Python SDK and the Strands MCP integration):

* ``stdio`` - ``mcp.stdio_client`` with ``StdioServerParameters``. The process is started directly
  (no shell), only allow-listed executables are permitted, and the child inherits only the MCP SDK's
  safe default environment plus the variables configured for the server.
* ``streamable_http`` - ``MCPClient(url=..., headers=..., auth=...)``. Supports bearer tokens, API
  key headers, custom headers and OAuth 2.0 client credentials.
* ``sse`` - ``mcp.client.sse.sse_client`` with headers (legacy transport).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp import StdioServerParameters, stdio_client
from mcp.client.sse import sse_client
from strands.tools.mcp import MCPClient

from config import APP_NAME, APP_VERSION
from models.mcp_models import AuthType, MCPServerConfig, TransportType
from services.mcp_credential_manager import (
    API_KEY,
    BEARER,
    OAUTH_SECRET,
    MCPCredentialManager,
)
from services.security import PolicyError, validate_stdio_command, validate_url


@dataclass
class ClientPolicy:
    stdio_allowlist: list[str]
    allow_http_localhost: bool = True
    allow_http_remote: bool = False


def validate_server_config(
    cfg: MCPServerConfig,
    policy: ClientPolicy,
    secrets: MCPCredentialManager | None = None,
) -> list[str]:
    """Return validation problems for a server configuration (empty list when valid)."""
    problems: list[str] = []
    try:
        if cfg.transport == TransportType.STDIO:
            validate_stdio_command(cfg.command or "", cfg.args, policy.stdio_allowlist)
            if cfg.cwd:
                path = Path(cfg.cwd).expanduser()
                if not path.is_dir():
                    problems.append(f"Working directory does not exist: {cfg.cwd}")
            overlap = set(cfg.env) & set(cfg.secret_env_keys)
            if overlap:
                problems.append(
                    "Variables defined both as plain and secret:"
                    f" {', '.join(sorted(overlap))}"
                )
        else:
            validate_url(
                cfg.url or "",
                allow_http_localhost=policy.allow_http_localhost,
                allow_http_remote=policy.allow_http_remote,
            )
            lowered = {h.lower() for h in cfg.headers}
            if cfg.auth_type == AuthType.BEARER and "authorization" in lowered:
                problems.append(
                    "Remove the plain 'Authorization' header; the bearer token sets it."
                )
            if secrets is not None:
                if cfg.auth_type == AuthType.BEARER and not secrets.has(cfg.id, BEARER):
                    problems.append("A bearer token is required.")
                if cfg.auth_type == AuthType.API_KEY_HEADER and not secrets.has(
                    cfg.id, API_KEY
                ):
                    problems.append("An API key is required.")
                if (
                    cfg.auth_type == AuthType.OAUTH_CLIENT_CREDENTIALS
                    and not secrets.has(cfg.id, OAUTH_SECRET)
                ):
                    problems.append("An OAuth client secret is required.")
    except PolicyError as exc:
        problems.append(str(exc))
    return problems


def build_headers(
    cfg: MCPServerConfig, secrets: MCPCredentialManager
) -> dict[str, str]:
    headers = dict(cfg.headers)
    headers.update(secrets.secret_headers(cfg.id, cfg.secret_header_names))
    if cfg.auth_type == AuthType.BEARER:
        token = secrets.get(cfg.id, BEARER)
        if token:
            headers["Authorization"] = f"Bearer {token}"
    elif cfg.auth_type == AuthType.API_KEY_HEADER:
        key = secrets.get(cfg.id, API_KEY)
        if key:
            headers[cfg.api_key_header_name] = key
    return headers


def build_client(
    cfg: MCPServerConfig,
    secrets: MCPCredentialManager,
    policy: ClientPolicy,
    stderr_path: Path | None = None,
) -> MCPClient:
    """Validate the configuration and return an unstarted ``MCPClient``."""
    problems = validate_server_config(cfg, policy, secrets)
    if problems:
        raise PolicyError(" ".join(problems))

    common = {
        "startup_timeout": cfg.startup_timeout,
        "prefix": cfg.effective_prefix,
        "application_name": APP_NAME,
        "application_version": APP_VERSION,
    }

    if cfg.transport == TransportType.STDIO:
        env = dict(cfg.env)
        env.update(secrets.secret_env(cfg.id, cfg.secret_env_keys))
        params = StdioServerParameters(
            command=cfg.command or "",
            args=list(cfg.args),
            env=env or None,
            cwd=str(Path(cfg.cwd).expanduser()) if cfg.cwd else None,
        )
        # Server stderr goes to a per-server log file (shown, redacted, as diagnostics) instead of the app log.
        if stderr_path is not None:
            stderr_path.parent.mkdir(parents=True, exist_ok=True)
            errlog = open(stderr_path, "w", encoding="utf-8")  # noqa: SIM115 - lives as long as the client
        else:
            errlog = open(os.devnull, "w")  # noqa: SIM115

        def _stdio():
            return stdio_client(params, errlog=errlog)

        client = MCPClient(_stdio, **common)
        client._docsgen_errlog = errlog  # type: ignore[attr-defined]
        return client

    headers = build_headers(cfg, secrets)
    url = cfg.url or ""

    if cfg.transport == TransportType.STREAMABLE_HTTP:
        auth: dict[str, Any] | None = None
        if cfg.auth_type == AuthType.OAUTH_CLIENT_CREDENTIALS:
            auth = {
                "client_id": cfg.oauth_client_id or "",
                "client_secret": secrets.get(cfg.id, OAUTH_SECRET) or "",
            }
            if cfg.oauth_scopes:
                auth["scopes"] = list(cfg.oauth_scopes)
        return MCPClient(url=url, headers=headers or None, auth=auth, **common)

    # Legacy SSE transport
    def _sse():
        return sse_client(
            url,
            headers=headers or None,
            timeout=float(min(cfg.startup_timeout, 60)),
            sse_read_timeout=float(max(cfg.tool_timeout, 60)),
        )

    return MCPClient(_sse, **common)
