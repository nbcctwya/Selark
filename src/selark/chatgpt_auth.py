"""Official SIWC public-client OAuth; owns only Selark's registrations and tokens.

No Codex/browser credential import, private ChatGPT routes, or API-key fallback.
Rotating refreshes are serialized across processes and journal their uncertain state.
"""

import asyncio
import base64
import contextlib
import hashlib
import os
import re
import secrets
import time
import webbrowser
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

import httpx
import jwt
from pydantic import Field, SecretStr

from selark.config import ConfigError, StrictConfig, _write_private, load_config, private_directory
from selark.errors import GatewayError
from selark.locking import file_lock
from selark.protocol import strict_json

ISSUER = "https://auth.openai.com"
AUTHORIZE = ISSUER + "/api/accounts/authorize"
TOKEN = ISSUER + "/api/accounts/oauth/token"
RESOURCE = "https://api.openai.com/v1"
MANAGE_USAGE = "https://chatgpt.com/settings/usage"
DYNAMIC_CLIENT = "dynamic_agent_client"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
PLAN_SCOPES = {"resource.invoke", "chatgpt.tokens.use.direct"}
TERMINAL_REFRESH = {
    "invalid_grant",
    "invalid_refresh_token",
    "token_expired",
    "refresh_token_expired",
    "refresh_token_invalidated",
    "refresh_token_reused",
}


def auth_error(code, message, status=401, upstream=None):
    return GatewayError(status, code, message, upstream)


def label(value, maximum=512):
    return (
        isinstance(value, str)
        and 0 < len(value) <= maximum
        and all(ord(c) >= 32 and ord(c) != 127 for c in value)
    )


def client_id(value):
    return (
        isinstance(value, str)
        and value != DYNAMIC_CLIENT
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", value) is not None
    )


def secret(value):
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 32768
        or not value.isascii()
        or any(c.isspace() for c in value)
    ):
        raise auth_error("chatgpt_token_invalid", "官方返回的凭据格式无效。", 502)
    return SecretStr(value)


class Host(StrictConfig):
    schema_version: Literal[1] = 1
    host_id: str = Field(pattern=r"^urn:uuid:[0-9a-f-]{36}$")


class Profile(StrictConfig):
    schema_version: Literal[1] = 1
    connection_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    client_id: str | None = None
    issuer: str | None = None
    subject: str | None = None
    email: str | None = None
    generation: int = Field(default=0, ge=0)
    state: Literal[
        "new", "registered", "connected", "signed_out", "reauth_required", "signing_out"
    ] = "new"
    refresh_state: Literal["ready", "refreshing", "uncertain"] = "ready"
    access_token: SecretStr | None = None
    refresh_token: SecretStr | None = None
    id_token: SecretStr | None = None
    scopes: list[str] = Field(default_factory=list, max_length=100)
    expires_at: float | None = None
    earliest_refresh_at: float | None = None
    remote_revocation_confirmed: bool | None = None


def cleared(profile, state="signed_out"):
    return profile.model_copy(
        update={
            "state": state,
            "generation": profile.generation + 1,
            "refresh_state": "ready",
            "access_token": None,
            "refresh_token": None,
            "id_token": None,
            "scopes": [],
            "expires_at": None,
            "earliest_refresh_at": None,
        }
    )


