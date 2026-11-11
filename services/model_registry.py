"""Provider specifications and model discovery.

Provider specs describe which fields the UI renders for each provider. Model names are never
hard-coded in agents: they come from live discovery (provider APIs) or from the editable suggestion
catalog in ``data/model_catalog.json``, and users can always type a model name manually.
"""

from __future__ import annotations

import importlib.util
import json
import logging
from pathlib import Path

import httpx

from config import PROJECT_ROOT
from models.provider_models import (
    DiscoveryResult,
    FieldSpec,
    ProviderConfig,
    ProviderId,
    ProviderSpec,
)
from services.security import sanitize_error, validate_url

logger = logging.getLogger(__name__)
CATALOG_PATH = PROJECT_ROOT / "data" / "model_catalog.json"
DISCOVERY_TIMEOUT = 15.0

_TOKEN_PARAM = FieldSpec(
    key="token_param",
    label="Token limit parameter",
    kind="select",
    options=["max_tokens", "max_completion_tokens", "none"],
    default="max_tokens",
    help=(
        "Some newer OpenAI models require 'max_completion_tokens'. Choose 'none' to"
        " omit the limit."
    ),
)
_SEND_TEMPERATURE = FieldSpec(
    key="send_temperature",
    label="Send temperature",
    kind="bool",
    default=True,
    help=(
        "Disable for models that reject the temperature parameter (for example some"
        " reasoning models)."
    ),
)

