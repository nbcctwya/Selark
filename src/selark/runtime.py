"""Local administrative state, protected migration, and live connection settings."""

import hashlib
import json
import os
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from selark.config import (
    ConfigError,
    ServerConfig,
    ServiceLimits,
    _write_private,
    data_directory,
    load_config,
    private_directory,
    server_document,
)
from selark.connections import (
    PRESETS,
    ConnectionConfig,
    ConnectionsConfig,
    connection_document,
    enforce_approval,
    legacy_connection,
    registry_document,
    require_connection,
    valid_model,
)
from selark.governance import Price, ResourcesConfig, Target
from selark.locking import file_lock
from selark.plans import PLAN_PRESETS, PlanSettings, empty_quota
from selark.storage import SCHEMA_VERSION, Store, utc_now


def fingerprint(config: ServerConfig) -> str:
    if isinstance(config, ConnectionConfig):
        data = connection_document(config)
        for field in ("name", "enabled", "discovery", "limits"):
            data.pop(field, None)
        if config.provider == "chatgpt":
            data["oauth_generation"] = config.oauth_generation
        if config.resource_type == "api":
            data.pop("resource_type", None)
            data.pop("plan", None)
        if config.provider != "anthropic" or config.anthropic_version == "2026-10-21":
            data.pop("anthropic_version", None)  # Existing 0.4 fingerprints remain valid.
        if config.connection_id == "mimo-default":
            # Preserve pre-0.4 verification fingerprints exactly.
            for field in (
                "connection_id",
                "provider",
                "stream_usage",
                "token_parameter",
                "workspace_id",
            ):
                data.pop(field, None)
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    data = server_document(config)
    for field in (
        "enabled",
        "host",
        "port",
        "schema_version",
        "connection_id",
        "limits",
        "logging",
    ):
        data.pop(field, None)
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def discovery_fingerprint(config):
    document = connection_document(config)
    data = {
        key: document[key]
        for key in ("connection_id", "provider", "base_url", "api_key", "workspace_id")
    }
    if config.provider == "chatgpt":
        data["oauth_generation"] = config.oauth_generation
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def capability_fingerprint(config):
    """Protocol evidence is independent of local approval and application policies."""
    data = connection_document(config)
    for key in (
        "name",
        "enabled",
        "discovery",
        "allowed_models",
        "default_model",
        "config_revision",
    ):
        data.pop(key, None)
    if config.provider == "chatgpt":
        data["oauth_generation"] = config.oauth_generation
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


