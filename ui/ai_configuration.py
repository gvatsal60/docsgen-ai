"""AI Provider Configuration page: provider, credentials, model, parameters, test/save/apply/clear."""

from __future__ import annotations

from typing import Any

import streamlit as st

from models.provider_models import FieldSpec, ProviderConfig, ProviderId
from services import model_registry
from services.container import ServiceContainer
from services.provider_factory import ProviderConfigError, _visible, validate_config
from ui.components import badge, flash, page_header, show_flashes


def _base_config(c: ServiceContainer, provider: ProviderId) -> ProviderConfig:
    active = c.llm.active_config
    if active and active.provider == provider:
        return active
    saved = c.llm.saved_config_for(provider)
    if saved:
        return saved
    spec = model_registry.get_spec(provider)
    defaults = {
        f.key: f.default for f in spec.setting_fields() if f.default is not None
    }
    return ProviderConfig(provider=provider, model_id="", settings=defaults)


def _setting_widget(field: FieldSpec, value: Any, key: str) -> Any:
    if field.kind == "select":
        options = field.options
        index = (
            options.index(value)
            if value in options
            else (options.index(field.default) if field.default in options else 0)
        )
        return st.selectbox(
            field.label, options, index=index, key=key, help=field.help or None
        )
    if field.kind == "bool":
        return st.toggle(
            field.label,
            value=bool(value if value is not None else field.default),
            key=key,
            help=field.help or None,
        )
    if field.kind == "number":
        return st.number_input(
            field.label,
            value=float(value or field.default or 0),
            key=key,
            help=field.help or None,
        )
    return st.text_input(
        field.label,
        value=str(value or ""),
        key=key,
        help=field.help or None,
        placeholder=str(field.default or ""),
    )