PROVIDER_SPECS: dict[ProviderId, ProviderSpec] = {
    ProviderId.OPENAI: ProviderSpec(
        id=ProviderId.OPENAI,
        label="OpenAI",
        description=(
            "OpenAI models, or any OpenAI-compatible endpoint via a custom base URL."
        ),
        strands_module="strands.models.openai",
        strands_class="OpenAIModel",
        pip_extra="openai",
        supports_discovery=True,
        docs_url="https://strandsagents.com/latest/documentation/docs/user-guide/concepts/model-providers/openai/",
        fields=[
            FieldSpec(
                key="api_key",
                label="API key",
                kind="secret",
                required=True,
                env_var="OPENAI_API_KEY",
            ),
            FieldSpec(
                key="base_url",
                label="Base URL (optional)",
                kind="url",
                help="Leave empty for api.openai.com.",
            ),
            FieldSpec(
                key="organization", label="Organization ID (optional)", kind="text"
            ),
            _TOKEN_PARAM,
            _SEND_TEMPERATURE,
        ],
    ),
    ProviderId.ANTHROPIC: ProviderSpec(
        id=ProviderId.ANTHROPIC,
        label="Anthropic Claude",
        description="Claude models through the Anthropic API.",
        strands_module="strands.models.anthropic",
        strands_class="AnthropicModel",
        pip_extra="anthropic",
        supports_discovery=True,
        docs_url="https://strandsagents.com/latest/documentation/docs/user-guide/concepts/model-providers/anthropic/",
        fields=[
            FieldSpec(
                key="api_key",
                label="API key",
                kind="secret",
                required=True,
                env_var="ANTHROPIC_API_KEY",
            )
        ],
    ),
    ProviderId.GEMINI: ProviderSpec(
        id=ProviderId.GEMINI,
        label="Google Gemini",
        description="Gemini models through the Google Gen AI SDK.",
        strands_module="strands.models.gemini",
        strands_class="GeminiModel",
        pip_extra="gemini",
        supports_discovery=True,
        docs_url="https://strandsagents.com/latest/documentation/docs/user-guide/concepts/model-providers/gemini/",
        fields=[
            FieldSpec(
                key="api_key",
                label="API key",
                kind="secret",
                required=True,
                env_var="GOOGLE_API_KEY",
            )
        ],
    ),
    ProviderId.BEDROCK: ProviderSpec(
        id=ProviderId.BEDROCK,
        label="Amazon Bedrock",
        description=(
            "Models on Amazon Bedrock using access keys, a named profile, an IAM role,"
            " a Bedrock API key or the default AWS credential chain."
        ),
        strands_module="strands.models.bedrock",
        strands_class="BedrockModel",
        pip_extra="",
        supports_discovery=True,
        model_hint=(
            "Use a model ID or inference profile ID, e.g. one listed by discovery."
        ),
        docs_url="https://strandsagents.com/latest/documentation/docs/user-guide/concepts/model-providers/amazon-bedrock/",  # noqa: E501
        fields=[
            FieldSpec(
                key="auth_mode",
                label="Authentication",
                kind="select",
                options=[
                    "default_chain",
                    "access_keys",
                    "profile",
                    "assume_role",
                    "bedrock_api_key",
                ],
                default="default_chain",
                help=(
                    "default_chain uses the standard AWS credential chain (environment,"
                    " shared config, instance/task role)."
                ),
            ),
            FieldSpec(
                key="region",
                label="AWS Region",
                kind="text",
                required=True,
                default="us-east-1",
            ),
            FieldSpec(
                key="aws_access_key_id",
                label="AWS Access Key ID",
                kind="secret",
                show_if={"auth_mode": ["access_keys", "assume_role"]},
            ),
            FieldSpec(
                key="aws_secret_access_key",
                label="AWS Secret Access Key",
                kind="secret",
                show_if={"auth_mode": ["access_keys", "assume_role"]},
            ),
            FieldSpec(
                key="aws_session_token",
                label="AWS Session Token (optional)",
                kind="secret",
                show_if={"auth_mode": ["access_keys", "assume_role"]},
            ),
            FieldSpec(
                key="profile_name",
                label="AWS profile name",
                kind="text",
                show_if={"auth_mode": ["profile", "assume_role"]},
            ),
            FieldSpec(
                key="role_arn",
                label="IAM role ARN to assume",
                kind="text",
                show_if={"auth_mode": ["assume_role"]},
            ),
            FieldSpec(
                key="bedrock_api_key",
                label="Bedrock API key",
                kind="secret",
                show_if={"auth_mode": ["bedrock_api_key"]},
            ),
        ],
    ),
    ProviderId.OLLAMA: ProviderSpec(
        id=ProviderId.OLLAMA,
        label="Ollama",
        description="Local or self-hosted models served by Ollama.",
        strands_module="strands.models.ollama",
        strands_class="OllamaModel",
        pip_extra="ollama",
        supports_discovery=True,
        docs_url="https://strandsagents.com/latest/documentation/docs/user-guide/concepts/model-providers/ollama/",
        fields=[
            FieldSpec(
                key="host",
                label="Server URL",
                kind="url",
                required=True,
                default="http://localhost:11434",
            )
        ],
    ),
    ProviderId.LITELLM: ProviderSpec(
        id=ProviderId.LITELLM,
        label="LiteLLM (100+ providers)",
        description=(
            "Any provider supported by LiteLLM (Azure OpenAI, Groq, Cohere, Together,"
            " OpenRouter, …). Use 'provider/model' names."
        ),
        strands_module="strands.models.litellm",
        strands_class="LiteLLMModel",
        pip_extra="litellm",
        model_hint="Format: provider/model, e.g. groq/llama-3.3-70b-versatile",
        docs_url="https://strandsagents.com/latest/documentation/docs/user-guide/concepts/model-providers/litellm/",
        fields=[
            FieldSpec(
                key="api_key",
                label="API key",
                kind="secret",
                help="Passed to LiteLLM as api_key.",
            ),
            FieldSpec(key="api_base", label="API base URL (optional)", kind="url"),
            FieldSpec(
                key="api_version",
                label="API version (optional, e.g. Azure)",
                kind="text",
            ),
        ],
    ),
    ProviderId.MISTRAL: ProviderSpec(
        id=ProviderId.MISTRAL,
        label="Mistral AI",
        description="Mistral models through the Mistral API.",
        strands_module="strands.models.mistral",
        strands_class="MistralModel",
        pip_extra="mistral",
        supports_discovery=True,
        docs_url="https://strandsagents.com/latest/documentation/docs/user-guide/concepts/model-providers/mistral/",
        fields=[
            FieldSpec(
                key="api_key",
                label="API key",
                kind="secret",
                required=True,
                env_var="MISTRAL_API_KEY",
            )
        ],
    ),
    ProviderId.WRITER: ProviderSpec(
        id=ProviderId.WRITER,
        label="Writer",
        description="Writer Palmyra models.",
        strands_module="strands.models.writer",
        strands_class="WriterModel",
        pip_extra="writer",
        docs_url="https://strandsagents.com/latest/documentation/docs/user-guide/concepts/model-providers/writer/",
        fields=[
            FieldSpec(
                key="api_key",
                label="API key",
                kind="secret",
                required=True,
                env_var="WRITER_API_KEY",
            )
        ],
    ),
    ProviderId.LLAMAAPI: ProviderSpec(
        id=ProviderId.LLAMAAPI,
        label="Llama API (Meta)",
        description="Meta Llama models through the Llama API.",
        strands_module="strands.models.llamaapi",
        strands_class="LlamaAPIModel",
        pip_extra="llamaapi",
        docs_url="https://strandsagents.com/latest/documentation/docs/user-guide/concepts/model-providers/llamaapi/",
        fields=[
            FieldSpec(
                key="api_key",
                label="API key",
                kind="secret",
                required=True,
                env_var="LLAMA_API_KEY",
            )
        ],
    ),
    ProviderId.LLAMACPP: ProviderSpec(
        id=ProviderId.LLAMACPP,
        label="llama.cpp server",
        description="A local llama.cpp server exposing the OpenAI-compatible API.",
        strands_module="strands.models.llamacpp",
        strands_class="LlamaCppModel",
        pip_extra="",
        supports_discovery=True,
        docs_url="https://strandsagents.com/latest/documentation/docs/user-guide/concepts/model-providers/llamacpp/",
        fields=[
            FieldSpec(
                key="base_url",
                label="Server URL",
                kind="url",
                required=True,
                default="http://localhost:8080",
            )
        ],
    ),
}