class Runtime:
    def __init__(self, directory: Path, *, readonly=False, data_path=None):
        self.directory = directory
        self.data_dir = data_path if data_path is not None else data_directory(directory)
        self.config_path = directory / "server.json"
        config = self.settings()
        if readonly:
            self.migration_backup = None
            self.store = Store(self.data_dir / "records.sqlite3", readonly=True)
            return
        private_directory(self.data_dir)
        self.migration_backup = None
        self.store = None
        with file_lock(self.data_dir / "management.lock"):
            config = self.settings()
            database = self.data_dir / "records.sqlite3"
            if not database.exists() and config.access_token is None:
                raise ConfigError(
                    "已迁移配置的记录库缺失；请恢复匹配的受保护备份，避免复活已撤销令牌。"
                )
            version = 0
            if database.exists():
                with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as old:
                    version = old.execute("PRAGMA user_version").fetchone()[0]
            if (database.exists() and version < SCHEMA_VERSION) or config.access_token is not None:
                with file_lock(self.data_dir / "service.lock", nonblocking=True):
                    self.migration_backup = self.backup("before-migration")
                    self.store = Store(database)
                    try:
                        if config.access_token is not None:
                            self.store.seed_legacy(config.access_token.get_secret_value())
                            # A fresh legacy installation has no pre-migration database.
                            # Existing databases already have a protected pre-upgrade snapshot.
                            target = self.migration_backup / "records.sqlite3"
                            if not target.exists():
                                self.store.backup(target)
                                from selark.backups import seal_backup

                                seal_backup(self.migration_backup)
                            _write_private(self.config_path, server_document(config))
                    except Exception:
                        self.store.close()
                        raise
            else:
                self.store = Store(database)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.store.close()

    def settings(self) -> ServerConfig:
        return load_config(self.config_path, ServerConfig)

    def registry(self):
        path = self.directory / "connections.json"
        return load_config(path, ConnectionsConfig) if path.exists() else ConnectionsConfig()

    def connection_settings(self):
        server, registry = self.settings(), self.registry()
        from selark.chatgpt_auth import ChatGPTAuth

        auth = ChatGPTAuth(self.directory)
        connections = [legacy_connection(server, registry)]
        for config in registry.connections:
            generation, unreadable = 0, False
            if config.provider == "chatgpt":
                try:
                    generation = auth.read(config.connection_id).generation
                except (ConfigError, OSError):
                    unreadable = True
            connections.append(
                config.model_copy(
                    update={
                        "limits": server.limits,
                        "oauth_generation": generation,
                        "authorization_unreadable": unreadable,
                    }
                )
            )
        return connections

    def resources(self):
        path = self.directory / "resources.json"
        return (
            load_config(path, ResourcesConfig)
            if path.exists() or path.is_symlink()
            else ResourcesConfig()
        )

    def edit_resource(self, kind, identity, document=None):
        with file_lock(self.data_dir / "management.lock"):
            current = self.resources().model_dump(mode="json")
            try:
                if kind == "alias":
                    if document is None:
                        if identity not in current["aliases"]:
                            raise ConfigError("未找到别名。")
                        del current["aliases"][identity]
                    else:
                        target = Target.model_validate(document)
                        connection = require_connection(
                            self.connection_settings(), target.connection_id
                        )
                        if target.model not in connection.allowed_models:
                            raise ConfigError("别名目标不在该连接允许模型列表中。")
                        current["aliases"][identity] = target.model_dump()
                elif kind == "price":
                    connection_id, model = identity
                    current["prices"] = [
                        p for p in current["prices"] if (p["connection_id"], p["model"]) != identity
                    ]
                    if document is not None:
                        price = Price.model_validate(
                            document | {"connection_id": connection_id, "model": model}
                        )
                        connection = require_connection(self.connection_settings(), connection_id)
                        if connection.resource_type in {"token_plan", "chatgpt_subscription"}:
                            raise ConfigError(
                                "Token Plan 不使用按量单价；套餐剩余额度与 Token 用量分别记录。"
                            )
                        if model not in connection.allowed_models:
                            raise ConfigError("价格目标不在该连接允许模型列表中。")
                        current["prices"].append(price.model_dump())
                else:
                    raise ConfigError("未知资源设置。")
                updated = ResourcesConfig.model_validate(current)
            except ValidationError:
                raise ConfigError("资源配置无效，请检查别名、模型、币种与十进制价格。") from None
            self.backup("before-resource-change")
            _write_private(self.directory / "resources.json", updated.model_dump(mode="json"))
        return updated.model_dump(mode="json")

    def change_policy(self, caller, patch):
        with file_lock(self.data_dir / "management.lock"):
            self.backup("before-caller-policy-change")
            return self.store.set_policy(caller, patch)

    def add_connection(self, identity, provider, key, **options):
        if provider not in PRESETS:
            raise ConfigError("不支持的供应商类型，请查看 connection providers。")
        name, url = PRESETS[provider]
        if provider == "chatgpt":
            options = {"resource_type": "chatgpt_subscription", **options}
        if provider in PLAN_PRESETS:
            options = {
                "resource_type": "token_plan",
                "plan": PlanSettings().model_dump(),
                "allowed_models": [],
                **{k: v for k, v in options.items() if v is not None},
            }
        if options.get("allowed_models") and options.get("default_model") is None:
            options["default_model"] = options["allowed_models"][0]
        try:
            config = ConnectionConfig.model_validate(
                {
                    "connection_id": identity,
                    "provider": provider,
                    "api_key": key,
                    "name": name,
                    "base_url": url,
                    **{k: v for k, v in options.items() if v is not None},
                }
            )
        except ValidationError:
            raise ConfigError("连接参数无效；请检查 ID、名称、地址、Key 和模型列表。") from None
        with file_lock(self.data_dir / "management.lock"):
            registry = self.registry()
            try:
                updated = ConnectionsConfig.model_validate(
                    registry.model_dump() | {"connections": [*registry.connections, config]}
                )
            except ValidationError:
                raise ConfigError("连接 ID 已存在、使用了保留 ID 或超过 32 个连接上限。") from None
            self.backup("before-connection-add")
            _write_private(self.directory / "connections.json", registry_document(updated))
        return self.connection(identity)

    def backup(self, reason: str) -> Path:
        # Receipts cannot be replaced/deleted between the SQLite snapshot and file copy.
        # Database writes may continue: copied receipts plus revision checks close that gap.
        with file_lock(self.data_dir / "recovery.lock"):
            return self._backup(reason)

    def _backup(self, reason: str) -> Path:
        root = self.data_dir / "backups"
        private_directory(root)
        target = root / (utc_now().replace(":", "-") + "-" + uuid4().hex[:8])
        private_directory(target)
        for name in (
            "server.json",
            "client.json",
            "connections.json",
            "resources.json",
            "storage.json",
        ):
            source = self.directory / name
            if source.is_symlink():
                raise ConfigError("待备份的配置不能是符号链接。")
            if source.exists():
                fd = os.open(target / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(source.read_bytes())
                    handle.flush()
                    os.fsync(handle.fileno())
        if self.store is not None:
            self.store.backup(target / "records.sqlite3")
        elif (self.data_dir / "records.sqlite3").exists():
            database = self.data_dir / "records.sqlite3"
            destination = target / "records.sqlite3"
            destination.touch(mode=0o600, exist_ok=False)
            with (
                closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as source,
                closing(sqlite3.connect(destination)) as dest,
            ):
                source.backup(dest)
        pending = self.data_dir / "pending"
        if pending.exists():
            private_directory(target / "pending")
            for source in pending.glob("*.json"):
                if source.is_symlink():
                    raise ConfigError("落账文件不能为符号链接。")
                content = source.read_bytes()
                fd = os.open(
                    target / "pending" / source.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
                with os.fdopen(fd, "wb") as output:
                    output.write(content)
                    output.flush()
                    os.fsync(output.fileno())
        from selark.chatgpt_auth import ChatGPTAuth

        ChatGPTAuth(self.directory).backup_registrations(target / "chatgpt-registration")
        _write_private(
            target / "manifest.json",
            {
                "reason": reason,
                "created_at": utc_now(),
                "recovery_consistent": True,
                "chatgpt_credentials": "registration_only_reauthorize_after_restore",
            },
        )
        from selark.backups import seal_backup

        seal_backup(target)
        from selark.persistence import sync_directory

        if (target / "pending").exists():
            sync_directory(target / "pending")
        sync_directory(target)
        sync_directory(root)
        return target

    @contextmanager
    def service(self):
        with file_lock(self.data_dir / "service.lock", nonblocking=True):
            report = self.recover_records(locked=True)
            if report["failed"] or report["invalid"]:
                raise ConfigError("待恢复落账文件无法处理，请运行 selark doctor；未重发模型请求。")
            yield

    def recover_records(self, *, locked=False):
        from selark.persistence import Journal

        if not locked:
            with file_lock(self.data_dir / "service.lock", nonblocking=True):
                return self.recover_records(locked=True)
        report = Journal(self.data_dir / "pending").replay(self.store, include_unsettled=True)
        report["interrupted"] = (
            0 if report["failed"] or report["invalid"] else self.store.recover_interrupted()
        )
        return report

    def change_limits(self, changes):
        if not changes or set(changes) - ServiceLimits.model_fields.keys():
            raise ConfigError("请提供有效的运行限制参数。")
        with file_lock(self.data_dir / "management.lock"):
            config = self.settings()
            try:
                limits = ServiceLimits.model_validate(config.limits.model_dump() | changes)
            except ValidationError:
                raise ConfigError("运行限制无效，请检查并发数、队列大小和超时范围。") from None
            if limits != config.limits:
                self.backup("before-limits-change")
                config.limits = limits
                _write_private(self.config_path, server_document(config))
        return {"limits": limits.model_dump(), "restart_required": True}

    def connection(self, identity="mimo-default", *, config=None) -> dict:
        config = (
            config
            if config is not None
            else require_connection(self.connection_settings(), identity)
        )
        result = config.model_dump(mode="json", exclude={"api_key"})
        result.update(configured=True, api_key="[REDACTED]")
        if config.provider == "chatgpt":
            from selark.chatgpt_auth import ChatGPTAuth

            result.pop("api_key")
            result["authentication"] = "oauth"
            result["authorization"] = (
                {"state": "unreadable", "ready": False}
                if config.authorization_unreadable
                else ChatGPTAuth(self.directory).status(identity)
            )
        result["verification"] = (
            {"state": "unknown", "verified_at": None}
            if config.authorization_unreadable
            else self.store.verification(config.connection_id, fingerprint(config))
        )
        from selark.model_state import catalog

        result["discovery"] = catalog(config)
        result["models_configured"] = bool(config.allowed_models)
        result["protocol"] = (
            "responses"
            if config.provider == "chatgpt"
            else "anthropic-messages"
            if config.provider == "anthropic"
            else "chat-completions"
        )
        if config.resource_type == "token_plan":
            result["plan_status"] = self.store.plan_status(
                config.connection_id, fingerprint(config)
            )
            result["generation_support"] = PLAN_PRESETS[config.provider]["generation_support"]
        return result

    def plan_status(self, identity):
        config = require_connection(self.connection_settings(), identity)
        if config.resource_type != "token_plan":
            raise ConfigError("此连接不是 Token Plan。")
        return self.store.plan_status(identity, fingerprint(config))

    def save_plan_status(self, config, result, checked_at):
        self.store.save_plan_status(config.connection_id, fingerprint(config), checked_at, result)

    def plan_failure(self, config, error, checked_at):
        result = empty_quota("failure") | {
            "error_code": error.code,
            "message": "套餐状态读取失败；不使用旧快照声称当前额度已知。",
        }
        self.save_plan_status(config, result, checked_at)

    def change_connection(self, changes: dict, identity="mimo-default") -> dict:
        if identity != "mimo-default":
            return self._change_extra(changes, identity)
        with file_lock(self.data_dir / "management.lock"):
            current = self.settings()
            registry = self.registry()
            document = server_document(current)
            allowed = {
                "base_url",
                "api_key",
                "allowed_models",
                "default_model",
                "timeout_seconds",
                "enabled",
                "name",
            }
            if not changes or set(changes) - allowed:
                raise ConfigError("未提供有效的连接修改参数。")
            try:
                update_fields = {k: v for k, v in changes.items() if k != "name"}
                updated = ServerConfig.model_validate(document | update_fields)
                renamed = ConnectionsConfig.model_validate(
                    registry.model_dump()
                    | {"legacy_name": changes.get("name", registry.legacy_name)}
                )
            except ValidationError:
                raise ConfigError("连接配置无效；请检查地址、凭据、允许模型和默认模型。") from None
            if server_document(updated) != document or renamed != registry:
                if any(
                    server_document(updated)[key] != document[key]
                    for key in update_fields
                    if key != "enabled"
                ):
                    updated.config_revision = current.config_revision + 1
                self.backup("before-connection-change")
                if server_document(updated) != document:
                    _write_private(self.config_path, server_document(updated))
                if renamed != registry:
                    _write_private(self.directory / "connections.json", registry_document(renamed))
        return self.connection()

    def _change_extra(self, changes, identity):
        allowed = {
            "name",
            "base_url",
            "api_key",
            "allowed_models",
            "default_model",
            "enabled",
            "timeout_seconds",
            "stream_usage",
            "token_parameter",
            "workspace_id",
            "anthropic_version",
            "plan",
        }
        if not changes or set(changes) - allowed:
            raise ConfigError("未提供有效的连接修改参数。")
        with file_lock(self.data_dir / "management.lock"):
            registry = self.registry()
            current = require_connection(registry.connections, identity)
            document = connection_document(current)
            try:
                updated = ConnectionConfig.model_validate(document | changes)
            except ValidationError:
                raise ConfigError("连接配置无效，请检查模型、默认模型、地址和凭据。") from None
            if connection_document(updated) != document:
                if any(
                    connection_document(updated)[k] != document[k]
                    for k in changes
                    if k not in {"name", "enabled"}
                ):
                    updated.config_revision += 1
                registry.connections = [
                    updated if c.connection_id == identity else c for c in registry.connections
                ]
                self.backup("before-connection-change")
                _write_private(self.directory / "connections.json", registry_document(registry))
        return self.connection(identity)

    def save_discovery(self, original, models, error=None):
        from selark.connections import Discovery

        with file_lock(self.data_dir / "management.lock"):
            current = require_connection(self.connection_settings(), original.connection_id)
            if discovery_fingerprint(current) != discovery_fingerprint(original):
                raise ConfigError("发现期间连接来源或授权已改变；结果未应用，请重新发现。")
            if current.discovery != original.discovery:
                raise ConfigError("发现目录已被另一查询更新；迟到结果未应用。")
            # Approval may change during a network query. Keep the current approval,
            # not the pre-query copy. A failed refresh retains the last catalog.
            previous = current.discovery
            catalog_at = previous.discovered_at or (
                previous.checked_at if previous.state == "success" else None
            )
            catalog_source = previous.catalog_fingerprint or (
                previous.source_fingerprint if previous.state == "success" else None
            )
            checked_at = utc_now()
            try:
                current.discovery = Discovery(
                    state="failure" if error else "success",
                    checked_at=checked_at,
                    source_fingerprint=discovery_fingerprint(current),
                    models=previous.models if error else models,
                    discovered_at=catalog_at if error else checked_at,
                    catalog_fingerprint=catalog_source if error else discovery_fingerprint(current),
                    error_code=error.code if error else None,
                    error_message=error.message if error else None,
                )
            except ValidationError:
                raise ConfigError("发现目录无效；原目录与启用列表均保留。") from None
            registry = self.registry()
            if current.connection_id == "mimo-default":
                registry.legacy_discovery = current.discovery
            else:
                registry.connections = [
                    current if c.connection_id == current.connection_id else c
                    for c in registry.connections
                ]
            self.backup("before-model-discovery")
            _write_private(self.directory / "connections.json", registry_document(registry))
        return self.connection(original.connection_id)

    def change_models(self, identity, action, models=(), *, all_models=False, default=None):
        """One atomic approval change, independent of catalog refresh and caller policies."""
        with file_lock(self.data_dir / "management.lock"):
            config = require_connection(self.connection_settings(), identity)
            selected = list(dict.fromkeys(models))
            if any(not valid_model(model) for model in selected):
                raise ConfigError("模型标识无效。")
            if action == "enable" and all_models:
                from selark.model_state import catalog

                state = catalog(config)
                if state["state"] != "success" or not state["current"]:
                    raise ConfigError(
                        "批量启用需要当前来源的成功发现目录，请先 connection discover。"
                    )
                selected = list(config.discovery.models)
            if action in {"enable", "disable"} and not selected and not all_models:
                raise ConfigError("请指定模型，或明确使用批量选项。")
            approved = list(config.allowed_models)
            chosen = config.default_model
            if action == "enable":
                approved = list(dict.fromkeys([*approved, *selected]))
            elif action == "disable":
                approved = [] if all_models else [m for m in approved if m not in selected]
                if chosen not in approved:
                    chosen = None
            elif action == "default":
                if default is not None and default not in approved:
                    raise ConfigError("默认模型必须已经启用；设置默认不会隐式启用模型。")
                chosen = default
            else:
                raise ConfigError("未知模型管理操作。")
            if approved == config.allowed_models and chosen == config.default_model:
                return self.connection(identity, config=config)
            changes = {"allowed_models": approved, "default_model": chosen}
            try:
                if identity == "mimo-default":
                    server = self.settings()
                    updated = ServerConfig.model_validate(server_document(server) | changes)
                    updated.config_revision += 1
                    path, document = self.config_path, server_document(updated)
                else:
                    updated = ConnectionConfig.model_validate(connection_document(config) | changes)
                    updated.config_revision += 1
                    registry = self.registry()
                    registry.connections = [
                        updated if c.connection_id == identity else c for c in registry.connections
                    ]
                    path = self.directory / "connections.json"
                    document = registry_document(registry)
            except ValidationError:
                raise ConfigError("启用列表无效或超出该连接支持范围；所有选择均未应用。") from None
            self.backup("before-model-approval")
            _write_private(path, document)
        return self.connection(identity)

    @contextmanager
    def model_admission(self, original, model):
        # This lock linearizes dispatch admission with administrative approval changes.
        # Requests already admitted retain their original provider snapshot.
        with file_lock(self.data_dir / "management.lock"):
            current = require_connection(self.connection_settings(), original.connection_id)
            enforce_approval(current, model)
            if capability_fingerprint(current) != capability_fingerprint(original):
                from selark.errors import GatewayError

                raise GatewayError(
                    409, "model_config_changed", "发送前连接配置或授权发生变化，未生成。"
                )
            yield

    def models(self, identity, *, caller=None):
        from selark.model_state import inventory

        config = require_connection(self.connection_settings(), identity)
        return inventory(self.store, config, caller=caller)