class ChatGPTAuth:
    def __init__(self, directory: Path, *, transport=None):
        self.root = directory / "chatgpt-auth"
        self.transport = transport  # Only tests inject a transport, never endpoints or keys.

    def path(self, identity):
        if not isinstance(identity, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", identity):
            raise ConfigError("ChatGPT 连接 ID 无效。")
        return self.root / "profiles" / (identity + ".json")

    def read(self, identity):
        path = self.path(identity)
        if self.root.is_symlink() or path.parent.is_symlink():
            raise ConfigError("ChatGPT 私有目录不能是符号链接。")
        if not path.exists() and not path.is_symlink():
            return Profile(connection_id=identity)
        if path.stat().st_size > 131072 or path.stat().st_uid != os.getuid():
            raise ConfigError("ChatGPT 凭据文件大小或所有者无效。")
        profile = load_config(path, Profile)
        if profile.connection_id != identity:
            raise ConfigError("ChatGPT 凭据与连接 ID 不匹配。")
        return profile

    def write(self, profile):
        private_directory(self.root)
        private_directory(self.root / "profiles")
        data = profile.model_dump(mode="json")
        for name in ("access_token", "refresh_token", "id_token"):
            value = getattr(profile, name)
            data[name] = value.get_secret_value() if value else None
        _write_private(self.path(profile.connection_id), data)
        # Short, atomic credential commits have no await: a cancelled refresh cannot
        # release its file lock and later overwrite a concurrently signed-out account.
        from selark.persistence import sync_directory

        sync_directory(self.root / "profiles")

    @asynccontextmanager
    async def locked(self, identity, *, timeout=20, kind="session"):
        private_directory(self.root)
        self.path(identity)  # Validate before constructing a lock filename.
        deadline = time.monotonic() + timeout
        while True:
            lock = file_lock(self.root / (kind + "." + identity + ".lock"), nonblocking=True)
            try:
                lock.__enter__()
                break
            except ConfigError:
                if time.monotonic() >= deadline:
                    raise auth_error(
                        "chatgpt_auth_busy",
                        "此 ChatGPT 连接正在登录、刷新或退出，请稍后重试。",
                        503,
                    ) from None
                await asyncio.sleep(0.02)
        try:
            yield
        finally:
            lock.__exit__(None, None, None)

    async def host(self):
        async with self.locked("host", kind="installation"):
            path = self.root / "host.json"
            if path.exists() or path.is_symlink():
                return load_config(path, Host).host_id
            host = Host(host_id="urn:uuid:" + str(uuid4()))
            _write_private(path, host.model_dump())
            return host.host_id

    def status(self, identity):
        profile = self.read(identity)
        expired = profile.expires_at is None or profile.expires_at <= time.time()
        return {
            "connection_id": identity,
            "resource_type": "chatgpt_subscription",
            "state": profile.state,
            "client_registered": bool(profile.client_id),
            "client_id": profile.client_id,
            "account_email": profile.email,
            "authorization_revision": profile.generation,
            "access_expired": expired,
            "expires_at": datetime.fromtimestamp(profile.expires_at, UTC).isoformat()
            if profile.expires_at and 0 < profile.expires_at < 253402300799
            else None,
            "refresh_available": bool(profile.refresh_token),
            "refresh_state": profile.refresh_state,
            "plan_permission": PLAN_SCOPES.issubset(profile.scopes),
            "ready": profile.state == "connected"
            and profile.refresh_state == "ready"
            and bool(profile.access_token)
            and PLAN_SCOPES.issubset(profile.scopes)
            and (not expired or bool(profile.refresh_token)),
            "remote_revocation_confirmed": profile.remote_revocation_confirmed,
            "remaining_tokens": None,
            "reset_at": None,
            "manage_usage_url": MANAGE_USAGE,
        }

    def backup_registrations(self, destination):
        """Keep account bindings, never snapshot a rotating refresh token for replay."""
        if not self.root.exists():
            return
        if self.root.is_symlink() or (self.root / "profiles").is_symlink():
            raise ConfigError("ChatGPT 私有目录不能是符号链接。")
        private_directory(destination)
        host = self.root / "host.json"
        if host.exists() or host.is_symlink():
            _write_private(destination / "host.json", load_config(host, Host).model_dump())
        private_directory(destination / "profiles")
        for path in (self.root / "profiles").glob("*.json"):
            profile = cleared(self.read(path.stem))
            _write_private(destination / "profiles" / path.name, profile.model_dump(mode="json"))

    @asynccontextmanager
    async def http(self):
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(15, connect=5, pool=5, write=5),
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=2),
            follow_redirects=False,
            transport=self.transport,
        ) as client:
            yield client

    async def document(self, client, method, url, *, data=None, empty=False):
        """Read only trusted fixed-origin endpoints; bound bodies and never echo errors."""
        try:
            async with asyncio.timeout(20):
                async with client.stream(
                    method,
                    url,
                    data=data,
                    timeout=httpx.Timeout(15, connect=5, pool=5, write=5),
                    follow_redirects=False,
                ) as response:
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 262144:
                            raise ValueError
                    if empty and response.status_code == 200:
                        return 200, {}
                    value = strict_json(body)
                    if not isinstance(value, dict):
                        raise ValueError
                    return response.status_code, value
        except (httpx.TimeoutException, TimeoutError):
            raise auth_error(
                "chatgpt_auth_timeout", "OpenAI 授权服务超时；未发送模型请求。", 504
            ) from None
        except httpx.RequestError:
            raise auth_error(
                "chatgpt_auth_unavailable", "无法访问 OpenAI 官方授权服务。", 502
            ) from None
        except (ValueError, UnicodeError):
            raise auth_error(
                "chatgpt_auth_protocol", "OpenAI 授权服务返回了无效响应。", 502
            ) from None

    async def metadata(self, client):
        status, data = await self.document(
            client, "GET", ISSUER + "/.well-known/openid-configuration"
        )
        if (
            status != 200
            or data.get("issuer") != ISSUER
            or data.get("authorization_endpoint") != AUTHORIZE
            or data.get("token_endpoint") != TOKEN
            or not isinstance(data.get("id_token_signing_alg_values_supported"), list)
            or "RS256" not in data["id_token_signing_alg_values_supported"]
        ):
            raise auth_error(
                "chatgpt_oidc_invalid", "官方 OIDC 元数据与已支持的授权契约不匹配。", 502
            )
        for key in ("jwks_uri", "revocation_endpoint"):
            value = data.get(key)
            if not isinstance(value, str):
                raise auth_error("chatgpt_oidc_invalid", "官方授权端点格式无效。", 502)
            url = urlsplit(value)
            if (
                url.scheme != "https"
                or url.netloc != "auth.openai.com"
                or not url.path.startswith("/")
                or url.query
                or url.fragment
            ):
                raise auth_error("chatgpt_oidc_invalid", "官方授权端点来源校验失败。", 502)
        return data

    async def verify_identity(self, client, encoded, issued, *, nonce=None):
        metadata = await self.metadata(client)
        status, jwks = await self.document(client, "GET", metadata["jwks_uri"])
        try:
            header = jwt.get_unverified_header(encoded)
            if status != 200 or header.get("alg") != "RS256" or not label(header.get("kid"), 200):
                raise ValueError
            keys = jwks.get("keys")
            if not isinstance(keys, list) or len(keys) > 64:
                raise ValueError
            matches = [
                k
                for k in keys
                if isinstance(k, dict)
                and k.get("kid") == header["kid"]
                and k.get("kty") == "RSA"
                and k.get("use", "sig") == "sig"
                and k.get("alg", "RS256") == "RS256"
            ]
            if len(matches) != 1:
                raise ValueError
            key = jwt.PyJWK.from_dict(matches[0], algorithm="RS256").key
            claims = jwt.decode(
                encoded,
                key,
                algorithms=["RS256"],
                audience=issued,
                issuer=ISSUER,
                leeway=30,
                options={"require": ["iss", "sub", "aud", "exp", "iat"]},
            )
            if not label(claims.get("sub")):
                raise ValueError
            audience = claims.get("aud")
            if (
                (isinstance(audience, list) and len(audience) > 1) or "azp" in claims
            ) and claims.get("azp") != issued:
                raise ValueError
            if nonce is not None and (
                not isinstance(claims.get("nonce"), str)
                or not secrets.compare_digest(claims["nonce"].encode(), nonce.encode())
            ):
                raise ValueError
            return claims
        except (ValueError, TypeError, KeyError, jwt.PyJWTError):
            raise auth_error(
                "chatgpt_identity_invalid",
                "ID Token 的签名、签发方、受众、有效期或 nonce 校验失败。",
            ) from None

    async def exchange(self, client, data):
        status, response = await self.document(client, "POST", TOKEN, data=data)
        if status == 200:
            return response
        error = response.get("error")
        code = (
            error
            if isinstance(error, str)
            else error.get("code")
            if isinstance(error, dict)
            else None
        )
        if isinstance(code, str) and code in TERMINAL_REFRESH:
            raise auth_error(
                "chatgpt_" + code, "授权码或刷新凭据已失效，请重新登录该连接。", upstream=status
            )
        if code == "invalid_client":
            raise auth_error(
                "chatgpt_invalid_client",
                "该客户端注册无效，请核对连接的官方授权状态。",
                upstream=status,
            )
        raise auth_error(
            "chatgpt_token_exchange_failed", "官方令牌交换未成功；未切换到 API Key。", 502, status
        )

    async def accept(self, client, previous, data, *, nonce=None, refreshing=False):
        if not client_id(previous.client_id):
            raise auth_error("chatgpt_registration_missing", "缺少官方颁发的客户端 ID。")
        raw_id = data.get("id_token")
        if raw_id is None and refreshing:
            claims = {"iss": previous.issuer, "sub": previous.subject, "email": previous.email}
            identity_token = previous.id_token
        else:
            identity_token = secret(raw_id)
            claims = await self.verify_identity(
                client, identity_token.get_secret_value(), previous.client_id, nonce=nonce
            )
        if previous.subject is not None and (
            claims["iss"] != previous.issuer or claims["sub"] != previous.subject
        ):
            raise auth_error(
                "chatgpt_account_mismatch", "授权账户与该连接原有身份不同；请使用独立连接。"
            )
        expires = data.get("expires_in")
        scope = data.get("scope")
        if scope is None and refreshing:
            scope = " ".join(previous.scopes)
        if (
            str(data.get("token_type", "")).lower() != "bearer"
            or type(expires) is not int
            or not 1 <= expires <= 86400
            or not isinstance(scope, str)
            or len(scope) > 4096
            or len(scope.split()) > 100
            or data.get("client_id", previous.client_id) != previous.client_id
        ):
            raise auth_error("chatgpt_token_invalid", "官方令牌字段或权限格式不受支持。", 502)
        earliest = data.get("earliest_refresh_at")
        if isinstance(earliest, str):
            try:
                parsed = datetime.fromisoformat(earliest.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError
                earliest = parsed.timestamp()
            except ValueError:
                raise auth_error(
                    "chatgpt_token_invalid", "无法解释官方最早刷新时间。", 502
                ) from None
        if earliest is not None and (
            type(earliest) not in (int, float) or not 0 <= earliest < 253402300799
        ):
            raise auth_error("chatgpt_token_invalid", "官方最早刷新时间格式无效。", 502)
        refresh = data.get("refresh_token")
        if refreshing and refresh is None:
            raise auth_error(
                "chatgpt_token_invalid", "刷新响应未提供替换刷新凭据；请重新登录。", 502
            )
        return previous.model_copy(
            update={
                "issuer": claims["iss"],
                "subject": claims["sub"],
                "email": claims.get("email") if label(claims.get("email"), 320) else None,
                "state": "connected",
                "refresh_state": "ready",
                "access_token": secret(data.get("access_token")),
                "refresh_token": secret(refresh) if refresh is not None else None,
                "id_token": identity_token,
                "scopes": scope.split(),
                "expires_at": time.time() + expires,
                "earliest_refresh_at": earliest,
                "generation": previous.generation + int(not refreshing),
                "remote_revocation_confirmed": None,
            }
        )

    @staticmethod
    def usable(profile, expected_generation=None, *, permission=True):
        if expected_generation is not None and profile.generation != expected_generation:
            raise auth_error(
                "chatgpt_session_changed", "ChatGPT 授权状态已改变，请重新确认当前连接。", 409
            )
        if profile.state != "connected" or profile.access_token is None:
            raise auth_error(
                "chatgpt_login_required", "此连接需要 selark chatgpt login 进行官方登录。"
            )
        if profile.refresh_state != "ready":
            raise auth_error(
                "chatgpt_refresh_uncertain",
                "上次刷新结果不确定，未重放刷新令牌；请重新登录该连接。",
            )
        if permission and not PLAN_SCOPES.issubset(profile.scopes):
            raise auth_error(
                "chatgpt_plan_permission_missing",
                "已登录但未授权套餐使用；请使用 chatgpt login --consent。",
                403,
            )

    async def token(self, identity, *, client=None, expected_generation=None, refresh_only=False):
        profile = await asyncio.to_thread(self.read, identity)
        if profile.refresh_state == "refreshing":
            # Another process may be rotating this token. Wait for its commit; if
            # the lock is free but the receipt remains, the previous refresh crashed.
            # That uncertain credential must never be replayed.
            async with self.locked(identity):
                profile = self.read(identity)
        self.usable(profile, expected_generation, permission=not refresh_only)
        if profile.expires_at and profile.expires_at > time.time() + 60:
            return profile
        if client is None:
            # A valid token is an offline file read. Avoid constructing TLS clients
            # (and loading system trust stores) on every model request.
            async with self.http() as client:
                return await self.token(
                    identity,
                    client=client,
                    expected_generation=expected_generation,
                    refresh_only=refresh_only,
                )
        async with self.locked(identity):
            profile = self.read(identity)
            self.usable(profile, expected_generation, permission=not refresh_only)
            if profile.expires_at and profile.expires_at > time.time() + 60:
                return profile
            if profile.earliest_refresh_at and profile.earliest_refresh_at > time.time():
                if profile.expires_at and profile.expires_at > time.time():
                    return profile
                raise auth_error(
                    "chatgpt_refresh_not_due", "尚未到官方允许的刷新时间，请稍后重试。", 503
                )
            if profile.refresh_token is None:
                raise auth_error(
                    "chatgpt_login_required", "访问凭据已过期且没有刷新凭据，请重新登录。"
                )
            pending = profile.model_copy(update={"refresh_state": "refreshing"})
            self.write(pending)  # Durable before sending a potentially rotating token.
            try:
                data = await self.exchange(
                    client,
                    {
                        "grant_type": "refresh_token",
                        "client_id": profile.client_id,
                        "refresh_token": profile.refresh_token.get_secret_value(),
                        "resource": RESOURCE,
                    },
                )
                updated = await self.accept(client, profile, data, refreshing=True)
                self.write(updated)
                return updated
            except BaseException as exc:
                if (
                    isinstance(exc, GatewayError)
                    and exc.code.removeprefix("chatgpt_") in TERMINAL_REFRESH
                ):
                    self.write(cleared(profile, "reauth_required"))
                else:
                    self.write(pending.model_copy(update={"refresh_state": "uncertain"}))
                raise

    async def logout(self, identity):
        async with self.locked(identity):
            profile = self.read(identity)
            stopping = profile.model_copy(
                update={"state": "signing_out", "generation": profile.generation + 1}
            )
            self.write(stopping)
            confirmed = False
            try:
                if profile.refresh_token and client_id(profile.client_id):
                    async with self.http() as client:
                        metadata = await self.metadata(client)
                        for attempt in range(2):
                            try:
                                status, _ = await self.document(
                                    client,
                                    "POST",
                                    metadata["revocation_endpoint"],
                                    empty=True,
                                    data={
                                        "token": profile.refresh_token.get_secret_value(),
                                        "token_type_hint": "refresh_token",
                                        "client_id": profile.client_id,
                                    },
                                )
                                if status == 200:
                                    confirmed = True
                                    break
                                if status < 500:
                                    break
                            except GatewayError:
                                pass
                            if attempt == 0:
                                await asyncio.sleep(0.25)
            except GatewayError:
                pass
            finally:
                updated = cleared(profile)
                updated.remote_revocation_confirmed = confirmed
                self.write(updated)
            return self.status(identity)

    async def login(self, identity, *, timeout=300, port=0, consent=False, launch=None):
        """Loopback callback only; callers own the browser and consent themselves."""
        host = await self.host()
        async with self.locked(identity, timeout=0, kind="login"):
            original = self.read(identity)
            state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .decode()
                .rstrip("=")
            )
            future = asyncio.get_running_loop().create_future()
            handlers = set()
            redirect = None

            async def callback(reader, writer):
                task = asyncio.current_task()
                handlers.add(task)
                status, message = 400, "Invalid callback. Return to Selark."
                try:
                    if len(handlers) > 8:
                        return
                    async with asyncio.timeout(5):
                        raw = await reader.readuntil(b"\r\n\r\n")
                        lines = raw.decode("ascii").split("\r\n")
                        method, target, version = lines[0].split(" ")
                        headers = [line.split(":", 1) for line in lines[1:] if line]
                        hosts = [v.strip() for k, v in headers if k.lower() == "host"]
                        address = urlsplit(target)
                        if method != "GET" or version not in {"HTTP/1.0", "HTTP/1.1"}:
                            raise ValueError
                        if hosts != [urlsplit(redirect).netloc] or address.netloc or address.scheme:
                            raise ValueError
                        if address.path != "/auth/callback":
                            status = 404
                            raise ValueError
                        query = parse_qs(address.query, keep_blank_values=True, max_num_fields=12)
                        if any(len(v) != 1 for v in query.values()):
                            raise ValueError
                        received = query.get("state", [""])[0]
                        if (
                            not secrets.compare_digest(received.encode(), state.encode())
                            or future.done()
                        ):
                            raise ValueError
                        if query.get("iss", [ISSUER])[0] != ISSUER:
                            raise ValueError
                        if "error" in query:
                            future.set_exception(
                                auth_error(
                                    "chatgpt_consent_denied",
                                    "登录或套餐授权被取消；原有连接保留。",
                                    403,
                                )
                            )
                        else:
                            issued = query.get("client_id", [original.client_id])[0]
                            code = query.get("code", [""])[0]
                            if not client_id(issued) or not label(code, 4096):
                                raise ValueError
                            if original.client_id and issued != original.client_id:
                                raise ValueError
                            future.set_result((issued, code))
                        status, message = (
                            200,
                            "Authorization received. Return to Selark to check the final result.",
                        )
                except (
                    ValueError,
                    UnicodeError,
                    asyncio.IncompleteReadError,
                    asyncio.LimitOverrunError,
                    TimeoutError,
                ):
                    pass
                finally:
                    body = message.encode()
                    writer.write(
                        (
                            f"HTTP/1.1 {status} Result\r\n"
                            "Content-Type: text/plain; charset=utf-8\r\n"
                            f"Content-Length: {len(body)}\r\nConnection: close\r\n"
                            "Cache-Control: no-store\r\nReferrer-Policy: no-referrer\r\n"
                            "Content-Security-Policy: default-src 'none'\r\n\r\n"
                        ).encode()
                        + body
                    )
                    with contextlib.suppress(OSError, TimeoutError):
                        await asyncio.wait_for(writer.drain(), 2)
                    writer.close()
                    with contextlib.suppress(OSError, TimeoutError):
                        await asyncio.wait_for(writer.wait_closed(), 2)
                    handlers.discard(task)

            server = await asyncio.start_server(callback, "127.0.0.1", port, limit=16384, backlog=8)
            selected_port = server.sockets[0].getsockname()[1]
            redirect = f"http://127.0.0.1:{selected_port}/auth/callback"
            query = {
                "client_id": original.client_id or DYNAMIC_CLIENT,
                "ext_agent_host_id": host,
                "response_type": "code",
                "redirect_uri": redirect,
                "scope": SCOPES,
                "resource": RESOURCE,
                "state": state,
                "nonce": nonce,
                "code_challenge_method": "S256",
                "code_challenge": challenge,
            }
            if not original.client_id:
                query["agent_name_hint"] = "Selark"
            if consent:
                query["prompt"] = "consent"
            # Hints are optional. Omit ID-token/login hints so URLs never contain tokens or email.
            url = AUTHORIZE + "?" + urlencode(query)
            try:
                async with asyncio.timeout(timeout):
                    if launch:
                        await launch(url)
                    else:
                        print("Continue with ChatGPT:\n" + url, flush=True)
                        await asyncio.to_thread(webbrowser.open, url)
                    issued, code = await future
                async with self.locked(identity):
                    current = self.read(identity)
                    if current.generation != original.generation:
                        raise auth_error(
                            "chatgpt_session_changed",
                            "等待授权期间该连接已退出或改变，请重新登录。",
                            409,
                        )
                    current.client_id = issued
                    if current.state == "new":
                        current.state = "registered"
                    self.write(current)  # Keep issued registration even if code exchange fails.
                    async with self.http() as client:
                        data = await self.exchange(
                            client,
                            {
                                "grant_type": "authorization_code",
                                "client_id": issued,
                                "code": code,
                                "code_verifier": verifier,
                                "redirect_uri": redirect,
                                "resource": RESOURCE,
                            },
                        )
                        updated = await self.accept(client, current, data, nonce=nonce)
                    self.write(updated)
                return self.status(identity)
            except TimeoutError:
                raise auth_error(
                    "chatgpt_login_timeout", "等待浏览器授权超时；原有凭据保留，可重新登录。", 408
                ) from None
            finally:
                server.close()
                await server.wait_closed()
                for task in list(handlers):
                    task.cancel()
                if handlers:
                    await asyncio.gather(*handlers, return_exceptions=True)
                if not future.done():
                    future.cancel()
