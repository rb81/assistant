"""Transport contracts shared by management API handlers and generated clients."""
from datetime import datetime
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field
from .management_domains import ReadAction, WriteAction


READ_SERVICE_REQUESTS = frozenset({
    "conversations.list", "tasks.messages", "tasks.activity", "tasks.summary",
    "records.list", "records.read", "records.children", "clients.read",
    "tasks.snapshot", "events.snapshot",
})


class HealthResponse(BaseModel):
    api_version: Literal[1]
    instance_id: str
    name: str
    capabilities: list[str]
    read_only: Literal[True]


class FullHealthResponse(BaseModel):
    api_version: Literal[2]
    instance_id: str
    name: str
    capabilities: list[str]
    read_only: Literal[False]
    revision: str = "unknown"


class ConfigurationFile(BaseModel):
    path: str
    format: Literal["yaml", "env"]
    values: dict[str, Any]


class ConfigurationResponse(BaseModel):
    setup_complete: bool
    files: list[ConfigurationFile]
    credential_names: list[str]
    read_only: Literal[True]


class ConfigurationCheckResponse(BaseModel):
    ok: bool
    problems: list[str]
    runtime_checked: bool
    containers_checked: list[str]
    settings: int
    credentials: int


class ServiceStatus(BaseModel):
    id: str
    title: str
    state: str
    health: str
    worker: bool = False
    uptime: str | None = None


class ServicesResponse(BaseModel):
    services: list[ServiceStatus]


class CredentialStatus(BaseModel):
    name: str
    status: Literal["configured", "activation pending", "removal pending"]


class CredentialsResponse(BaseModel):
    credentials: list[CredentialStatus]


class CredentialStorageStatusResponse(BaseModel):
    mode: Literal["host-files", "volume"]
    retirement_pending: bool


class GeneratedDatabasePasswordResponse(BaseModel):
    password: str


class RecordPageResponse(BaseModel):
    rows: list[dict[str, Any]]
    total: int


class TaskMessage(BaseModel):
    id: int
    kind: str
    channel: str
    body: str
    created_at: datetime
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    recipients: list[str] = Field(default_factory=list)
    external: bool = False
    subject: str | None = None


class TaskMessagesResponse(BaseModel):
    rows: list[TaskMessage]
    total: int = Field(ge=0)


class ToolCall(BaseModel):
    """One tool the assistant used, paired with its result when the result has been recorded."""

    id: str
    task_id: int
    created_at: datetime
    tool_name: str
    tool_action: str | None = None
    status: Literal["running", "succeeded", "failed", "interrupted"]
    input: Any = None
    output: Any = None
    duration_ms: int | None = None


class TaskActivityResponse(BaseModel):
    rows: list[ToolCall]
    total: int = Field(ge=0)
    truncated: bool = False


class Conversation(BaseModel):
    """One human conversation thread and the task that most recently handled it."""

    thread_id: str
    channel: str
    message_count: int = Field(ge=0)
    opening_message: str = ""
    last_activity_at: datetime
    latest_task_id: int = Field(gt=0)
    latest_task_status: str
    latest_task_summary: str | None = None
    workstream_title: str | None = None
    workstream_name_status: str | None = None


class ConversationsResponse(BaseModel):
    rows: list[Conversation]
    total: int = Field(ge=0)


class TaskStatusCount(BaseModel):
    status: str
    count: int = Field(ge=0)


class TaskSummaryResponse(BaseModel):
    jobs: list[TaskStatusCount]


class TaskDeletionPreviewResponse(BaseModel):
    task_id: int
    status: str
    thread_id: str
    workstream_id: int
    deletable: bool
    blockers: list[dict[str, Any]]
    preview_digest: str | None
    deleted: bool = False
    deleted_at: datetime | None = None
    task_summary: str | None = None
    marker: str | None = None
    related_task_count: int | None = None
    task_log_count: int | None = None
    conversation_preserved: bool | None = None
    project_data_preserved: bool | None = None


