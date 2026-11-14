"""Live end-to-end tests against a real LLM provider. Skipped unless credentials are configured.

Enable with, for example (then run ``pytest tests/integration -m live``):
    DOCSGEN_LIVE_PROVIDER=openai DOCSGEN_LIVE_MODEL=gpt-4o-mini OPENAI_API_KEY=...
    DOCSGEN_LIVE_PROVIDER=anthropic DOCSGEN_LIVE_MODEL=claude-3-5-haiku-latest ANTHROPIC_API_KEY=...
    DOCSGEN_LIVE_PROVIDER=bedrock DOCSGEN_LIVE_MODEL=us.amazon.nova-lite-v1:0 AWS_REGION=us-east-1
"""

from __future__ import annotations

import os

import pytest

from models.agent_models import TaskType
from models.provider_models import ProviderConfig, ProviderId
from services.workflow_service import WorkflowService
from tests.conftest import load_sources, load_template

PROVIDER = os.environ.get("DOCSGEN_LIVE_PROVIDER")
MODEL = os.environ.get("DOCSGEN_LIVE_MODEL")

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not (PROVIDER and MODEL),
        reason=(
            "Set DOCSGEN_LIVE_PROVIDER and DOCSGEN_LIVE_MODEL (plus the provider's"
            " credentials) to run live tests."
        ),
    ),
]


@pytest.fixture()
def live(container):
    provider = ProviderId(PROVIDER)
    settings = (
        {
            "region": os.environ.get("AWS_REGION", "us-east-1"),
            "auth_mode": "default_chain",
        }
        if provider == ProviderId.BEDROCK
        else {}
    )
    if provider == ProviderId.OLLAMA:
        settings["host"] = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    cfg = ProviderConfig(
        provider=provider,
        model_id=MODEL,
        temperature=0.0,
        max_tokens=4000,
        settings=settings,
    )
    container.llm.apply(cfg)
    return container


def test_live_connection(live):
    status = live.llm.test_connection()
    assert status.ok, status.message


def test_live_workflow_end_to_end(live, demo_stdio_config, tmp_path):
    load_template(live)
    load_sources(live, names=("sample_source.txt", "sample_budget.csv"))
    state = WorkflowService.new_state(
        TaskType.POPULATE_PLACEHOLDERS,
        "Populate the placeholders from the sources. Do not invent values.",
    )
    live.workflow.run_pipeline(state, live.document_context())
    assert state.drafts, state.events
    assert (
        "Contoso" in state.drafts.get("client_name").content
        if "client_name" in state.drafts
        else True
    )
    WorkflowService.approve_all(state)
    record = live.workflow.generate_document(
        state, live.document_context(), 1, live.llm.describe(), ai_validation=False
    )
    assert os.path.exists(record.docx_path)