def render(c: ServiceContainer) -> None:
    page_header(
        "AI Provider Configuration",
        "Choose the LLM used by every agent. Changes apply immediately to new agent"
        " runs — no restart needed.",
        "🧠",
    )
    clear_secret_widgets()
    show_flashes()

    specs = model_registry.list_specs()
    labels = {
        s.id: (
            s.label
            + ("" if model_registry.is_installed(s) else "  (SDK not installed)")
        )
        for s in specs
    }
    current = c.llm.active_config.provider if c.llm.active_config else ProviderId.OPENAI
    provider = st.selectbox(
        "AI provider",
        [s.id for s in specs],
        index=[s.id for s in specs].index(current),
        format_func=lambda p: labels[p],
        key="ai_provider",
    )
    spec = model_registry.get_spec(provider)
    base = _base_config(c, provider)
    pk = f"ai_{provider.value}"

    left, right = st.columns([3, 2], gap="large")
    with right:
        with st.container(border=True):
            st.markdown("**Active configuration**")
            if c.llm.active_config:
                st.write(c.llm.describe())
                status = c.llm.status
                label = (
                    "Not tested"
                    if status.ok is None
                    else ("Connected" if status.ok else "Error")
                )
                st.markdown(
                    badge(
                        label,
                        (
                            "ok"
                            if status.ok
                            else ("not tested" if status.ok is None else "error")
                        ),
                    )
                )
                if status.ok is not None:
                    st.caption(f"{status.message} ({status.latency_ms} ms)")
            else:
                st.markdown(badge("Not configured", "disabled"))
            st.caption(f"Configuration revision: {c.llm.revision}")
        st.caption(spec.description)
        if spec.docs_url:
            st.caption(f"[Strands provider documentation]({spec.docs_url})")
        if not model_registry.is_installed(spec):
            st.warning(
                "This provider's SDK is not installed. Run:"
                f" `{model_registry.install_hint(spec)}`"
            )

    with left:
        st.markdown("#### Credentials and connection")
        secret_status = c.credentials.provider_secret_status(spec)
        new_secrets: dict[str, str] = {}
        settings: dict[str, Any] = {}
        # Settings first (some secrets depend on e.g. Bedrock auth mode)
        for field in spec.setting_fields():
            if field.show_if and not _visible(
                field.show_if, {**base.settings, **settings}
            ):
                continue
            settings[field.key] = _setting_widget(
                field, base.settings.get(field.key, field.default), f"{pk}_{field.key}"
            )
        for field in spec.secret_fields():
            if field.show_if and not _visible(field.show_if, settings):
                continue
            source = secret_status.get(field.key, "missing")
            namespace = c.credentials.llm_namespace(provider)
            masked = c.credentials.masked(namespace, field.key)
            hint = (
                f"Saved ({masked}, {source}). Leave empty to keep."
                if source not in ("missing",) and not source.startswith("environment")
                else (
                    f"Using {source}. Enter a value to override."
                    if source.startswith("environment")
                    else "Not set"
                )
            )
            value = st.text_input(
                field.label + (" *" if field.required else ""),
                type="password",
                key=f"{pk}_secret_{field.key}",
                placeholder=hint,
                help=field.help or None,
            )
            st.caption(hint)
            if value:
                new_secrets[field.key] = value

        st.markdown("#### Model")
        cache_key = f"{pk}_models"
        discovered = st.session_state.get(cache_key)
        cols = st.columns([1, 1])
        if cols[0].button(
            "Discover models",
            key=f"{pk}_discover",
            disabled=not spec.supports_discovery,
            help="Query the provider for the models available to your account.",
        ):
            _store_secrets(
                c,
                provider,
                new_secrets,
                st.session_state.get(f"{pk}_persist") == "Encrypted on this machine",
            )
            new_secrets.clear()
            with st.spinner("Listing models…"):
                result = c.llm.discover_models(
                    ProviderConfig(
                        provider=provider,
                        model_id=base.model_id or "-",
                        settings=settings,
                    )
                )
            st.session_state[cache_key] = result.model_dump()
            discovered = st.session_state[cache_key]
        model_options = (discovered or {}).get(
            "models"
        ) or model_registry.catalog_models(provider)
        if discovered:
            if discovered.get("error"):
                cols[1].warning(f"Discovery: {discovered['error']}")
            else:
                cols[1].success(
                    f"{len(model_options)} model(s)"
                    f" {'discovered' if discovered.get('source') == 'discovered' else 'from catalog'}"
                )
        if base.model_id and base.model_id not in model_options:
            model_options = [base.model_id] + model_options
        choice = st.selectbox(
            "Model",
            model_options or ["(enter a model name below)"],
            index=(
                model_options.index(base.model_id)
                if base.model_id in model_options
                else 0
            ),
            key=f"{pk}_model_select",
            help=spec.model_hint or None,
        )
        custom = st.text_input(
            "Custom model name (overrides the selection)",
            key=f"{pk}_model_custom",
            placeholder=spec.model_hint or "e.g. a fine-tuned or newly released model",
        )
        model_id = (custom or (choice if model_options else "")).strip()

        st.markdown("#### Parameters")
        pcols = st.columns(2)
        temperature = None
        max_tokens = None
        if spec.supports_temperature:
            use_temp = pcols[0].toggle(
                "Set temperature",
                value=base.temperature is not None,
                key=f"{pk}_use_temp",
            )
            temperature = (
                pcols[0].slider(
                    "Temperature",
                    0.0,
                    1.0,
                    float(base.temperature if base.temperature is not None else 0.2),
                    0.05,
                    key=f"{pk}_temp",
                    disabled=not use_temp,
                )
                if use_temp
                else None
            )
        if spec.supports_max_tokens:
            max_tokens = int(
                pcols[1].number_input(
                    "Maximum output tokens",
                    min_value=256,
                    max_value=200_000,
                    value=int(base.max_tokens or 4096),
                    step=256,
                    key=f"{pk}_maxtok",
                )
            )

        persist_choice = st.radio(
            "Credential storage",
            ["Session only", "Encrypted on this machine"],
            index=1 if c.settings.default_persist_credentials else 0,
            horizontal=True,
            key=f"{pk}_persist",
            help=(
                "Session-only credentials disappear when you close the browser tab."
                " Encrypted credentials are stored with Fernet encryption in the data"
                " directory."
            ),
        )
        persist = persist_choice == "Encrypted on this machine"

        config = ProviderConfig(
            provider=provider,
            model_id=model_id,
            temperature=temperature,
            max_tokens=max_tokens,
            settings=settings,
        )

        b1, b2, b3, b4 = st.columns(4)
        test = b1.button(
            "Test connection", key=f"{pk}_test", type="secondary", width="stretch"
        )
        save = b2.button("Save configuration", key=f"{pk}_save", width="stretch")
        apply = b3.button(
            "Apply configuration", key=f"{pk}_apply", type="primary", width="stretch"
        )
        clear = b4.button("Clear credentials", key=f"{pk}_clear", width="stretch")

        if test or save or apply:
            _store_secrets(c, provider, new_secrets, persist)
            problems = validate_config(
                config,
                c.credentials.provider_secrets(spec),
                allow_http_remote=c.settings.allow_insecure_http_remote,
            )
            if problems:
                for problem in problems:
                    st.error(problem)
            else:
                if test:
                    with st.spinner(
                        "Initializing the model through Strands and sending a test"
                        " request…"
                    ):
                        status = c.llm.test_connection(config)
                    (st.success if status.ok else st.error)(
                        f"{status.message} ({status.latency_ms} ms)"
                    )
                if save:
                    c.llm.save(config, make_default=True)
                    flash(
                        "Configuration saved. It will be restored as the default next"
                        " time."
                        + (
                            ""
                            if persist
                            else (
                                " Credentials are session-only and must be re-entered"
                                " next time."
                            )
                        )
                    )
                    st.rerun()
                if apply:
                    try:
                        c.llm.apply(config)
                    except ProviderConfigError as exc:
                        st.error(str(exc))
                    else:
                        flash(
                            f"Applied: {c.llm.describe()}. All subsequent agent runs"
                            " use this model."
                        )
                        st.rerun()
        if clear:
            removed = c.credentials.clear_namespace(
                c.credentials.llm_namespace(provider)
            )
            flash(f"Removed {removed} stored credential(s) for {spec.label}.", "info")
            st.rerun()


def _store_secrets(
    c: ServiceContainer, provider: ProviderId, secrets: dict[str, str], persist: bool
) -> None:
    namespace = c.credentials.llm_namespace(provider)
    for key, value in secrets.items():
        c.credentials.set_secret(namespace, key, value, persist=persist)
    if secrets:
        # Clear the password widgets on the next run so secrets do not linger in widget state.
        st.session_state.setdefault("_clear_secret_widgets", []).extend(
            f"ai_{provider.value}_secret_{k}" for k in secrets
        )


def clear_secret_widgets() -> None:
    for key in st.session_state.pop("_clear_secret_widgets", []):
        st.session_state.pop(key, None)
