"""Create Strands model instances from a :class:`ProviderConfig` and resolved secrets.

This is the only module that knows how each Strands provider class is constructed. Imports are lazy
so that missing optional SDKs only affect the provider that needs them.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

from models.provider_models import ProviderConfig, ProviderId
from services.model_registry import get_spec, install_hint
from services.security import validate_url

logger = logging.getLogger(__name__)


class ProviderConfigError(ValueError):
    """The configuration is incomplete or invalid."""


class ProviderDependencyError(RuntimeError):
    """The SDK needed by the provider is not installed."""


def validate_config(
    config: ProviderConfig, secrets: dict[str, str], *, allow_http_remote: bool = False
) -> list[str]:
    """Return a list of human-readable validation problems (empty when valid)."""
    spec = get_spec(config.provider)
    problems: list[str] = []
    if not config.model_id:
        problems.append("Select or enter a model.")
    settings = config.settings
    for field in spec.fields:
        if field.show_if and not _visible(field.show_if, settings):
            continue
        value = (
            secrets.get(field.key)
            if field.is_secret
            else settings.get(field.key, field.default)
        )
        if field.required and (value is None or value == ""):
            problems.append(f"{field.label} is required.")
        if field.kind == "url" and value:
            try:
                validate_url(str(value), allow_http_remote=allow_http_remote)
            except ValueError as exc:
                problems.append(f"{field.label}: {exc}")
    if config.provider == ProviderId.BEDROCK:
        mode = settings.get("auth_mode", "default_chain")
        has_keys = secrets.get("aws_access_key_id") and secrets.get(
            "aws_secret_access_key"
        )
        if mode == "access_keys" and not has_keys:
            problems.append(
                "AWS access key ID and secret access key are required for access-key"
                " authentication."
            )
        if mode == "assume_role" and not settings.get("role_arn"):
            problems.append("An IAM role ARN is required to assume a role.")
        if mode == "profile" and not settings.get("profile_name"):
            problems.append("A profile name is required for profile authentication.")
        if mode == "bedrock_api_key" and not secrets.get("bedrock_api_key"):
            problems.append("A Bedrock API key is required.")
    return problems


def _visible(show_if: dict[str, Any], settings: dict[str, Any]) -> bool:
    for key, allowed in show_if.items():
        value = settings.get(key)
        allowed_values = allowed if isinstance(allowed, list) else [allowed]
        if value not in allowed_values:
            return False
    return True


def _load_class(config: ProviderConfig) -> type:
    spec = get_spec(config.provider)
    try:
        module = importlib.import_module(spec.strands_module)
    except ImportError as exc:
        raise ProviderDependencyError(
            f"The {spec.label} provider requires an optional dependency. Install it"
            f" with: {install_hint(spec)}"
        ) from exc
    return getattr(module, spec.strands_class)


def build_boto_session(config: ProviderConfig, secrets: dict[str, str]):
    """Build a boto3 session for Bedrock according to the selected authentication mode.

    Returns ``None`` for Bedrock API key mode, where no AWS session is used.
    """
    import boto3

    settings = config.settings
    mode = settings.get("auth_mode", "default_chain")
    region = settings.get("region") or "us-east-1"
    if mode == "bedrock_api_key":
        return None
    if mode == "access_keys":
        return boto3.Session(
            aws_access_key_id=secrets.get("aws_access_key_id"),
            aws_secret_access_key=secrets.get("aws_secret_access_key"),
            aws_session_token=secrets.get("aws_session_token") or None,
            region_name=region,
        )
    if mode == "profile":
        return boto3.Session(
            profile_name=settings.get("profile_name"), region_name=region
        )
    if mode == "assume_role":
        if secrets.get("aws_access_key_id") and secrets.get("aws_secret_access_key"):
            base = boto3.Session(
                aws_access_key_id=secrets.get("aws_access_key_id"),
                aws_secret_access_key=secrets.get("aws_secret_access_key"),
                aws_session_token=secrets.get("aws_session_token") or None,
                region_name=region,
            )
        elif settings.get("profile_name"):
            base = boto3.Session(
                profile_name=settings.get("profile_name"), region_name=region
            )
        else:
            base = boto3.Session(region_name=region)
        creds = base.client("sts").assume_role(
            RoleArn=settings["role_arn"], RoleSessionName="docsgen"
        )["Credentials"]
        from services.security import Redactor

        Redactor.register(creds["SecretAccessKey"])
        Redactor.register(creds["SessionToken"])
        return boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            region_name=region,
        )
    return boto3.Session(region_name=region)  # default credential chain


def _common_params(
    config: ProviderConfig, max_tokens_key: str = "max_tokens"
) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if config.temperature is not None and config.settings.get("send_temperature", True):
        params["temperature"] = config.temperature
    if config.max_tokens and max_tokens_key != "none":
        params[max_tokens_key] = config.max_tokens
    return params


def create_model(
    config: ProviderConfig, secrets: dict[str, str], *, allow_http_remote: bool = False
):
    """Instantiate the Strands model for ``config``. Raises ProviderConfigError / ProviderDependencyError."""
    problems = validate_config(config, secrets, allow_http_remote=allow_http_remote)
    if problems:
        raise ProviderConfigError(" ".join(problems))
    cls = _load_class(config)
    s = config.settings
    p = config.provider

    if p == ProviderId.OPENAI:
        client_args: dict[str, Any] = {"api_key": secrets["api_key"]}
        if s.get("base_url"):
            client_args["base_url"] = s["base_url"]
        if s.get("organization"):
            client_args["organization"] = s["organization"]
        params = _common_params(config, s.get("token_param", "max_tokens"))
        return cls(client_args=client_args, model_id=config.model_id, params=params)

    if p == ProviderId.ANTHROPIC:
        params = {}
        if config.temperature is not None:
            params["temperature"] = config.temperature
        return cls(
            client_args={"api_key": secrets["api_key"]},
            model_id=config.model_id,
            max_tokens=config.max_tokens or 4096,
            params=params,
        )

    if p == ProviderId.GEMINI:
        params = _common_params(config, "max_output_tokens")
        return cls(
            client_args={"api_key": secrets["api_key"]},
            model_id=config.model_id,
            params=params,
        )

    if p == ProviderId.BEDROCK:
        kwargs: dict[str, Any] = {"model_id": config.model_id}
        if config.temperature is not None:
            kwargs["temperature"] = config.temperature
        if config.max_tokens:
            kwargs["max_tokens"] = config.max_tokens
        region = s.get("region") or "us-east-1"
        if s.get("auth_mode") == "bedrock_api_key":
            return cls(api_key=secrets["bedrock_api_key"], region_name=region, **kwargs)
        # Strands rejects region_name together with boto_session; the session carries the region.
        return cls(boto_session=build_boto_session(config, secrets), **kwargs)

    if p == ProviderId.OLLAMA:
        kwargs = {"model_id": config.model_id}
        if config.temperature is not None:
            kwargs["temperature"] = config.temperature
        if config.max_tokens:
            kwargs["max_tokens"] = config.max_tokens
        return cls(s.get("host") or "http://localhost:11434", **kwargs)

    if p == ProviderId.LITELLM:
        client_args = {}
        if secrets.get("api_key"):
            client_args["api_key"] = secrets["api_key"]
        if s.get("api_base"):
            client_args["api_base"] = s["api_base"]
        if s.get("api_version"):
            client_args["api_version"] = s["api_version"]
        return cls(
            client_args=client_args,
            model_id=config.model_id,
            params=_common_params(config),
        )

    if p == ProviderId.MISTRAL:
        kwargs = {"model_id": config.model_id}
        if config.temperature is not None:
            kwargs["temperature"] = config.temperature
        if config.max_tokens:
            kwargs["max_tokens"] = config.max_tokens
        return cls(api_key=secrets["api_key"], **kwargs)

    if p == ProviderId.WRITER:
        kwargs = {"model_id": config.model_id}
        if config.temperature is not None:
            kwargs["temperature"] = config.temperature
        if config.max_tokens:
            kwargs["max_tokens"] = config.max_tokens
        return cls(client_args={"api_key": secrets["api_key"]}, **kwargs)

    if p == ProviderId.LLAMAAPI:
        kwargs = {"model_id": config.model_id}
        if config.temperature is not None:
            kwargs["temperature"] = config.temperature
        if config.max_tokens:
            kwargs["max_completion_tokens"] = config.max_tokens
        return cls(client_args={"api_key": secrets["api_key"]}, **kwargs)

    if p == ProviderId.LLAMACPP:
        return cls(
            base_url=s.get("base_url") or "http://localhost:8080",
            model_id=config.model_id or "default",
            params=_common_params(config),
        )

    raise ProviderConfigError(f"Unsupported provider: {p}")
