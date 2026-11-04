"""Public management projections and operations over the shared Runtime/Store."""

from selark.connections import PRESETS
from selark.discovery import discover_connection
from selark.errors import GatewayError
from selark.model_state import catalog, inventory
from selark.plans import PLAN_PRESETS
from selark.runtime import connection_revision
from selark.storage import application_revision

CONNECTION_FIELDS = (
    "name",
    "enabled",
    "base_url",
    "api_key",
    "timeout_seconds",
    "stream_usage",
    "token_parameter",
    "workspace_id",
    "anthropic_version",
    "plan",
)


def providers():
    return {
        "items": [
            {
                "id": identity,
                "name": name,
                "base_url": url,
                "resource_type": "token_plan"
                if identity in PLAN_PRESETS
                else ("chatgpt_subscription" if identity == "chatgpt" else "api"),
                "can_create": identity != "chatgpt",
                "credential_type": "oauth" if identity == "chatgpt" else "api_key",
            }
            for identity, (name, url) in PRESETS.items()
        ]
    }


def connection_config(runtime, identity):
    for config in runtime.connection_settings():
        if config.connection_id == identity:
            return config
    raise GatewayError(404, "connection_not_found", "未找到连接。")


def editable_fields(config):
    if config.connection_id == "mimo-default":
        return ["name", "enabled", "base_url", "api_key", "timeout_seconds"]
    if config.provider == "chatgpt":
        return ["name", "enabled", "timeout_seconds"]
    fields = list(CONNECTION_FIELDS)
    if config.resource_type != "token_plan":
        fields.remove("plan")
    return fields


def discovery_view(config):
    state = catalog(config)
    return {
        key: state.get(key)
        for key in (
            "state",
            "checked_at",
            "discovered_at",
            "current",
            "count",
            "models",
            "error_code",
        )
    }


def connection_view(config):
    # Explicit projection: no secret, authentication profile or credential fingerprint.
    result = {
        key: getattr(config, key)
        for key in (
            "name",
            "provider",
            "resource_type",
            "base_url",
            "enabled",
            "timeout_seconds",
            "stream_usage",
            "token_parameter",
            "workspace_id",
            "anthropic_version",
            "allowed_models",
            "default_model",
        )
    }
    return result | {
        "id": config.connection_id,
        "api_key_configured": config.api_key is not None,
        "plan": config.plan.model_dump() if config.plan is not None else None,
        "revision": connection_revision(config),
        "approval_revision": config.config_revision,
        "editable_fields": editable_fields(config),
        "discovery": discovery_view(config),
    }


def connections(runtime):
    return {"items": [connection_view(config) for config in runtime.connection_settings()]}


def connection(runtime, identity):
    return connection_view(connection_config(runtime, identity))


def add_connection(runtime, selection):
    options = selection.model_dump(exclude={"id", "provider", "api_key"}, exclude_none=True)
    runtime.add_connection(selection.id, selection.provider, selection.api_key, **options)
    return connection(runtime, selection.id)


def change_connection(runtime, identity, selection):
    config = connection_config(runtime, identity)
    changes = selection.model_dump(exclude={"revision"}, exclude_unset=True)
    if set(changes) - set(editable_fields(config)):
        raise GatewayError(400, "unsupported_field", "该连接不支持修改所提供的字段。")
    runtime.change_connection(changes, identity, expected_revision=selection.revision)
    return connection(runtime, identity)


def models(runtime, identity):
    config = connection_config(runtime, identity)
    return inventory(runtime.store, config) | {"discovery": discovery_view(config)}


def approve_models(runtime, identity, selection):
    connection_config(runtime, identity)
    runtime.change_models(
        identity,
        "enable" if selection.enabled else "disable",
        selection.models,
        all_models=selection.all_models,
        expected_revision=selection.revision,
        allow_default_clear=selection.clear_default,
    )
    return models(runtime, identity)


def default_model(runtime, identity, selection):
    connection_config(runtime, identity)
    runtime.change_models(
        identity,
        "default",
        default=selection.model,
        expected_revision=selection.revision,
    )
    return models(runtime, identity)


def discover_models(runtime, identity, selection):
    connection_config(runtime, identity)
    discover_connection(runtime, identity)
    return models(runtime, identity)


def applications(runtime):
    return {
        "items": [
            item | {"revision": application_revision(item)} for item in runtime.store.tokens()
        ]
    }


def application(runtime, identity):
    return runtime.store.application(identity)


def application_quota(runtime, identity):
    runtime.store.application(identity)
    return {"id": identity, **runtime.store.quota(identity)}


def create_application(runtime, selection):
    created = runtime.store.create_token(selection.name, policy=selection.policy.model_dump())
    return runtime.store.application(created["id"]) | {"token": created["token"]}


def change_policy(runtime, identity, selection):
    runtime.change_policy(
        identity,
        selection.policy.model_dump(exclude_unset=True),
        expected_revision=selection.revision,
    )
    return runtime.store.application(identity)


def revoke_application(runtime, identity, selection):
    runtime.store.revoke(identity, expected_revision=selection.revision)
    return runtime.store.application(identity) | {
        "in_flight_policy": "already_admitted_requests_continue",
    }
