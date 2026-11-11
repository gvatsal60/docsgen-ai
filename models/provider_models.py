"""Pydantic models describing LLM providers and their configuration."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class ProviderId(str, Enum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"
    BEDROCK = "bedrock"
    OLLAMA = "ollama"
    LITELLM = "litellm"
    MISTRAL = "mistral"
    WRITER = "writer"
    LLAMAAPI = "llamaapi"
    LLAMACPP = "llamacpp"


FieldKind = Literal["secret", "text", "url", "select", "number", "bool"]


class FieldSpec(BaseModel):
    """A provider-specific configuration field rendered dynamically in the UI."""

    key: str
    label: str
    kind: FieldKind = "text"
    required: bool = False
    help: str = ""
    default: Any = None
    options: list[str] = Field(default_factory=list)
    env_var: str | None = None  # optional environment fallback for secrets
    show_if: dict[str, Any] | None = None  # e.g. {"auth_mode": "access_keys"}

    @property
    def is_secret(self) -> bool:
        return self.kind == "secret"


class ProviderSpec(BaseModel):
    """Static description of a provider supported through Strands Agents."""

    id: ProviderId
    label: str
    description: str
    strands_module: str
    strands_class: str
    pip_extra: str
    fields: list[FieldSpec]
    supports_discovery: bool = False
    supports_temperature: bool = True
    supports_max_tokens: bool = True
    model_hint: str = ""
    docs_url: str = ""

    def secret_fields(self) -> list[FieldSpec]:
        return [f for f in self.fields if f.is_secret]

    def setting_fields(self) -> list[FieldSpec]:
        return [f for f in self.fields if not f.is_secret]


class ProviderConfig(BaseModel):
    """Non-secret provider configuration. Secrets are held by the credential manager."""

    provider: ProviderId
    model_id: str = ""
    temperature: float | None = Field(default=0.2, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=4096, ge=1, le=1_000_000)
    settings: dict[str, Any] = Field(default_factory=dict)

    @field_validator("model_id")
    @classmethod
    def _strip(cls, value: str) -> str:
        return (value or "").strip()

    def label(self) -> str:
        return f"{self.provider.value}:{self.model_id or '—'}"


class ConnectionStatus(BaseModel):
    ok: bool | None = None  # None = never tested
    message: str = "Not tested"
    provider: ProviderId | None = None
    model_id: str | None = None
    latency_ms: int | None = None
    tested_at: datetime | None = None


class DiscoveryResult(BaseModel):
    models: list[str] = Field(default_factory=list)
    source: Literal["discovered", "catalog", "none"] = "none"
    error: str | None = None
