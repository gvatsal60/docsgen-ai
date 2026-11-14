"""Application configuration.

Settings are resolved in this order (later wins):
1. Built-in defaults.
2. Streamlit secrets (``.streamlit/secrets.toml``) under the ``docsgen`` table, if available.
3. Environment variables prefixed with ``DOCSGEN_``.
4. Values saved from the Application Settings page (``config.json`` in the data directory).

Provider credentials and MCP secrets are *not* settings; they are entered in the UI and stored
through :mod:`storage.credential_store`.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

APP_NAME = "DocsGen"
APP_VERSION = "1.0.0"
PROJECT_ROOT = Path(__file__).resolve().parent

DEFAULT_STDIO_ALLOWLIST = [
    "python",
    "python3",
    "uv",
    "uvx",
    "npx",
    "node",
    "docker",
    "deno",
    "bunx",
]


class AppSettings(BaseModel):
    """Runtime settings that users may change from the Application Settings page."""

    data_dir: Path = Field(default=PROJECT_ROOT / ".docsgen_data")
    max_upload_mb: int = Field(default=25, ge=1, le=500)
    max_source_files: int = Field(default=20, ge=1, le=200)
    log_level: str = "INFO"

    # Agent execution
    agent_timeout_seconds: int = Field(default=300, ge=10, le=3600)
    agent_context_chars: int = Field(default=60_000, ge=5_000, le=1_000_000)
    tool_result_max_chars: int = Field(default=12_000, ge=1_000, le=200_000)

    # MCP security
    mcp_startup_timeout: int = Field(default=30, ge=5, le=300)
    mcp_tool_timeout: int = Field(default=60, ge=5, le=900)
    stdio_command_allowlist: list[str] = Field(
        default_factory=lambda: list(DEFAULT_STDIO_ALLOWLIST)
    )
    allow_insecure_http_localhost: bool = True
    allow_insecure_http_remote: bool = False
    new_tools_enabled_by_default: bool = True

    # Credentials
    default_persist_credentials: bool = False

    # Housekeeping
    upload_retention_hours: int = Field(default=24, ge=1, le=24 * 30)

    @field_validator("log_level")
    @classmethod
    def _level(cls, value: str) -> str:
        value = value.upper()
        if value not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ValueError("log_level must be DEBUG, INFO, WARNING or ERROR")
        return value

    @field_validator("stdio_command_allowlist")
    @classmethod
    def _allowlist(cls, value: list[str]) -> list[str]:
        cleaned = sorted({v.strip() for v in value if v and v.strip()})
        for item in cleaned:
            if any(ch in item for ch in " ;|&$`<>\n"):
                raise ValueError(f"Invalid command in allowlist: {item!r}")
        return cleaned

    # Derived paths -------------------------------------------------------------------------
    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def generated_dir(self) -> Path:
        return self.data_dir / "generated"

    @property
    def config_file(self) -> Path:
        return self.data_dir / "config.json"

    @property
    def credentials_file(self) -> Path:
        return self.data_dir / "credentials.enc"

    @property
    def master_key_file(self) -> Path:
        return self.data_dir / "master.key"

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.uploads_dir, self.generated_dir):
            path.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.data_dir, 0o700)
        except OSError:  # pragma: no cover - platform dependent
            pass


# Fields that may be changed from the UI (data_dir is fixed per process for safety).
EDITABLE_SETTINGS = [name for name in AppSettings.model_fields if name != "data_dir"]


def _from_streamlit_secrets() -> dict[str, Any]:
    try:
        import streamlit as st

        section = st.secrets.get("docsgen", {})  # type: ignore[attr-defined]
        return dict(section) if section else {}
    except Exception:  # noqa: BLE001 - secrets are optional
        return {}


def _from_env() -> dict[str, Any]:
    values: dict[str, Any] = {}
    for name, field in AppSettings.model_fields.items():
        raw = os.environ.get(f"DOCSGEN_{name.upper()}")
        if raw is None:
            continue
        annotation = field.annotation
        if annotation is bool:
            values[name] = raw.strip().lower() in {"1", "true", "yes", "on"}
        elif annotation is int:
            values[name] = int(raw)
        elif name == "stdio_command_allowlist":
            values[name] = [v for v in raw.split(",") if v.strip()]
        else:
            values[name] = raw
    return values


def load_settings(overrides: dict[str, Any] | None = None) -> AppSettings:
    """Build settings from defaults, secrets, environment and saved overrides."""
    values: dict[str, Any] = {}
    values.update(_from_streamlit_secrets())
    values.update(_from_env())
    if overrides:
        values.update({k: v for k, v in overrides.items() if k in EDITABLE_SETTINGS})
    settings = AppSettings(**values)
    settings.ensure_dirs()
    return settings


_LOGGING_CONFIGURED = False


def setup_logging(level: str = "INFO") -> None:
    """Configure root logging once, with credential redaction on every handler."""
    global _LOGGING_CONFIGURED
    from services.security import RedactingFilter

    root = logging.getLogger()
    if not _LOGGING_CONFIGURED:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        root.addHandler(stream_handler)
        _LOGGING_CONFIGURED = True
    for handler in root.handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(RedactingFilter())
    root.setLevel(level)
    # Third-party libraries can be chatty and may echo request payloads at DEBUG level.
    for noisy in (
        "httpx",
        "httpx2",
        "httpcore",
        "botocore",
        "urllib3",
        "openai",
        "anthropic",
        "google_genai",
        "LiteLLM",
        "mcp",
        "strands",
    ):
        logging.getLogger(noisy).setLevel(
            max(logging.WARNING, logging.getLevelName(level))
        )
