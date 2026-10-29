"""The offline burst harness must expose transport failures, never retry or hide them."""

import asyncio
import json

import httpx
import pytest

from scripts.verify_resource_matrix import mixed_burst

from .four_resources import FourUpstreams, app_for, auth, caller, payload
from .test_resource_matrix import resources as resources


@pytest.mark.parametrize("fault", [None, "transport", "primary_and_transport"])
def test_burst_preserves_failure_and_waits_for_every_client(resources, tmp_path, fault):
    async def scenario():
        upstream = FourUpstreams()
        app = app_for(resources, upstream, max_concurrent_requests=4, max_queued_requests=8)
        tokens = [caller(resources, "A"), caller(resources, "B")]
        issued, settled, pools = [], [], set()
        output = tmp_path / "burst.json"
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://local") as c,
        ):

            async def post(identity, *, who, session, http, **options):
                index = len(issued)
                issued.append(index)
                pools.add(id(http))
                try:
                    if fault and index == 9:
                        raise httpx.ReadError("synthetic transport failure; do not retry")
                    return await c.post(
                        "/v1/chat/completions",
                        headers=auth(tokens[who]),
                        json=payload(identity, **options) | {"session_id": session},
                    )
                finally:
                    settled.append(index)

            async def health():
                if fault == "primary_and_transport":
                    raise RuntimeError("synthetic primary failure")
                return (await c.get("/health")).json()

            async def idle():
                return app.state.admission.health()["admitted"] == 0

            args = (
                0,
                "http://127.0.0.1",
                post,
                health,
                idle,
                upstream,
                app.state.admission,
                output,
            )
            if fault:
                error = RuntimeError if fault == "primary_and_transport" else AssertionError
                with pytest.raises(error):
                    await mixed_burst(*args)
                evidence = json.loads(output.with_suffix(".failure.json").read_text())
                assert evidence["exception"] == error.__name__
                assert evidence["clients_submitted"] == evidence["clients_settled"] == 64
                assert evidence["client_errors"] == [{"client": 9, "exception": "ReadError"}]
                assert evidence["real_generation_requests"] == 0
            else:
                report = await mixed_burst(*args)
                assert report["passed"] and report["client_transport_errors"] == 0
                assert report["generation_retries"] == 0
                assert not output.with_suffix(".failure.json").exists()
            assert sorted(issued) == sorted(settled) == list(range(64))
            assert len(pools) == 1
            assert app.state.admission.health()["admitted"] == 0
            assert not app.state.admission.plans and not app.state.admission.callers
            total = resources.store.summary({})["summary"]
            assert total["upstream_attempts"] == len(upstream.calls)
            assert total["known_tokens"]["total_tokens"] == 14 * len(upstream.calls)

    asyncio.run(scenario())
