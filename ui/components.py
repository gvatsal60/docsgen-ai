"""Reusable UI building blocks (status badges, navigation helpers, guarded actions)."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any

import streamlit as st

from services.security import sanitize_error

NAV_KEY = "nav_page"

STATUS_COLORS = {
    "connected": "green",
    "ok": "green",
    "approved": "green",
    "success": "green",
    "enabled": "green",
    "connecting": "orange",
    "pending": "orange",
    "warning": "orange",
    "requires approval": "orange",
    "error": "red",
    "failed": "red",
    "rejected": "red",
    "blocked": "red",
    "disconnected": "gray",
    "disabled": "gray",
    "skipped": "gray",
    "not tested": "gray",
    "info": "blue",
}


def badge(text: str, kind: str | None = None) -> str:
    color = STATUS_COLORS.get((kind or text).lower(), "blue")
    safe = text.replace("[", "(").replace("]", ")")
    return f":{color}-badge[{safe}]"


PENDING_NAV_KEY = "_nav_pending"


def goto(page: str) -> None:
    """Request navigation; applied by app.py before the navigation widget is created."""
    st.session_state[PENDING_NAV_KEY] = page


def apply_pending_navigation() -> None:
    page = st.session_state.pop(PENDING_NAV_KEY, None)
    if page:
        st.session_state[NAV_KEY] = page


def goto_button(label: str, page: str, key: str, **kwargs: Any) -> None:
    st.button(label, key=key, on_click=goto, args=(page,), **kwargs)


def page_header(title: str, subtitle: str = "", icon: str = "") -> None:
    st.markdown(f"## {icon} {title}".strip())
    if subtitle:
        st.caption(subtitle)


@contextlib.contextmanager
def guarded(action: str):
    """Show errors from an action as a friendly message instead of a traceback."""
    try:
        yield
    except Exception as exc:  # noqa: BLE001 - UI boundary
        st.error(f"{action} failed: {sanitize_error(exc)}")


def flash(message: str, kind: str = "success") -> None:
    """Queue a message to show after the next rerun."""
    st.session_state.setdefault("_flash", []).append((kind, message))


def show_flashes() -> None:
    for kind, message in st.session_state.pop("_flash", []):
        getattr(
            st, kind if kind in ("success", "info", "warning", "error") else "info"
        )(message)


def confirm_button(
    label: str, key: str, confirm_label: str = "Confirm", **kwargs: Any
) -> bool:
    """Two-step button for destructive actions."""
    armed_key = f"{key}__armed"
    if st.session_state.get(armed_key):
        cols = st.columns(2)
        if cols[0].button(confirm_label, key=f"{key}__yes", type="primary"):
            st.session_state[armed_key] = False
            return True
        if cols[1].button("Cancel", key=f"{key}__no"):
            st.session_state[armed_key] = False
            st.rerun()
        return False
    if st.button(label, key=key, **kwargs):
        st.session_state[armed_key] = True
        st.rerun()
    return False


def parse_kv_lines(text: str, sep: str = "=") -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if sep not in line:
            raise ValueError(f"Expected 'NAME{sep}VALUE' but got: {line[:40]}")
        key, value = line.split(sep, 1)
        result[key.strip()] = value.strip()
    return result


def format_kv_lines(values: dict[str, str], sep: str = "=") -> str:
    return "\n".join(f"{k}{sep}{v}" for k, v in values.items())


def run_with_status(label: str, fn: Callable[[Callable[[str, str], None]], Any]) -> Any:
    """Run ``fn(progress)`` inside an ``st.status`` box that shows progress messages."""
    with st.status(label, expanded=True) as status:

        def progress(step: str, message: str) -> None:
            status.write(f"**{step}** — {message}")
            status.update(label=f"{label}: {step}")

        try:
            result = fn(progress)
        except Exception as exc:  # noqa: BLE001
            status.update(label=f"{label}: failed", state="error")
            st.error(sanitize_error(exc))
            return None
        status.update(label=f"{label}: done", state="complete", expanded=False)
        return result
