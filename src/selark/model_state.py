"""Read-only projections of discovery, approval, application access and observed capability."""

from selark.config import ConfigError
from selark.governance import Policy, permitted
from selark.runtime import capability_fingerprint, discovery_fingerprint


def protocol(config):
    return {"chatgpt": "responses", "anthropic": "anthropic-messages"}.get(
        config.provider, "chat-completions"
    )


def catalog(config):
    discovery = config.discovery
    document = discovery.model_dump(exclude={"source_fingerprint", "catalog_fingerprint"})
    source = discovery.catalog_fingerprint or (
        discovery.source_fingerprint if discovery.state == "success" else None
    )
    document["discovered_at"] = discovery.discovered_at or (
        discovery.checked_at if discovery.state == "success" else None
    )
    document["current"] = bool(source and source == discovery_fingerprint(config))
    if discovery.source_fingerprint and discovery.source_fingerprint != discovery_fingerprint(
        config
    ):
        document["previous_state"], document["state"] = document["state"], "stale"
    document["count"] = len(discovery.models)
    return document


def observations(store, config):
    stamp = capability_fingerprint(config)
    with store.lock:
        rows = store.db.execute(
            "SELECT * FROM (SELECT requested_model,model,model_fingerprint,protocol,capability,"
            "verified_at,finished_at,upstream_result,upstream_status,request_id,started_at,"
            "ROW_NUMBER() OVER (PARTITION BY requested_model,capability ORDER BY "
            "(model_fingerprint=?) DESC,started_at DESC,request_id DESC) AS position "
            "FROM requests WHERE connection_id=? AND origin='live' AND upstream_attempts>0 "
            "AND upstream_result IN ('success','failed')) WHERE position=1",
            (stamp, config.connection_id),
        ).fetchall()
    result = {}
    for row in rows:
        current = row["model_fingerprint"] == stamp
        outcome = "success" if row["upstream_result"] == "success" else "failure"
        scope = "model_connection_configuration" if row["model_fingerprint"] else "legacy_unknown"
        observation = {
            "state": outcome if current else "stale" if row["model_fingerprint"] else "unscoped",
            "outcome": outcome,
            "verified_at": row["verified_at"] or row["finished_at"],
            "request_id": row["request_id"],
            "actual_model": row["model"],
            "protocol": row["protocol"],
            "capability": row["capability"],
            "scope": scope,
            "current_configuration": current,
            "account_access_observed": "allowed"
            if outcome == "success"
            else ("rejected" if row["upstream_status"] in (401, 403) else "unknown"),
            "live_availability": "unknown",
        }
        result.setdefault(row["requested_model"], []).append(observation)
    return result


def inventory(store, config, *, caller=None, callers=None):
    directory = catalog(config)
    callers = store.tokens() if callers is None else callers
    if caller is not None:
        callers = [item for item in callers if item["id"] == caller]
        if not callers:
            raise ConfigError("未找到应用 ID。")
    policies = [(c, Policy.model_validate(c["policy"])) for c in callers]
    history = observations(store, config)
    models = []
    for identity in dict.fromkeys([*config.discovery.models, *config.allowed_models]):
        approved = identity in config.allowed_models
        authorized = [
            c["id"]
            for c, p in policies
            if c["active"] and permitted(p, config.connection_id, identity, config.resource_type)
        ]
        evidence = history.get(identity, [])
        models.append(
            {
                "id": identity,
                "discovery": "listed"
                if identity in config.discovery.models
                else (
                    "not_in_catalog"
                    if directory["current"] and directory["state"] == "success"
                    else "unknown"
                ),
                "approved": approved,
                "default": identity == config.default_model,
                "authorized_applications": authorized,
                "effective_applications": authorized if config.enabled and approved else [],
                "verification": evidence,
                "verified_capabilities": [
                    e["capability"]
                    for e in evidence
                    if e["state"] == "success"
                    and e["protocol"] == protocol(config)
                    and e["capability"] in {"text_nonstream", "text_stream"}
                ],
                "live_availability": "unknown",
            }
        )
    return {
        "connection_id": config.connection_id,
        "connection_enabled": config.enabled,
        "default_model": config.default_model,
        "protocol": protocol(config),
        "discovery": directory,
        "models": models,
        "counts": {
            "discovered": len(config.discovery.models),
            "approved": len(config.allowed_models),
            "application_authorized": sum(bool(m["effective_applications"]) for m in models),
            "historically_verified": sum(bool(m["verified_capabilities"]) for m in models),
        },
        "live_availability": "unknown",
    }
