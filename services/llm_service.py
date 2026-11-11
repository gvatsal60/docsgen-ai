"""Centralized LLM service (the "LLM Manager").

Every agent obtains its model through :meth:`LLMService.get_model`. Applying a new configuration
increments :attr:`revision`, so all subsequent agent executions use the new provider/model without
restarting the application.
"""

from __future__ import annotations

import concurrent.futures
import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from models.provider_models import (
    ConnectionStatus,
    DiscoveryResult,
    ProviderConfig,
    ProviderId,
)
from services import model_registry, provider_factory
from services.credential_manager import CredentialManager
from services.security import sanitize_error
from storage.configuration_store import ConfigurationStore

logger = logging.getLogger(__name__)

TEST_PROMPT = "This is a connectivity test. Reply with the single word OK."

ModelFactory = Callable[[ProviderConfig, dict[str, str]], Any]


class LLMNotConfiguredError(RuntimeError):
    pass


class LLMService:
    def __init__(
        self,
        config_store: ConfigurationStore,
        credentials: CredentialManager,
        *,
        allow_http_remote: Callable[[], bool] = lambda: False,
        model_factory: ModelFactory | None = None,
    ) -> None:
        self._store = config_store
        self._credentials = credentials
        self._allow_http_remote = allow_http_remote
        self._factory: ModelFactory = model_factory or (
            lambda cfg, secrets: provider_factory.create_model(
                cfg, secrets, allow_http_remote=self._allow_http_remote()
            )
        )
        self._lock = threading.Lock()
        self._active: ProviderConfig | None = None
        self.revision = 0
        self.status = ConnectionStatus()
        self._load()

    # ------------------------------------------------------------------ persistence
    def _load(self) -> None:
        llm = self._store.get("llm", {}) or {}
        active = llm.get("active")
        if active:
            try:
                self._active = ProviderConfig(**active)
                self.revision = 1
            except ValueError:
                logger.warning("Saved LLM configuration is invalid and was ignored.")

    def saved_configs(self) -> dict[str, ProviderConfig]:
        llm = self._store.get("llm", {}) or {}
        result = {}
        for key, value in (llm.get("saved") or {}).items():
            try:
                result[key] = ProviderConfig(**value)
            except ValueError:
                continue
        return result

    def saved_config_for(self, provider: ProviderId) -> ProviderConfig | None:
        return self.saved_configs().get(provider.value)

    def save(self, config: ProviderConfig, *, make_default: bool = True) -> None:
        """Persist the non-secret configuration (secrets are stored by the credential manager)."""
        llm = self._store.get("llm", {}) or {}
        saved = llm.get("saved") or {}
        saved[config.provider.value] = config.model_dump(mode="json")
        llm["saved"] = saved
        if make_default:
            llm["active"] = config.model_dump(mode="json")
        self._store.update("llm", llm)

    # ------------------------------------------------------------------ active configuration
    @property
    def active_config(self) -> ProviderConfig | None:
        return self._active

    def apply(self, config: ProviderConfig) -> None:
        """Make ``config`` the configuration used by all subsequent agent runs."""
        secrets = self._secrets_for(config)
        problems = provider_factory.validate_config(
            config, secrets, allow_http_remote=self._allow_http_remote()
        )
        if problems:
            raise provider_factory.ProviderConfigError(" ".join(problems))
        with self._lock:
            changed = (
                self._active is None or self._active.model_dump() != config.model_dump()
            )
            self._active = config
            self.revision += 1
            if changed:
                self.status = ConnectionStatus()
        logger.info(
            "LLM configuration applied: provider=%s model=%s (revision %d)",
            config.provider.value,
            config.model_id,
            self.revision,
        )

    def clear_active(self) -> None:
        with self._lock:
            self._active = None
            self.revision += 1
            self.status = ConnectionStatus()

    def is_configured(self) -> bool:
        return self._active is not None and bool(self._active.model_id)

    def describe(self) -> str:
        if not self._active:
            return "Not configured"
        spec = model_registry.get_spec(self._active.provider)
        return f"{spec.label} · {self._active.model_id}"

    # ------------------------------------------------------------------ models
    def _secrets_for(self, config: ProviderConfig) -> dict[str, str]:
        return self._credentials.provider_secrets(
            model_registry.get_spec(config.provider)
        )

    def build_model(self, config: ProviderConfig):
        return self._factory(config, self._secrets_for(config))

    def get_model(self):
        """Return a fresh model instance for the active configuration."""
        config = self._active
        if config is None:
            raise LLMNotConfiguredError(
                "No AI provider is configured. Open 'AI Provider Configuration' first."
            )
        return self.build_model(config)

    def discover_models(self, config: ProviderConfig) -> DiscoveryResult:
        return model_registry.discover_models(
            config,
            self._secrets_for(config),
            allow_http_remote=self._allow_http_remote(),
        )

    # ------------------------------------------------------------------ connection test
    def test_connection(
        self, config: ProviderConfig | None = None, timeout: float = 60.0
    ) -> ConnectionStatus:
        """Validate, initialize the model through Strands, send a tiny request and verify the reply."""
        from strands import Agent

        config = config or self._active
        if config is None:
            return ConnectionStatus(ok=False, message="No configuration to test.")
        started = time.monotonic()

        def _run() -> str:
            model = self.build_model(config)
            agent = Agent(
                model=model,
                tools=[],
                callback_handler=None,
                system_prompt="You are a connectivity check. Answer briefly.",
            )
            result = agent(TEST_PROMPT)
            return str(result).strip()

        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="llm-test"
        )
        try:
            reply = executor.submit(_run).result(timeout=timeout)
            ok = bool(reply)
            message = (
                f"Connected. Model replied: {reply[:80]!r}"
                if ok
                else "The model returned an empty response."
            )
        except concurrent.futures.TimeoutError:
            ok, message = False, f"No response within {int(timeout)} seconds."
        except Exception as exc:  # noqa: BLE001 - displayed to the user
            ok, message = False, sanitize_error(exc)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        status = ConnectionStatus(
            ok=ok,
            message=message,
            provider=config.provider,
            model_id=config.model_id,
            latency_ms=int((time.monotonic() - started) * 1000),
            tested_at=datetime.now(),
        )
        if (
            self._active is not None
            and config.model_dump() == self._active.model_dump()
        ):
            self.status = status
        return status