def get_spec(provider: ProviderId | str) -> ProviderSpec:
    return PROVIDER_SPECS[ProviderId(provider)]


def list_specs() -> list[ProviderSpec]:
    return list(PROVIDER_SPECS.values())


def is_installed(spec: ProviderSpec) -> bool:
    """Whether the provider's Strands module and SDK dependency can be imported."""
    try:
        return importlib.util.find_spec(
            spec.strands_module
        ) is not None and _sdk_available(spec)
    except (ImportError, ValueError):
        return False


_SDK_MODULES = {
    ProviderId.OPENAI: "openai",
    ProviderId.ANTHROPIC: "anthropic",
    ProviderId.GEMINI: "google.genai",
    ProviderId.BEDROCK: "boto3",
    ProviderId.OLLAMA: "ollama",
    ProviderId.LITELLM: "litellm",
    ProviderId.MISTRAL: "mistralai",
    ProviderId.WRITER: "writerai",
    ProviderId.LLAMAAPI: "llama_api_client",
    ProviderId.LLAMACPP: "httpx",
}


def _sdk_available(spec: ProviderSpec) -> bool:
    module = _SDK_MODULES.get(spec.id)
    if not module:
        return True
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def install_hint(spec: ProviderSpec) -> str:
    extra = spec.pip_extra
    return (
        f'pip install "strands-agents[{extra}]"'
        if extra
        else "pip install strands-agents boto3"
    )


