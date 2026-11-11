"""Credential manager for LLM providers.

Keys are namespaced as ``llm/<provider>/<field>``. Values can be session-only or persisted encrypted.
Optional environment fallbacks (e.g. ``OPENAI_API_KEY``) are supported but never required.
"""

from __future__ import annotations

import os

from models.provider_models import ProviderId, ProviderSpec
from services.security import Redactor, mask_secret
from storage.credential_store import LayeredCredentialStore


class CredentialManager:
    def __init__(self, store: LayeredCredentialStore) -> None:
        self.store = store

    @staticmethod
    def _key(namespace: str, name: str) -> str:
        return f"{namespace}/{name}"

    # Generic API -----------------------------------------------------------------------------
    def set_secret(self, namespace: str, name: str, value: str, persist: bool) -> None:
        value = (value or "").strip()
        if not value:
            return
        Redactor.register(value)
        self.store.set(self._key(namespace, name), value, persist=persist)

    def get_secret(self, namespace: str, name: str) -> str | None:
        return self.store.get(self._key(namespace, name))

    def has_secret(self, namespace: str, name: str) -> bool:
        return self.get_secret(namespace, name) is not None

    def secret_source(self, namespace: str, name: str) -> str | None:
        return self.store.source(self._key(namespace, name))

    def masked(self, namespace: str, name: str) -> str:
        return mask_secret(self.get_secret(namespace, name))

    def delete_secret(self, namespace: str, name: str) -> None:
        self.store.delete(self._key(namespace, name))

    def clear_namespace(self, namespace: str) -> int:
        return self.store.delete_prefix(namespace + "/")

    # LLM helpers -----------------------------------------------------------------------------
    @staticmethod
    def llm_namespace(provider: ProviderId) -> str:
        return f"llm/{provider.value}"

    def provider_secrets(self, spec: ProviderSpec) -> dict[str, str]:
        """Resolve secrets for a provider: UI-entered values first, then optional environment fallback."""
        values: dict[str, str] = {}
        namespace = self.llm_namespace(spec.id)
        for field in spec.secret_fields():
            value = self.get_secret(namespace, field.key)
            if value is None and field.env_var:
                value = os.environ.get(field.env_var)
                if value:
                    Redactor.register(value)
            if value:
                values[field.key] = value
        return values

    def provider_secret_status(self, spec: ProviderSpec) -> dict[str, str]:
        """For the UI: where each secret comes from (session / persistent / environment / missing)."""
        namespace = self.llm_namespace(spec.id)
        status: dict[str, str] = {}
        for field in spec.secret_fields():
            source = self.secret_source(namespace, field.key)
            if source is None and field.env_var and os.environ.get(field.env_var):
                source = f"environment ({field.env_var})"
            status[field.key] = source or "missing"
        return status
