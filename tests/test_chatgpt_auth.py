"""Own public-client registration; all identities and OAuth responses are synthetic."""

import asyncio
import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from selark.chatgpt_auth import (
    AUTHORIZE,
    DYNAMIC_CLIENT,
    ISSUER,
    RESOURCE,
    SCOPES,
    TOKEN,
    ChatGPTAuth,
)
from selark.config import ConfigError
from selark.errors import GatewayError


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class Authority:
    def __init__(self, key):
        self.key = key
        self.query = None
        self.claim_patch = {}
        self.scope = SCOPES
        self.grants = []
        self.failure = None
        self.revoke_status = 200

    async def request(self, request):
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": AUTHORIZE,
                    "token_endpoint": TOKEN,
                    "jwks_uri": ISSUER + "/.well-known/jwks.json",
                    "revocation_endpoint": ISSUER + "/api/accounts/oauth/revoke",
                    "id_token_signing_alg_values_supported": ["RS256"],
                },
            )
        if request.url.path == "/.well-known/jwks.json":
            key = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
            return httpx.Response(200, json={"keys": [{**key, "kid": "test", "use": "sig"}]})
        if request.url.path.endswith("/revoke"):
            return httpx.Response(self.revoke_status, json={})
        assert str(request.url) == TOKEN
        data = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.grants.append(data["grant_type"])
        assert data["client_id"] == "selark-test-client"
        assert data["resource"] == RESOURCE
        if self.failure == "timeout":
            raise httpx.ReadTimeout("synthetic", request=request)
        if self.failure:
            return httpx.Response(400, json={"error": self.failure})
        if data["grant_type"] == "authorization_code":
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(data["code_verifier"].encode()).digest())
                .decode()
                .rstrip("=")
            )
            assert challenge == self.query["code_challenge"]
            assert data["redirect_uri"] == self.query["redirect_uri"]
        claims = {
            "iss": ISSUER,
            "sub": "synthetic-account",
            "aud": "selark-test-client",
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
            "nonce": self.query["nonce"],
            "email": "user@example.invalid",
            **self.claim_patch,
        }
        return httpx.Response(
            200,
            json={
                "token_type": "Bearer",
                "access_token": "fake-access-" + str(len(self.grants)),
                "refresh_token": "fake-refresh-" + str(len(self.grants)),
                "expires_in": 3600,
                "scope": self.scope,
                "id_token": jwt.encode(
                    claims, self.key, algorithm="RS256", headers={"kid": "test"}
                ),
            },
        )

    async def launch(self, url):
        self.query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        assert url.startswith(AUTHORIZE + "?")
        params = {
            "state": self.query["state"],
            "code": "fake-code",
            "client_id": "selark-test-client",
        }
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.get(self.query["redirect_uri"], params=params)
            assert response.status_code == 200


def auth(tmp_path, signing_key):
    authority = Authority(signing_key)
    return ChatGPTAuth(tmp_path, transport=httpx.MockTransport(authority.request)), authority


def test_registration_and_identity(tmp_path, signing_key):
    async def scenario():
        manager, server = auth(tmp_path, signing_key)
        first = await manager.login("personal", launch=server.launch)
        original = server.query.copy()
        assert original["client_id"] == DYNAMIC_CLIENT
        assert original["ext_agent_host_id"].startswith("urn:uuid:")
        assert first["ready"] and first["plan_permission"]
        assert "fake-access" not in json.dumps(first)
        assert manager.path("personal").stat().st_mode & 0o777 == 0o600
        await manager.login("personal", launch=server.launch)
        assert server.query["client_id"] == "selark-test-client"
        assert server.query["ext_agent_host_id"] == original["ext_agent_host_id"]
        assert server.query["nonce"] != original["nonce"]
        assert server.query["state"] != original["state"]
        assert "id_token_hint" not in server.query and "login_hint" not in server.query

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "patch",
    [
        {"iss": "https://example.invalid"},
        {"aud": "wrong"},
        {"nonce": "wrong"},
        {"exp": 1},
        {"sub": ""},
        {"azp": "wrong"},
    ],
)
def test_invalid_identity_not_saved(tmp_path, signing_key, patch):
    async def scenario():
        manager, server = auth(tmp_path, signing_key)
        server.claim_patch = patch
        with pytest.raises(GatewayError, match="校验失败"):
            await manager.login("personal", launch=server.launch)
        profile = manager.read("personal")
        assert profile.client_id == "selark-test-client"
        assert profile.access_token is None

    asyncio.run(scenario())


def test_callbacks_do_not_consume_invalid_state(tmp_path, signing_key):
    async def scenario():
        manager, server = auth(tmp_path, signing_key)

        async def launch(url):
            q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
            good = {"state": q["state"], "code": "fake-code", "client_id": "selark-test-client"}
            async with httpx.AsyncClient(trust_env=False) as client:
                for patch in ({"state": "bad"}, {"iss": "https://example.invalid"}):
                    assert (
                        await client.get(q["redirect_uri"], params={**good, **patch})
                    ).status_code == 400
                assert (
                    await client.get(
                        q["redirect_uri"], params=good, headers={"Host": "bad.invalid"}
                    )
                ).status_code == 400
            await server.launch(url)
            async with httpx.AsyncClient(trust_env=False) as client:
                assert (await client.get(q["redirect_uri"], params=good)).status_code == 400

        await manager.login("personal", launch=launch)
        assert server.grants == ["authorization_code"]

    asyncio.run(scenario())


