"""Per-browser-session service container stored in ``st.session_state``.

Credentials entered as "session only", live MCP connections, uploaded files and workflow state are
scoped to one Streamlit session; they are never shared between users of the same server.
"""

from __future__ import annotations

import atexit
import logging
import weakref
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from services.container import ServiceContainer

logger = logging.getLogger(__name__)
_CONTAINER_KEY = "_docsgen_container"
_live_containers: weakref.WeakSet[ServiceContainer] = weakref.WeakSet()


def get_container() -> ServiceContainer:
    import streamlit as st

    from services.container import ServiceContainer

    container = st.session_state.get(_CONTAINER_KEY)
    if container is None:
        container = ServiceContainer.create()
        st.session_state[_CONTAINER_KEY] = container
        _live_containers.add(container)
        # Disconnect MCP servers and delete uploads when the session is garbage collected.
        # The finalizer must not reference the container itself, only the resources to release.
        weakref.finalize(
            container,
            ServiceContainer.shutdown_resources,
            container.mcp.connections,
            container.files,
        )
    return container


def reset_container() -> None:
    import streamlit as st

    container = st.session_state.pop(_CONTAINER_KEY, None)
    if container is not None:
        container.shutdown()


@atexit.register
def _shutdown_all() -> None:  # pragma: no cover - process exit
    for container in list(_live_containers):
        try:
            container.shutdown()
        except Exception:
            logger.debug("Error during shutdown", exc_info=True)