# ---------------------------------------------------------------------------------- Catalog
def load_catalog(path: Path = CATALOG_PATH) -> dict[str, list[str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {
            k: [str(m) for m in v]
            for k, v in data.items()
            if not k.startswith("_") and isinstance(v, list)
        }
    except (OSError, ValueError):
        return {}


def catalog_models(provider: ProviderId) -> list[str]:
    return load_catalog().get(provider.value, [])


# -------------------------------------------------------------------------------- Discovery
def discover_models(
    config: ProviderConfig, secrets: dict[str, str], *, allow_http_remote: bool = False
) -> DiscoveryResult:
    """List models available to the configured account, falling back to the suggestion catalog."""
    try:
        models = _discover(config, secrets, allow_http_remote=allow_http_remote)
        if models:
            return DiscoveryResult(models=sorted(set(models)), source="discovered")
        return DiscoveryResult(
            models=catalog_models(config.provider),
            source="catalog",
            error="Discovery returned no models.",
        )
    except NotImplementedError:
        return DiscoveryResult(models=catalog_models(config.provider), source="catalog")
    except Exception as exc:  # noqa: BLE001 - shown to the user, sanitized
        logger.info(
            "Model discovery failed for %s: %s",
            config.provider.value,
            sanitize_error(exc),
        )
        return DiscoveryResult(
            models=catalog_models(config.provider),
            source="catalog",
            error=sanitize_error(exc),
        )


def _get_json(
    url: str,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
) -> dict:
    with httpx.Client(timeout=DISCOVERY_TIMEOUT, follow_redirects=False) as client:
        response = client.get(url, headers=headers or {}, params=params)
        if response.status_code in (401, 403):
            raise PermissionError(
                f"Authentication failed (HTTP {response.status_code}). Check the API"
                " key."
            )
        response.raise_for_status()
        return response.json()


def _discover(
    config: ProviderConfig, secrets: dict[str, str], *, allow_http_remote: bool
) -> list[str]:
    s = config.settings
    p = config.provider
    if p == ProviderId.OPENAI:
        key = secrets.get("api_key")
        if not key:
            raise PermissionError("An API key is required for model discovery.")
        base = (s.get("base_url") or "https://api.openai.com/v1").rstrip("/")
        validate_url(base, allow_http_remote=allow_http_remote)
        data = _get_json(f"{base}/models", headers={"Authorization": f"Bearer {key}"})
        return [m["id"] for m in data.get("data", []) if "id" in m]
    if p == ProviderId.ANTHROPIC:
        key = secrets.get("api_key")
        if not key:
            raise PermissionError("An API key is required for model discovery.")
        data = _get_json(
            "https://api.anthropic.com/v1/models",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            params={"limit": "100"},
        )
        return [m["id"] for m in data.get("data", []) if "id" in m]
    if p == ProviderId.GEMINI:
        key = secrets.get("api_key")
        if not key:
            raise PermissionError("An API key is required for model discovery.")
        data = _get_json(
            "https://generativelanguage.googleapis.com/v1beta/models",
            headers={"x-goog-api-key": key},
            params={"pageSize": "200"},
        )
        return [
            m["name"].removeprefix("models/")
            for m in data.get("models", [])
            if "generateContent" in m.get("supportedGenerationMethods", [])
        ]
    if p == ProviderId.MISTRAL:
        key = secrets.get("api_key")
        if not key:
            raise PermissionError("An API key is required for model discovery.")
        data = _get_json(
            "https://api.mistral.ai/v1/models",
            headers={"Authorization": f"Bearer {key}"},
        )
        return [
            m["id"]
            for m in data.get("data", [])
            if m.get("capabilities", {}).get("completion_chat", True)
        ]
    if p == ProviderId.OLLAMA:
        host = validate_url(
            s.get("host") or "http://localhost:11434",
            allow_http_remote=allow_http_remote,
        ).rstrip("/")
        data = _get_json(f"{host}/api/tags")
        return [m["name"] for m in data.get("models", []) if "name" in m]
    if p == ProviderId.LLAMACPP:
        base = validate_url(
            s.get("base_url") or "http://localhost:8080",
            allow_http_remote=allow_http_remote,
        ).rstrip("/")
        data = _get_json(f"{base}/v1/models")
        return [m["id"] for m in data.get("data", []) if "id" in m]
    if p == ProviderId.BEDROCK:
        from services.provider_factory import build_boto_session

        session = build_boto_session(config, secrets)
        if session is None:
            raise NotImplementedError  # Bedrock API key mode: no control-plane access
        client = session.client("bedrock", region_name=s.get("region") or "us-east-1")
        ids = [
            m["modelId"]
            for m in client.list_foundation_models(byOutputModality="TEXT").get(
                "modelSummaries", []
            )
            if "ON_DEMAND" in m.get("inferenceTypesSupported", [])
        ]
        try:
            profiles = client.list_inference_profiles(maxResults=1000).get(
                "inferenceProfileSummaries", []
            )
            ids.extend(
                p_["inferenceProfileId"]
                for p_ in profiles
                if p_.get("status", "ACTIVE") == "ACTIVE"
            )
        except Exception as exc:  # noqa: BLE001 - older accounts/regions may not support profiles
            logger.debug(
                "Bedrock inference profiles unavailable: %s",
                sanitize_error(str(exc), 200),
            )
        return ids
    raise NotImplementedError