class ApprovalsResponse(BaseModel):
    approvals: list[dict[str, Any]]
    executions: list[dict[str, Any]] = Field(default_factory=list)
    limit: int
    offset: int


class ProjectFile(BaseModel):
    id: int
    path: str
    sha256: str | None
    retaining_workstreams: int
    available: bool
    size_bytes: int | None


class ProjectFilesResponse(BaseModel):
    files: list[ProjectFile]
    total: int


class ProjectFileContentResponse(BaseModel):
    id: int
    path: str
    sha256: str | None
    offset: int
    next_offset: int
    size_bytes: int
    truncated: bool
    content_base64: str


class LogsResponse(BaseModel):
    service: str
    content: str
    truncated: bool
    tail: int


class SettingField(BaseModel):
    group: str
    key: str
    label: str
    value: str
    default: str
    secret: bool
    boolean: bool
    data_type: str
    metadata: dict[str, Any]
    editable: bool = True
    edit_restriction: str | None = None


class SettingGroup(BaseModel):
    """Navigation metadata for one settings group.

    `parent` nests it inside another group's page; `section` places it within that page's section.
    """

    id: str
    title: str
    help: str | None = None
    parent: str | None = None
    section: str | None = None
    basic: bool = False
    destination: Literal[
        "assistant", "providers", "models", "connections", "knowledge", "safety", "operations", "server",
    ] = "assistant"


class FullConfigurationResponse(BaseModel):
    api_version: Literal[2]
    revision: str
    setup_complete: bool
    files: list[ConfigurationFile]
    settings: list[SettingField]
    groups: list[SettingGroup] = Field(default_factory=list)
    credential_names: list[str]
    instructions: str
    agent_yaml: str = ""
    agent_yaml_backups: list[str] = Field(default_factory=list)
    setup_notice: str
    read_only: Literal[False]


class StructuredConfigurationResponse(BaseModel):
    """Product settings without source files or saved credential values."""
    api_version: Literal[2]
    revision: str
    setup_complete: bool
    settings: list[SettingField]
    groups: list[SettingGroup]
    credential_names: list[str]
    instructions: str
    instructions_configured: bool
    setup_notice: str
    read_only: Literal[False]


class ModelSettingField(SettingField):
    """Saved setting with a redacted, inheritance-aware overview display."""
    display_value: str
    value_source: Literal["configured", "inherited", "provider_default", "credential_status", "unset"]
    inherited_from: str | None = None
    pending_activation: bool = False


class ModelSettingsLink(BaseModel):
    """Owning settings page for controls omitted from the quick model editor."""
    group: str
    title: str
    destination: Literal["assistant", "providers", "models", "connections", "knowledge", "safety", "operations", "advanced"]
    field_key: str


class ModelSettingsSection(BaseModel):
    id: str
    title: str
    enabled: bool | None = None
    provider: str
    summary_keys: list[str]
    selection_keys: list[str] | None = None
    related_settings: list[ModelSettingsLink] | None = None
    settings: list[ModelSettingField]


class ModelSettingsResponse(BaseModel):
    """One saved-configuration snapshot, not a claim about running models."""
    api_version: Literal[2]
    revision: str
    value_state: Literal["saved"]
    activation_tracked: bool
    pending_activation: bool
    sections: list[ModelSettingsSection]
    read_only: Literal[True]


class SettingChoicesResponse(BaseModel):
    """Explicit options for one setting; provider discovery failures remain visible."""
    key: str
    choices: list[str]
    choice_labels: dict[str, str]
    warnings: list[str]


class ModelReasoningCapabilities(BaseModel):
    """Advertised capability hints, never proof of a provider-enforced thinking cap."""
    supported_efforts: list[str] | None = None
    supports_max_tokens: bool = False
    mandatory: bool = False
    default_effort: str | None = None
    default_enabled: bool | None = None


class ProviderModel(BaseModel):
    id: str
    name: str
    reasoning: ModelReasoningCapabilities | None = None


