"""LLM configuration: provider selection, credentials, model selection, connection test, switching."""

from __future__ import annotations

import pytest
from strands.models.anthropic import AnthropicModel
from strands.models.bedrock import BedrockModel
from strands.models.gemini import GeminiModel
from strands.models.litellm import LiteLLMModel
from strands.models.ollama import OllamaModel
from strands.models.openai import OpenAIModel

from models.provider_models import ProviderConfig, ProviderId
from services import model_registry
from services.llm_service import LLMNotConfiguredError, LLMService
from services.provider_factory import ProviderConfigError, create_model, validate_config
from testkit.helpers import ScriptedModel


@pytest.mark.parametrize(
    "provider,model_id,secrets,settings,cls",
    [
        (
            ProviderId.OPENAI,
            "gpt-x",
            {"api_key": "sk-test-1234567890abcdef"},
            {"base_url": "https://llm.example.com/v1"},
            OpenAIModel,
        ),
        (
            ProviderId.ANTHROPIC,
            "claude-x",
            {"api_key": "sk-ant-test-1234567890"},
            {},
            AnthropicModel,
        ),
        (
            ProviderId.GEMINI,
            "gemini-x",
            {"api_key": "AIza-test-key-1234567890"},
            {},
            GeminiModel,
        ),
        (
            ProviderId.BEDROCK,
            "us.amazon.nova-pro-v1:0",
            {
                "aws_access_key_id": "AKIAIOSFODNN7EXAMPLE",
                "aws_secret_access_key": "secret-example",
            },
            {"auth_mode": "access_keys", "region": "eu-west-1"},
            BedrockModel,
        ),
        (
            ProviderId.BEDROCK,
            "us.amazon.nova-pro-v1:0",
            {},
            {"auth_mode": "default_chain", "region": "us-east-1"},
            BedrockModel,
        ),
        (
            ProviderId.OLLAMA,
            "llama3.1",
            {},
            {"host": "http://localhost:11434"},
            OllamaModel,
        ),
        (
            ProviderId.LITELLM,
            "groq/llama-3.3-70b-versatile",
            {"api_key": "gsk-test-123456"},
            {},
            LiteLLMModel,
        ),
    ],
)
def test_create_model_for_each_provider(provider, model_id, secrets, settings, cls):
    model = create_model(
        ProviderConfig(
            provider=provider,
            model_id=model_id,
            temperature=0.1,
            max_tokens=1000,
            settings=settings,
        ),
        secrets,
    )
    assert isinstance(model, cls)
    assert model.get_config()["model_id"] == model_id


def test_openai_parameters_and_token_param():
    cfg = ProviderConfig(
        provider=ProviderId.OPENAI,
        model_id="o4-mini",
        temperature=0.3,
        max_tokens=500,
        settings={"token_param": "max_completion_tokens", "send_temperature": False},
    )
    model = create_model(cfg, {"api_key": "sk-test-1234567890abcdef"})
    assert model.get_config()["params"] == {"max_completion_tokens": 500}


def test_validation_messages():
    assert "API key is required." in validate_config(
        ProviderConfig(provider=ProviderId.OPENAI, model_id="m"), {}
    )
    assert "Select or enter a model." in validate_config(
        ProviderConfig(provider=ProviderId.ANTHROPIC, model_id=""), {"api_key": "k"}
    )
    problems = validate_config(
        ProviderConfig(
            provider=ProviderId.BEDROCK,
            model_id="m",
            settings={"auth_mode": "assume_role", "region": "us-east-1"},
        ),
        {},
    )
    assert any("role ARN" in p for p in problems)
    problems = validate_config(
        ProviderConfig(
            provider=ProviderId.OLLAMA,
            model_id="m",
            settings={"host": "http://10.0.0.5:11434"},
        ),
        {},
    )
    # remote plain HTTP refused by default
    assert any("https" in p for p in problems)
    with pytest.raises(ProviderConfigError):
        create_model(ProviderConfig(provider=ProviderId.GEMINI, model_id="g"), {})