def test_missing_plan_scope_and_denial(tmp_path, signing_key):
    async def scenario():
        manager, server = auth(tmp_path, signing_key)
        server.scope = "openid profile email offline_access"
        result = await manager.login("personal", launch=server.launch)
        assert result["state"] == "connected" and not result["ready"]
        with pytest.raises(GatewayError) as error:
            await manager.token("personal")
        assert error.value.code == "chatgpt_plan_permission_missing"

        async def denied(url):
            q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
            async with httpx.AsyncClient(trust_env=False) as client:
                await client.get(
                    q["redirect_uri"]
                    + "?"
                    + urlencode({"state": q["state"], "error": "access_denied"})
                )

        with pytest.raises(GatewayError) as error:
            await manager.login("other", launch=denied)
        assert error.value.code == "chatgpt_consent_denied"
        assert server.grants == ["authorization_code"]

    asyncio.run(scenario())


def test_failed_exchange_keeps_registration(tmp_path, signing_key):
    async def scenario():
        manager, server = auth(tmp_path, signing_key)
        server.failure = "invalid_grant"
        with pytest.raises(GatewayError):
            await manager.login("personal", launch=server.launch)
        server.failure = None
        await manager.login("personal", launch=server.launch)
        assert server.query["client_id"] == "selark-test-client"

    asyncio.run(scenario())


def test_refresh_serialized_across_instances(tmp_path, signing_key):
    async def scenario():
        manager, server = auth(tmp_path, signing_key)
        await manager.login("personal", launch=server.launch)
        profile = manager.read("personal")
        profile.expires_at = time.time() - 1
        manager.write(profile)
        other = ChatGPTAuth(tmp_path, transport=manager.transport)
        results = await asyncio.gather(manager.token("personal"), other.token("personal"))
        assert [p.access_token.get_secret_value() for p in results] == ["fake-access-2"] * 2
        assert all(p.generation == profile.generation for p in results)
        assert server.grants == ["authorization_code", "refresh_token"]

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["timeout", "invalid_grant", "refresh_token_reused"])
def test_uncertain_refresh_not_replayed(tmp_path, signing_key, failure):
    async def scenario():
        manager, server = auth(tmp_path, signing_key)
        await manager.login("personal", launch=server.launch)
        profile = manager.read("personal")
        profile.expires_at = time.time() - 1
        manager.write(profile)
        server.failure = failure
        for _ in range(2):
            with pytest.raises(GatewayError):
                await manager.token("personal")
        assert server.grants == ["authorization_code", "refresh_token"]
        final = manager.read("personal")
        if failure == "timeout":
            assert final.refresh_state == "uncertain" and final.refresh_token
        else:
            assert final.state == "reauth_required" and final.refresh_token is None

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [200, 503])
def test_logout_removes_credentials(tmp_path, signing_key, status):
    async def scenario():
        manager, server = auth(tmp_path, signing_key)
        await manager.login("personal", launch=server.launch)
        server.revoke_status = status
        result = await manager.logout("personal")
        assert result["state"] == "signed_out"
        assert result["remote_revocation_confirmed"] == (status == 200)
        saved = manager.read("personal")
        assert saved.client_id == "selark-test-client" and saved.access_token is None
        with pytest.raises(GatewayError):
            await manager.token("personal")

    asyncio.run(scenario())


def test_logout_during_browser_wait_does_not_resurrect(tmp_path, signing_key):
    async def scenario():
        manager, server = auth(tmp_path, signing_key)

        async def launch(url):
            await manager.logout("personal")
            await server.launch(url)

        with pytest.raises(GatewayError) as error:
            await manager.login("personal", launch=launch)
        assert error.value.code == "chatgpt_session_changed"
        assert not server.grants

    asyncio.run(scenario())


def test_status_offline_and_private(tmp_path, signing_key):
    manager, _ = auth(tmp_path, signing_key)
    assert manager.status("personal")["state"] == "new"
    assert not manager.root.exists()
    asyncio.run(manager.host())
    with pytest.raises(ConfigError):
        manager.read("../../escape")


def test_late_refresh_waiter_waits_for_rotation(tmp_path, signing_key):
    async def scenario():
        manager, server = auth(tmp_path, signing_key)
        await manager.login("personal", launch=server.launch)
        original = manager.read("personal")
        original.expires_at = time.time() - 1
        manager.write(original)
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow(request):
            if str(request.url) == TOKEN:
                entered.set()
                await release.wait()
            return await server.request(request)

        manager.transport = httpx.MockTransport(slow)
        other = ChatGPTAuth(tmp_path, transport=manager.transport)
        first = asyncio.create_task(manager.token("personal"))
        await asyncio.wait_for(entered.wait(), 2)
        second = asyncio.create_task(other.token("personal"))
        await asyncio.sleep(0.03)
        assert not second.done()
        release.set()
        a, b = await asyncio.gather(first, second)
        assert a.access_token == b.access_token and len(server.grants) == 2

    asyncio.run(scenario())


def test_crashed_rotation_and_refresh_window(tmp_path, signing_key):
    async def scenario():
        manager, server = auth(tmp_path, signing_key)
        await manager.login("personal", launch=server.launch)
        profile = manager.read("personal")
        profile.expires_at = time.time() - 1
        profile.earliest_refresh_at = time.time() + 1000
        manager.write(profile)
        with pytest.raises(GatewayError) as error:
            await manager.token("personal")
        assert error.value.code == "chatgpt_refresh_not_due"
        profile.refresh_state = "refreshing"
        manager.write(profile)
        with pytest.raises(GatewayError) as error:
            await manager.token("personal")
        assert error.value.code == "chatgpt_refresh_uncertain"
        assert server.grants == ["authorization_code"]

    asyncio.run(scenario())
