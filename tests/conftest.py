"""Shared fixtures. No test needs network access or paid API keys."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SAMPLES = ROOT / "sample_data"
DEMO_SERVER = ROOT / "mcp_servers" / "demo_server.py"


def _ensure_samples() -> None:
    if not (SAMPLES / "sample_template.docx").exists():
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "make_samples.py")], check=True
        )


_ensure_samples()


@pytest.fixture()
def data_dir(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("DOCSGEN_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("DOCSGEN_MASTER_KEY", raising=False)
    for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path / "data"


@pytest.fixture()
def container(data_dir):
    from services.container import ServiceContainer

    c = ServiceContainer.create(data_dir=data_dir)
    # allow the interpreter running the tests to start the demo MCP server
    c.settings.stdio_command_allowlist.append(Path(sys.executable).name)
    yield c
    c.shutdown()


@pytest.fixture()
def sample_template(tmp_path) -> Path:
    target = tmp_path / "template.docx"
    shutil.copy(SAMPLES / "sample_template.docx", target)
    return target


@pytest.fixture()
def demo_stdio_config():
    from models.mcp_models import MCPServerConfig, TransportType

    return MCPServerConfig(
        id="demo",
        name="Demo MCP",
        description="Bundled demo server",
        transport=TransportType.STDIO,
        command=sys.executable,
        args=[str(DEMO_SERVER)],
    )


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture()
def demo_http_server():
    """Start the demo server over Streamable HTTP with a required bearer token."""
    port = _free_port()
    env = dict(os.environ, DEMO_MCP_TOKEN="test-token-0123456789")
    proc = subprocess.Popen(
        [sys.executable, str(DEMO_SERVER), "--http", "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            break
        except OSError:
            time.sleep(0.2)
    yield {"url": f"http://127.0.0.1:{port}/mcp", "token": "test-token-0123456789"}
    proc.terminate()
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:  # pragma: no cover
        proc.kill()


def load_sources(
    container,
    names=(
        "sample_source.txt",
        "sample_budget.csv",
        "sample_specification.pdf",
        "sample_budget.xlsx",
        "sample_company_profile.docx",
    ),
):
    from services.file_service import ALLOWED_SOURCE_TYPES

    for name in names:
        stored = container.files.store(
            name, (SAMPLES / name).read_bytes(), ALLOWED_SOURCE_TYPES
        )
        doc = container.extraction.extract(stored, set(container.session.sources))
        container.session.sources[doc.doc_id] = doc


def load_template(container):
    from document_engine.template_parser import parse_template
    from services.container import UploadedTemplate
    from services.file_service import ALLOWED_TEMPLATE_TYPES

    stored = container.files.store(
        "sample_template.docx",
        (SAMPLES / "sample_template.docx").read_bytes(),
        ALLOWED_TEMPLATE_TYPES,
    )
    container.session.template = UploadedTemplate(
        stored.file_id,
        stored.original_name,
        stored.path,
        parse_template(stored.path, stored.original_name),
    )