def test_every_provider_has_spec_and_catalog_entries():
    for spec in model_registry.list_specs():
        assert spec.fields
        assert isinstance(model_registry.catalog_models(spec.id), list)


def _service(container, factory):
    return LLMService(
        container.config_store, container.credentials, model_factory=factory
    )


def test_api_key_entry_and_provider_switching(container):
    built = []

    def factory(cfg, secrets):
        built.append((cfg.provider, cfg.model_id, sorted(secrets)))
        return ScriptedModel({})

    svc = _service(container, factory)
    with pytest.raises(LLMNotConfiguredError):
        svc.get_model()
    container.credentials.set_secret(
        "llm/openai", "api_key", "sk-session-1234567890", persist=False
    )
    svc.apply(ProviderConfig(provider=ProviderId.OPENAI, model_id="gpt-a"))
    rev = svc.revision
    svc.get_model()
    container.credentials.set_secret(
        "llm/anthropic", "api_key", "sk-ant-persist-1234567890", persist=True
    )
    svc.apply(ProviderConfig(provider=ProviderId.ANTHROPIC, model_id="claude-b"))
    svc.get_model()
    assert svc.revision == rev + 1
    assert built == [
        (ProviderId.OPENAI, "gpt-a", ["api_key"]),
        (ProviderId.ANTHROPIC, "claude-b", ["api_key"]),
    ]
    assert svc.describe() == "Anthropic Claude · claude-b"


def test_apply_rejects_incomplete_configuration(container):
    svc = _service(container, lambda c, s: ScriptedModel({}))
    with pytest.raises(ProviderConfigError):
        svc.apply(
            ProviderConfig(provider=ProviderId.OPENAI, model_id="gpt")
        )  # no API key


def test_save_and_restore_configuration(container):
    container.credentials.set_secret("llm/ollama", "dummy", "x", persist=True)
    svc = _service(container, lambda c, s: ScriptedModel({}))
    cfg = ProviderConfig(
        provider=ProviderId.OLLAMA,
        model_id="llama3.1",
        settings={"host": "http://localhost:11434"},
    )
    svc.save(cfg)
    restored = _service(container, lambda c, s: ScriptedModel({}))
    assert restored.active_config == cfg
    assert restored.saved_config_for(ProviderId.OLLAMA).model_id == "llama3.1"


def test_connection_test_success_and_sanitized_failure(container):
    svc = _service(container, lambda c, s: ScriptedModel({}, text_reply="OK"))
    container.credentials.set_secret(
        "llm/openai", "api_key", "sk-secret-abcdef123456", persist=False
    )
    cfg = ProviderConfig(provider=ProviderId.OPENAI, model_id="gpt")
    status = svc.test_connection(cfg)
    assert status.ok and "OK" in status.message and status.latency_ms is not None

    def failing(cfg_, secrets):
        raise RuntimeError(f"401 Unauthorized for key {secrets['api_key']}")

    svc_fail = _service(container, failing)
    status = svc_fail.test_connection(cfg)
    assert status.ok is False
    assert "sk-secret-abcdef123456" not in status.message and "***" in status.message


def test_env_fallback_for_secrets(container, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-1234567890abcdef")
    spec = model_registry.get_spec(ProviderId.OPENAI)
    assert (
        container.credentials.provider_secrets(spec)["api_key"]
        == "sk-env-1234567890abcdef"
    )
    assert container.credentials.provider_secret_status(spec)["api_key"].startswith(
        "environment"
    )


def test_discovery_falls_back_to_catalog(container):
    svc = _service(container, lambda c, s: ScriptedModel({}))
    result = svc.discover_models(
        ProviderConfig(provider=ProviderId.ANTHROPIC, model_id="x")
    )  # no key
    assert result.source == "catalog" and result.models and result.error
