"""Dashboard response shapes; request schemas remain with their HTTP handlers."""

from typing import Literal, NotRequired

from pydantic import ConfigDict
from typing_extensions import TypedDict

ResourceType = Literal["api", "token_plan"]


class Shape(TypedDict):
    __pydantic_config__ = ConfigDict(extra="forbid", strict=True)


class DashboardError(Shape):
    error: str
    code: NotRequired[str]


class SessionRequest(Shape):
    # The existing login handler ignores additional properties.
    __pydantic_config__ = ConfigDict(extra="allow")
    token: str


class SessionResult(Shape):
    authenticated: Literal[True]
    expires_in: int
    session_token: str


class Capabilities(Shape):
    connection_management: bool
    model_approval: bool
    model_discovery: bool
    application_management: bool


class Provider(Shape):
    id: str
    name: str
    base_url: str | None
    resource_type: ResourceType
    can_create: bool
    credential_type: Literal["api_key"]


class PolicyView(Shape):
    allow_token_plans: bool
    allowed_connections: list[str] | None
    allowed_models: list[str] | None
    requests_per_minute: int | None
    max_concurrent_requests: int | None
    max_output_tokens: int | None
    max_estimated_input_tokens: int | None
    daily_token_limit: int | None
    daily_budget: str | None
    budget_currency: str


class PlanView(Shape):
    tier: str | None
    use: Literal["personal_interactive_coding", "personal_interactive"]
    reasoning_effort: Literal["low", "high", "max"]
    extra_usage_policy: Literal["forbid"]


class DiscoverySummary(Shape):
    state: Literal["never", "success", "failure", "stale"]
    checked_at: str | None
    discovered_at: str | None
    current: bool
    count: int


class Discovery(DiscoverySummary):
    models: list[str]
    error_code: str | None


class ManagedConnection(Shape):
    id: str
    name: str
    provider: str
    resource_type: ResourceType
    base_url: str
    enabled: bool
    api_key_configured: bool
    timeout_seconds: float
    stream_usage: bool
    token_parameter: Literal["max_tokens", "max_completion_tokens"]
    workspace_id: str | None
    anthropic_version: Literal["2023-06-01", "2026-10-21"]
    plan: PlanView | None
    allowed_models: list[str]
    default_model: str | None
    revision: str
    approval_revision: int
    editable_fields: list[str]
    discovery: Discovery


class ModelVerification(Shape):
    state: str
    outcome: str
    verified_at: str | None
    request_id: str
    actual_model: str | None
    protocol: str | None
    capability: str | None
    scope: str
    current_configuration: bool
    account_access_observed: str
    live_availability: Literal["unknown"]


class CatalogModel(Shape):
    id: str
    discovery: Literal["listed", "not_in_catalog", "unknown"]
    approved: bool
    default: bool
    authorized_applications: list[str]
    effective_applications: list[str]
    verified_capabilities: list[str]
    live_availability: Literal["unknown"]
    verification: list[ModelVerification]


class ModelCounts(Shape):
    discovered: int
    approved: int
    application_authorized: int
    historically_verified: int


class ModelCatalog(Shape):
    counts: ModelCounts
    protocol: str | None
    approval_revision: int
    models: list[CatalogModel]


class ManagedModels(Shape):
    connection_id: str
    connection_enabled: bool
    approval_revision: int
    default_model: str | None
    protocol: Literal["chat-completions", "anthropic-messages", "responses"]
    discovery: Discovery
    models: list[CatalogModel]
    counts: ModelCounts
    live_availability: Literal["unknown"]


class ManagedApplication(Shape):
    id: str
    name: str
    created_at: str
    revoked_at: str | None
    active: bool
    policy: PolicyView
    revision: str


class CreatedApplication(ManagedApplication):
    token: str


class RevokedApplication(ManagedApplication):
    in_flight_policy: Literal["already_admitted_requests_continue"]


class Quota(Shape):
    day: str
    timezone: Literal["UTC"]
    policy: PolicyView
    requests: int
    in_flight_requests: int
    uncertain_requests: int
    remaining_tokens: int | None
    token_quota_state: Literal["unlimited", "limited", "exhausted", "blocked_unknown_usage"]
    historical_requests_excluded: int
    charged_or_reserved_tokens: int
    unknown_token_requests: int
    estimated_reserved_tokens: int
    estimated_cost_or_reservation: dict[str, str]
    unknown_cost_requests: int
    billed_cost: None
    scope: str


class ApplicationQuota(Quota):
    id: str


class EffectiveResource(Shape):
    id: str
    name: str
    resource_type: ResourceType
    models: list[str]


class Application(Shape):
    id: str
    name: str
    created_at: str
    revoked_at: str | None
    active: bool
    policy: PolicyView
    quota: Quota
    effective_resources: list[EffectiveResource]


class Authorization(Shape):
    kind: str
    state: NotRequired[str]
    credential_configured: NotRequired[bool]
    remote_validity: NotRequired[str]


class Verification(Shape):
    state: str
    previous_state: NotRequired[str]
    verified_at: NotRequired[str | None]
    age_seconds: NotRequired[float | None]
    model: NotRequired[str | None]
    scope: NotRequired[str]


class Availability(Shape):
    state: str
    checked_live: bool
    blockers: list[str]


class QuotaWindow(Shape):
    name: str
    used_ratio: float | None
    reset_at: str | None


