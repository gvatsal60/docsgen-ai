"""Service container: builds and wires all services for one browser session."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from config import AppSettings, load_settings, setup_logging
from models.agent_models import WorkflowState
from models.document_models import GenerationRecord, SourceDocument, TemplateStructure
from services.agent_manager import AgentManager
from services.credential_manager import CredentialManager
from services.extraction_service import ExtractionService
from services.file_service import FileService
from services.llm_service import LLMService
from services.mcp_client_factory import ClientPolicy
from services.mcp_connection_manager import MCPConnectionManager
from services.mcp_credential_manager import MCPCredentialManager
from services.mcp_manager import MCPManager
from services.workflow_service import WorkflowService
from storage.configuration_store import ConfigurationStore
from storage.credential_store import (
    EncryptedFileCredentialStore,
    LayeredCredentialStore,
    MemoryCredentialStore,
)
from tools.context import DocumentContext
from tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


@dataclass
class UploadedTemplate:
    file_id: str
    file_name: str
    path: Path
    structure: TemplateStructure


@dataclass
class SessionData:
    template: UploadedTemplate | None = None
    sources: dict[str, SourceDocument] = field(default_factory=dict)
    workflow: WorkflowState | None = None
    history: list[GenerationRecord] = field(default_factory=list)
    instructions: str = ""
    run_config: dict = field(
        default_factory=dict
    )  # task type, targets, research and approval choices


class ServiceContainer:
    def __init__(self, settings: AppSettings, config_store: ConfigurationStore) -> None:
        self.settings = settings
        self.config_store = config_store
        session_secrets = MemoryCredentialStore()
        persistent: EncryptedFileCredentialStore | None
        try:
            persistent = EncryptedFileCredentialStore(
                settings.credentials_file, settings.master_key_file
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Encrypted credential storage unavailable: %s", type(exc).__name__
            )
            persistent = None
        self.credential_store = LayeredCredentialStore(session_secrets, persistent)
        self.credentials = CredentialManager(self.credential_store)
        self.llm = LLMService(
            config_store,
            self.credentials,
            allow_http_remote=lambda: self.settings.allow_insecure_http_remote,
        )

        self.mcp_secrets = MCPCredentialManager(self.credentials)
        connections = MCPConnectionManager(
            self.mcp_secrets, self.client_policy, settings.data_dir / "logs"
        )
        self.mcp = MCPManager(
            config_store,
            self.mcp_secrets,
            self.client_policy,
            connections,
            default_enabled=lambda: self.settings.new_tools_enabled_by_default,
        )
        self.tools = ToolRegistry(config_store, self.mcp)
        self.agents = AgentManager(
            self.llm,
            self.tools,
            timeout_provider=lambda: self.settings.agent_timeout_seconds,
        )
        self.workflow = WorkflowService(
            self.agents,
            settings.generated_dir,
            budget_provider=lambda: self.settings.agent_context_chars,
        )

        self.files = FileService(
            settings.uploads_dir,
            settings.max_upload_mb,
            settings.upload_retention_hours,
        )
        self.extraction = ExtractionService()
        self.session = SessionData()
        self.startup_messages: list[str] = []

    @classmethod
    def create(cls, data_dir: Path | None = None) -> ServiceContainer:
        bootstrap = load_settings()
        if data_dir is not None:
            bootstrap = bootstrap.model_copy(update={"data_dir": data_dir})
            bootstrap.ensure_dirs()
        store = ConfigurationStore(bootstrap.config_file)
        settings = load_settings(store.get("settings", {}) or {})
        if data_dir is not None:
            settings = settings.model_copy(update={"data_dir": data_dir})
            settings.ensure_dirs()
        setup_logging(settings.log_level)
        container = cls(settings, store)
        for server_id, state in container.mcp.connect_startup_servers().items():
            if state.status != "connected":
                container.startup_messages.append(
                    f"MCP server '{server_id}' could not connect on startup:"
                    f" {state.last_error}"
                )
        return container

    # ------------------------------------------------------------------ helpers
    def client_policy(self) -> ClientPolicy:
        return ClientPolicy(
            stdio_allowlist=self.settings.stdio_command_allowlist,
            allow_http_localhost=self.settings.allow_insecure_http_localhost,
            allow_http_remote=self.settings.allow_insecure_http_remote,
        )

    def document_context(self) -> DocumentContext:
        tpl = self.session.template
        return DocumentContext(
            template_path=tpl.path if tpl else None,
            template=tpl.structure if tpl else None,
            sources=dict(self.session.sources),
            max_result_chars=self.settings.tool_result_max_chars,
        )

    def update_settings(self, values: dict) -> AppSettings:
        current = self.settings.model_dump()
        current.update(values)
        new = AppSettings(**current)
        self.config_store.update(
            "settings",
            {k: v for k, v in new.model_dump(mode="json").items() if k != "data_dir"},
        )
        self.settings = new
        setup_logging(new.log_level)
        return new

    # ------------------------------------------------------------------ lifecycle
    @staticmethod
    def shutdown_resources(
        connections: MCPConnectionManager, files: FileService
    ) -> None:
        try:
            connections.disconnect_all()
        finally:
            files.cleanup()

    def shutdown(self) -> None:
        ServiceContainer.shutdown_resources(self.mcp.connections, self.files)
