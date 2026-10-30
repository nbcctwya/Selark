"""Read-only checks for the ledger structures required by each supported schema."""

from selark.config import ConfigError


def validate_schema(db):
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version not in {0, 1, 2, 3, 4}:
        raise ConfigError("记录库版本不受支持。")
    tables = dict(db.execute("SELECT name,type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
    # An empty version-zero database is the only legitimate pre-initialization shape.
    if version == 0 and not tables:
        return False
    columns = {
        "tokens": "id name digest created_at revoked_at kind expires_at consumed_at fingerprint",
        "requests": (
            "request_id original_request_id started_at finished_at caller_id connection_id "
            "session_id model requested_model purpose status http_status upstream_status "
            "duration_ms upstream_attempts usage_json usage_final prompt_tokens completion_tokens "
            "total_tokens error_code error_message origin"
        ),
        "verifications": "connection_id fingerprint verified_at state request_id error_code",
        "imports": "content_hash imported_at records_added",
    }
    keys = {
        "tokens": ["id"],
        "requests": ["request_id"],
        "verifications": ["connection_id"],
        "imports": ["content_hash"],
    }
    if version >= 2:
        columns["requests"] += (
            " upstream_result delivery_status upstream_error_code delivery_error_code "
            "queue_ms record_revision"
        )
        columns["verifications"] += " order_key"
        keys["verifications"] = ["connection_id", "fingerprint"]
    if version >= 3:
        columns["tokens"] += " policy_json"
        columns["requests"] += " requested_alias"
        columns["allocations"] = (
            "request_id caller_id day created_at state quota_tokens token_basis quota_cost "
            "currency cost_status estimated_cost document"
        )
        keys["allocations"] = ["request_id"]
    if version >= 4:
        columns["requests"] += " resource_type plan_id"
        columns["plan_status"] = "connection_id fingerprint checked_at document"
        keys["plan_status"] = ["connection_id", "fingerprint"]
    for name, required in columns.items():
        fields = db.execute(f"PRAGMA table_info({name})").fetchall()
        primary = [row[1] for row in sorted(fields, key=lambda row: row[5]) if row[5]]
        if (
            tables.get(name) != "table"
            or not set(required.split()).issubset(row[1] for row in fields)
            or primary != keys[name]
        ):
            raise ConfigError(
                f"记录库业务结构无效：{name} 缺少必需表、字段或主键；请恢复健康备份。"
            )
    # Authentication requires each digest to identify exactly one caller.
    unique_digest = False
    for index in db.execute("PRAGMA index_list(tokens)").fetchall():
        if index[2] and not index[4]:
            fields = db.execute("SELECT name FROM pragma_index_info(?)", (index[1],)).fetchall()
            unique_digest |= [row[0] for row in fields] == ["digest"]
    if not unique_digest:
        raise ConfigError("记录库业务结构无效：调用方令牌缺少唯一约束。")
    return True