class QuotaCounter(Shape):
    window: str
    unit: str
    limit: int | None
    used: int | None
    remaining: int | None
    reset_at: str | None


class ProviderQuota(Shape):
    state: str
    remaining_tokens: NotRequired[int | None]
    current_remaining_tokens: NotRequired[int | None]
    historical_snapshot: NotRequired[bool]
    checked_at: NotRequired[str | None]
    age_seconds: NotRequired[float | None]
    extra_usage: NotRequired[str]
    membership_status: NotRequired[str]
    conflicting_fields: NotRequired[bool]
    invalid_fields: NotRequired[bool]
    windows: list[QuotaWindow]
    reported_counters: list[QuotaCounter]


class Connection(Shape):
    id: str
    name: str
    provider: str
    resource_type: ResourceType
    enabled: bool
    models: list[str]
    default_model: str | None
    client_allowed_models: list[str]
    authorization: Authorization
    verification: Verification
    availability: Availability
    discovery: DiscoverySummary
    model_catalog: ModelCatalog
    provider_quota: ProviderQuota


class KnownTokens(Shape):
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None


class ResourceAccounting(Shape):
    scope: str
    requests: int
    tokens_by_basis: dict[str, int]


class UsageSummary(Shape):
    requests: int
    known_usage_requests: int
    missing_usage_requests: int
    local_rejected_requests: int
    unknown_consumption_requests: int
    in_flight_requests: int
    reported_usage_requests: int
    upstream_attempts: int
    unknown_attempt_count_requests: int
    unknown_time_requests: int
    status_counts: dict[str, int]
    known_tokens: KnownTokens
    missing_field_requests: dict[str, int]
    estimated_cost_by_currency: dict[str, str]
    unknown_cost_requests: int
    billed_cost: None
    resource_accounting: ResourceAccounting
    average_latency_ms: NotRequired[float | None]
    latency_samples: NotRequired[int]
    completed_requests: NotRequired[int]
    success_rate: NotRequired[float | None]


class EmptyTrendTokens(Shape):
    total_tokens: None


class EmptyTrendPoint(Shape):
    day: str
    requests: int
    known_tokens: KnownTokens | EmptyTrendTokens
    estimated_cost_by_currency: dict[str, str]
    unknown_consumption_requests: int
    success_rate: float | None
    average_latency_ms: float | None


class PopulatedTrendPoint(UsageSummary):
    day: str
    success_rate: float | None
    average_latency_ms: float | None


class ModelUsage(UsageSummary):
    connection_id: str | None
    model: str | None


class UsageFilters(Shape):
    since: str
    until: str
    caller_id: NotRequired[str]
    connection_id: NotRequired[str]
    model: NotRequired[str]


class Usage(Shape):
    timezone: str
    filters: UsageFilters
    summary: UsageSummary
    group_by: str
    trend: list[PopulatedTrendPoint | EmptyTrendPoint]
    models: list[ModelUsage]
    unknown_time_excluded: int


class Capacity(Shape, total=False):
    active: int
    queued: int
    admitted: int
    max_active: int
    max_queued: int
    accepting: bool


class StorageHealth(Shape, total=False):
    degraded: bool
    pending_records: int
    invalid_records: int
    accepting: bool


class Service(Shape):
    state: str
    reachable: bool | None
    version: NotRequired[str]
    version_matches: NotRequired[bool]
    pid: NotRequired[int]
    capacity: NotRequired[Capacity]
    storage: NotRequired[StorageHealth]


class SnapshotOptions(Shape):
    models: list[str]
    connections: list[str]


class SnapshotCapabilities(Shape):
    model_approval: bool


class Snapshot(Shape):
    checked_at: str
    version: str
    scope: str
    ok: bool
    issues: list[str]
    connections: list[Connection]
    applications: list[Application]
    service: Service
    usage: Usage | None
    options: SnapshotOptions
    limits: NotRequired[dict[str, int | float]]
    capabilities: SnapshotCapabilities


class RecordCost(Shape, total=False):
    estimated_cost: str | None
    currency: str | None
    cost_status: str
    token_basis: str


class RequestRecord(Shape):
    request_id: str
    started_at: str | None
    finished_at: str | None
    caller_id: str | None
    connection_id: str | None
    model: str | None
    resource_type: ResourceType | None
    purpose: str
    status: str
    http_status: int | None
    upstream_status: int | None
    duration_ms: float | None
    queue_ms: float | None
    upstream_attempts: int | None
    upstream_result: str | None
    delivery_status: str | None
    error_code: str | None
    upstream_error_code: str | None
    delivery_error_code: str | None
    usage_final: bool
    usage_known: bool
    usage_consistency: str
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    origin: str
    cost: RecordCost


class RecordsPage(Shape):
    items: list[RequestRecord]
    total: int
    offset: int
    limit: Literal[25]


class LogEvent(Shape):
    timestamp: str
    path: str
    status: NotRequired[float]
    duration_ms: NotRequired[float]
    queue_ms: NotRequired[float]
    upstream_attempts: NotRequired[float]


class Logs(Shape):
    items: list[LogEvent]
    omitted: int
    window: int


class BackupItem(Shape):
    name: str
    integrity: Literal["not_checked"]
    created_at: str | None
    manifest: Literal["unreadable", "sha256_present", "legacy"]


class Backups(Shape):
    items: list[BackupItem]
    total: int
    limit: int


class ModelApprovalResult(Shape):
    connection_id: str
    model: str
    enabled: bool
    default_model: str | None