class ProviderModelCatalogResponse(BaseModel):
    provider: str
    models: list[ProviderModel]
    zdr_only: bool
    warning: str | None = None


class ConfigurationPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    values: dict[
        Annotated[str, Field(min_length=1, max_length=128)],
        Annotated[str, Field(max_length=16384)],
    ] = Field(max_length=512)
    remove_credentials: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(
        default_factory=list, max_length=512,
    )
    instructions: str | None = Field(default=None, max_length=524288)
    acknowledge: bool = False
    agent_yaml: str | None = Field(default=None, max_length=524288)


class ConfigurationPreviewResponse(BaseModel):
    valid: bool
    revision: str
    preview_id: str
    problems: list[str]
    changes: list[dict[str, str]]


class ConfigurationSaveRequest(ConfigurationPreviewRequest):
    operation: Literal["configuration.save"]
    preview_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    confirmed: Literal[True]
    apply: bool = False


class ServiceLifecycleOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["service.lifecycle"]
    action: Literal["start", "stop", "restart", "apply"]
    service: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    confirmed: Literal[True]
    revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class CredentialReplaceOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["credential.replace"]
    name: str = Field(min_length=1, max_length=128)
    value: str = Field(min_length=1, max_length=16384)
    confirmed: Literal[True]


class CredentialApplyOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["credential.apply"]
    name: str | None = None
    confirmed: Literal[True]


class DatabaseResetOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["database.reset"]
    clean_install: bool = False
    keep_mailbox_position: bool = True
    confirmation: Literal["RESET"]


class DatabaseRecoveryOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["database.recover"]
    confirmed: Literal[True]


class CredentialStorageOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["credential-storage.move"]
    use_volume: bool
    confirmed: Literal[True]


class SearchMaintenanceOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["search.maintenance"]
    action: Literal["status", "probe", "reindex"]
    confirmed: bool = False


class RecordMutationOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["record.mutate"]
    action: Literal[
        "cancel", "retry", "review", "instruct", "cancel-reminder", "delete-memory",
        "update-memory", "create-memory", "pin-memory", "unpin-memory", "approve-memory",
        "reject-memory", "expire-memory", "block-script", "unblock-script", "revoke-script",
    ]
    record_id: int | None = Field(default=None, gt=0)
    body: str = Field(default="", max_length=100000)
    confirmed: Literal[True]


class ProjectDeleteOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["project.delete"]
    project_id: int = Field(gt=0)
    preview_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    confirmed: Literal[True]


class ProjectResumeOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["project.resume"]
    deletion_id: int = Field(gt=0)
    confirmed: Literal[True]


class FileUploadOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["file.upload"]
    workstream_id: int = Field(gt=0)
    path: str = Field(min_length=1, max_length=1024)
    content_base64: str = Field(max_length=1_398_104)
    confirmed: Literal[True]


class FileAttachOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["file.attach"]
    workstream_id: int = Field(gt=0)
    file_id: int = Field(gt=0)
    confirmed: Literal[True]


class TaskStartOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["task.start"]
    body: str = Field(min_length=1, max_length=100000)
    client_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    workstream_id: int | None = Field(default=None, gt=0)
    attachment_file_ids: list[Annotated[int, Field(gt=0)]] = Field(default_factory=list, max_length=20)
    confirmed: Literal[True]


class TaskReplyOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["task.reply"]
    task_id: int = Field(gt=0)
    body: str = Field(min_length=1, max_length=100000)
    client_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    attachment_file_ids: list[Annotated[int, Field(gt=0)]] = Field(default_factory=list, max_length=20)
    confirmed: Literal[True]


class TaskDeleteOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["task.delete"]
    task_id: int = Field(gt=0)
    preview_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    confirmed: Literal[True]


class ResearchStartOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["research.start"]
    question: str = Field(min_length=1, max_length=100000)
    client_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    workstream_id: int | None = Field(default=None, gt=0)
    attachment_file_ids: list[Annotated[int, Field(gt=0)]] = Field(default_factory=list, max_length=20)
    title: str | None = Field(default=None, max_length=500)
    instructions: str | None = Field(default=None, max_length=100000)
    confirmed: Literal[True]


class ApprovalDecisionOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["approval.decide"]
    approval_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    version: str = Field(min_length=1, max_length=128)
    decision: Literal["approve", "deny", "approve_once", "approve_always",
                      "block_once", "block_always", "cancel_job"]
    instruction: str = Field(default="", max_length=20000)
    confirmed: Literal[True]


class ClientRegistrationOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["client.register"]
    name: str = Field(min_length=1, max_length=120)
    client_type: Literal["desktop", "cli", "tui", "custom"] = "desktop"
    client_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    fallback_channels: list[Literal["telegram", "email"]] = Field(
        default_factory=lambda: ["telegram", "email"], max_length=2,
    )
    confirmed: Literal[True]


class ClientUnregisterOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["client.unregister"]
    client_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    confirmed: Literal[True]


class ClientDeliveryResolveOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["client.delivery-resolve"]
    client_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    delivery_id: int = Field(gt=0)
    resolution: Literal["mark-sent", "return-to-client"]
    confirmed: Literal[True]


class ClientReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: Literal["clients.read"]
    action: Literal["list", "poll", "acknowledge", "reconcile", "history"]
    client_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    message_ids: list[Annotated[int, Field(gt=0)]] = Field(default_factory=list, max_length=100)
    before_id: int | None = Field(default=None, gt=0)


class ClientMessage(BaseModel):
    id: int
    task_id: int | None
    request_id: int | None
    purpose: str
    body: str
    attachments: list[Any]
    created_at: datetime


class ClientPollResponse(BaseModel):
    messages: list[ClientMessage]
    poll_interval_seconds: int = Field(gt=0)
    availability_timeout_seconds: int = Field(gt=0)


class ClientDelivery(ClientMessage):
    status: Literal["pending", "sending", "sent", "failed", "uncertain"]
    fallback_channel: str | None
    error: str | None
    sent_at: datetime | None


class ClientHistoryResponse(BaseModel):
    messages: list[ClientDelivery]
    next_before_id: int | None


class ClientAcknowledgmentResponse(BaseModel):
    acknowledged: list[int]


class ClientAcknowledgmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_ids: list[Annotated[int, Field(gt=0)]] = Field(min_length=1, max_length=100)


class DomainMutationOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["domain.mutate"]
    action: WriteAction
    arguments: dict[str, Any] = Field(default_factory=dict, max_length=100)
    workstream_id: int | None = Field(default=None, gt=0)
    task_id: int | None = Field(default=None, gt=0)
    client_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    confirmed: Literal[True]


class DomainReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: Literal["domain.read"] = "domain.read"
    action: ReadAction
    arguments: dict[str, Any] = Field(default_factory=dict, max_length=100)
    workstream_id: int | None = Field(default=None, gt=0)
    task_id: int | None = Field(default=None, gt=0)
    client_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


class TaskMessagesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: Literal["tasks.messages"]
    task_id: int = Field(gt=0)
    page: int = Field(default=0, ge=0, le=1_000_000)


class TaskActivityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: Literal["tasks.activity"]
    task_id: int = Field(gt=0)
    since: str | None = Field(default=None, max_length=40)


class TaskSnapshotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: Literal["tasks.snapshot"]
    task_id: int = Field(gt=0)


class EventSnapshotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: Literal["events.snapshot"]


class ConversationsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: Literal["conversations.list"]
    page: int = Field(default=0, ge=0, le=1_000_000)
    query: str = Field(default="", max_length=200)
    channel: Literal["", "chat", "client", "telegram", "email", "reminder"] = ""


class TaskSummaryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: Literal["tasks.summary"]


class TaskPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: Literal["tasks.deletion-preview"]
    task_id: int = Field(gt=0)


class DatabasePasswordGenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: Literal["credentials.generate-database-password"]


class ApprovalsListRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: Literal["approvals.list"]
    limit: int = Field(default=50, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=1_000_000)


class PermissionControlOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["permission.control"]
    action: Literal["block-host", "unblock-host", "revoke-grant", "remove-legacy"]
    target: str = Field(min_length=1, max_length=253)
    confirmed: Literal[True]


class PermissionReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: Literal["permissions.read"]
    view: Literal["hosts", "history"]


class RecordListRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: Literal["records.list"]
    collection: Literal["tasks", "memories", "reminders", "projects", "scripts", "research"]
    page: int = Field(default=0, ge=0, le=1_000_000)
    query: str = Field(default="", max_length=500)


class RecordReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: Literal["records.read"]
    collection: Literal["tasks", "memories", "reminders", "projects", "scripts", "research"]
    record_id: int = Field(gt=0)


class RecordChildrenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: Literal["records.children"]
    collection: Literal["tasks", "projects", "research"]
    record_id: int = Field(gt=0)
    page: int = Field(default=0, ge=0, le=1_000_000)


class ProjectPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: Literal["projects.deletion-preview"]
    project_id: int = Field(gt=0)


class PendingDeletionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: Literal["projects.pending-deletions"]


class ProjectFilesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: Literal["projects.files"]
    project_id: int = Field(gt=0)
    page: int = Field(default=0, ge=0, le=1_000_000)


class ProjectFileContentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: Literal["projects.file-content"]
    project_id: int = Field(gt=0)
    file_id: int = Field(gt=0)
    offset: int = Field(default=0, ge=0, le=2**63 - 1)
    limit: int = Field(default=1_048_576, ge=1, le=1_048_576)


class ConfigurationCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: Literal["configuration.check"]
    runtime: bool = False


ManagementOperationRequest = Annotated[
    Union[
        ConfigurationSaveRequest, ServiceLifecycleOperation, CredentialReplaceOperation,
        CredentialApplyOperation, DatabaseResetOperation, DatabaseRecoveryOperation,
        CredentialStorageOperation, SearchMaintenanceOperation, RecordMutationOperation,
        ProjectDeleteOperation, ProjectResumeOperation, FileUploadOperation, FileAttachOperation,
        TaskStartOperation, TaskReplyOperation, TaskDeleteOperation,
        PermissionControlOperation,
        ResearchStartOperation, ApprovalDecisionOperation,
        ClientRegistrationOperation, ClientUnregisterOperation, ClientDeliveryResolveOperation,
        DomainMutationOperation,
    ],
    Field(discriminator="operation"),
]

ManagementReadRequest = Annotated[
    Union[
        RecordListRequest, RecordReadRequest, RecordChildrenRequest,
        ProjectPreviewRequest, PendingDeletionsRequest, ProjectFilesRequest,
        ProjectFileContentRequest, ConfigurationCheckRequest,
        TaskMessagesRequest, TaskActivityRequest, TaskSnapshotRequest, EventSnapshotRequest,
        ConversationsRequest, TaskSummaryRequest, TaskPreviewRequest, DatabasePasswordGenerationRequest, ApprovalsListRequest,
        PermissionReadRequest,
        ClientReadRequest,
        DomainReadRequest,
    ],
    Field(discriminator="request"),
]


class ManagementOperationResponse(BaseModel):
    id: str
    action: str
    state: Literal["queued", "running", "succeeded", "failed", "interrupted"] = Field(
        description="Queued/running are nonterminal. A scheduled executor restart stays running, "
        "with explicitly unverified progress and no result, until healthy recovery or its deadline."
    )
    progress: str
    created_at: str
    updated_at: str
    error: str | None = None
    result: dict[str, Any] | None = None
    verification_deadline: str | None = Field(
        default=None, description="Deadline for private-executor confirmation of a control-plane lifecycle."
    )
