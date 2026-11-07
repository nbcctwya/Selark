"""Local configuration, ledger and gateway diagnostics; no model generation."""

import os
import sqlite3
from contextlib import closing
from pathlib import Path

import httpx

from selark import __version__
from selark.config import (
    ClientConfig,
    ConfigError,
    ServerConfig,
    data_directory,
    load_config,
    server_url,
)
from selark.connections import ConnectionsConfig
from selark.governance import ResourcesConfig
from selark.platforms import native
from selark.storage import SCHEMA_VERSION, token_digest


def doctor(directory: Path) -> dict:
    checks = []
    config = None
    try:
        config = load_config(directory / "server.json", ServerConfig)
        checks.append({"check": "server_config", "ok": True, "connection_enabled": config.enabled})
    except ConfigError as exc:
        checks.append({"check": "server_config", "ok": False, "message": str(exc)})
    try:
        path = directory / "connections.json"
        registry = load_config(path, ConnectionsConfig) if path.exists() else ConnectionsConfig()
        checks.append(
            {
                "check": "connections_config",
                "ok": True,
                "configured_connections": int(config is not None and config.api_key is not None)
                + len(registry.connections),
            }
        )
        for connection in registry.connections:
            if connection.provider == "chatgpt":
                from selark.chatgpt_auth import ChatGPTAuth

                status = ChatGPTAuth(directory).status(connection.connection_id)
                checks.append({"check": "chatgpt_authorization", "ok": status["ready"], **status})
    except ConfigError as exc:
        checks.append({"check": "connections_config", "ok": False, "message": str(exc)})
    try:
        path = directory / "resources.json"
        settings = (
            load_config(path, ResourcesConfig)
            if path.exists() or path.is_symlink()
            else ResourcesConfig()
        )
        checks.append(
            {
                "check": "resources_config",
                "ok": True,
                "aliases": len(settings.aliases),
                "prices": len(settings.prices),
            }
        )
    except ConfigError as exc:
        checks.append({"check": "resources_config", "ok": False, "message": str(exc)})
    try:
        client = load_config(directory / "client.json", ClientConfig)
        checks.append({"check": "client_config", "ok": True})
    except ConfigError as exc:
        client = None
        checks.append({"check": "client_config", "ok": False, "message": str(exc)})
    database = data_directory(directory) / "records.sqlite3"
    from selark.persistence import Journal

    pending = database.parent / "pending"
    if pending.exists():
        count = invalid = 0
        for path in pending.glob("*.json"):
            count += 1
            try:
                Journal.load(path)
            except (ValueError, OSError, TypeError, AttributeError):
                invalid += 1
        checks.append(
            {
                "check": "recovery_receipts",
                "ok": count == 0,
                "pending": count,
                "invalid": invalid,
                "message": "没有待恢复记录"
                if not count
                else "有待恢复落账；运行中自动重放，停服后可用 selark records recover。",
            }
        )
    if not database.exists():
        checks.append(
            {
                "check": "storage",
                "ok": False,
                "message": "记录库不存在，请运行 selark migrate；已迁移过的配置应恢复备份。",
            }
        )
    else:
        try:
            native.check_private(database)
            db = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2)
            try:
                integrity = db.execute("PRAGMA quick_check").fetchone()[0]
                count = db.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
                active = db.execute(
                    "SELECT COUNT(*) FROM requests WHERE status='running'"
                ).fetchone()[0]
                schema = db.execute("PRAGMA user_version").fetchone()[0]
                checks.append(
                    {
                        "check": "storage",
                        "ok": integrity == "ok"
                        and schema == SCHEMA_VERSION
                        and os.access(database, os.W_OK),
                        "schema_version": schema,
                        "message": "记录库正常"
                        if schema == SCHEMA_VERSION and integrity == "ok"
                        else "记录库需升级或修复；停服后运行 selark migrate，损坏时恢复备份。",
                        "integrity": integrity,
                        "records": count,
                        "running_records": active,
                        "path": str(database),
                    }
                )
                if client:
                    row = db.execute(
                        "SELECT id,revoked_at FROM tokens WHERE digest=? AND kind='caller'",
                        (token_digest(client.access_token.get_secret_value()),),
                    ).fetchone()
                    checks.append(
                        {
                            "check": "client_token",
                            "ok": bool(row and not row[1]),
                            "caller_id": row[0] if row else None,
                            "message": "本地客户端令牌有效"
                            if row and not row[1]
                            else "客户端令牌未注册或已撤销",
                        }
                    )
            finally:
                db.close()
            try:
                with closing(
                    sqlite3.connect(database.as_uri() + "?mode=rw", uri=True, timeout=0.05)
                ) as writer:
                    writer.execute("BEGIN IMMEDIATE")
                    writer.rollback()
                checks.append({"check": "storage_writer", "ok": True})
            except sqlite3.Error as exc:
                busy = getattr(exc, "sqlite_errorcode", None) in {
                    sqlite3.SQLITE_BUSY,
                    sqlite3.SQLITE_LOCKED,
                }
                checks.append(
                    {
                        "check": "storage_writer",
                        "ok": False,
                        "message": "记录库写锁繁忙，请检查长事务或正在执行的管理操作。"
                        if busy
                        else "记录库不能写入，请检查权限、可用空间和文件系统。",
                    }
                )
        except (sqlite3.Error, ConfigError, OSError):
            checks.append(
                {
                    "check": "storage",
                    "ok": False,
                    "message": "记录库损坏、权限不正确或不可访问，请检查路径与备份。",
                }
            )
    if config:
        try:
            with httpx.Client(trust_env=False, timeout=3) as http:
                response = http.get(server_url(config) + "/health")
            health = response.json()
            good = (
                response.status_code == 200
                and health.get("status") == "ok"
                and health.get("version") == __version__
                and not health.get("storage", {}).get("degraded", False)
            )
            checks.append(
                {
                    "check": "service",
                    "ok": good,
                    "version": health.get("version"),
                    "message": "服务正常"
                    if good
                    else "健康检查失败或服务版本过旧，请重启 selark serve",
                }
            )
        except (httpx.HTTPError, ValueError, AttributeError):
            checks.append(
                {
                    "check": "service",
                    "ok": False,
                    "message": "无法连接本机服务，请运行 selark serve。",
                }
            )
    return {
        "ok": all(check["ok"] for check in checks),
        "checks": checks,
        "model_generation_requests": 0,
        "note": "doctor 不执行真实连通性探测；使用 connection probe 显式生成。",
    }
