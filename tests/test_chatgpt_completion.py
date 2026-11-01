"""Observed SIWC completion shape and adversarial wire-level regressions."""

import copy
import json
import random

import httpx
import pytest
from fastapi.testclient import TestClient

from selark.sse import SSEDecoder

from .test_chatgpt import PAYLOAD, app_for, headers, result
from .test_chatgpt import resource as resource


def completed_stream(*, aggregate=False, texts=("你好 🐍\n", "42"), reasoning=True):
    """A real-shaped stream: response -> items -> parts -> item.done -> completion."""
    created = result("in_progress", None)
    created["output"] = []
    values = [{"type": "response.created", "response": created}]
    output = []
    if reasoning:
        item = {"id": "rs_test", "type": "reasoning", "summary": []}
        values += [
            {"type": "response.output_item.added", "output_index": 0, "item": item},
            {"type": "response.output_item.done", "output_index": 0, "item": item},
        ]
        output.append(item)
    for position, text in enumerate(texts, len(output)):
        item = {
            "id": f"msg_test_{position}",
            "type": "message",
            "role": "assistant",
            "status": "in_progress",
            "content": [],
        }
        ref = {"output_index": position, "item_id": item["id"], "content_index": 0}
        values.append(
            {
                "type": "response.output_item.added",
                "output_index": position,
                "item": copy.deepcopy(item),
            }
        )
        values.append(
            {
                "type": "response.content_part.added",
                **ref,
                "part": {"type": "output_text", "text": ""},
            }
        )
        for delta in (text[:1], text[1:]):
            values.append({"type": "response.output_text.delta", **ref, "delta": delta})
        values.append({"type": "response.output_text.done", **ref, "text": text})
        part = {"type": "output_text", "text": text}
        values.append({"type": "response.content_part.done", **ref, "part": part})
        item.update(status="completed", content=[part])
        values.append(
            {
                "type": "response.output_item.done",
                "output_index": position,
                "item": copy.deepcopy(item),
            }
        )
        output.append(item)
    terminal = result()
    terminal["output"] = copy.deepcopy(output) if aggregate else []
    values.append({"type": "response.completed", "response": terminal})
    return numbered(values)


def numbered(values):
    return [value | {"sequence_number": i} for i, value in enumerate(values)]


class FragmentedSSE(httpx.AsyncByteStream):
    def __init__(self, values, seed=0):
        self.body = b"\xef\xbb\xbf" + b"".join(
            ("data: " + json.dumps(value, ensure_ascii=False) + "\r\n\r\n").encode()
            for value in values
        )
        self.seed = seed
        self.closed = False

    async def __aiter__(self):
        rng = random.Random(self.seed)
        cursor = 0
        while cursor < len(self.body):
            end = cursor + rng.randint(1, 73)
            yield self.body[cursor:end]
            cursor = end

    async def aclose(self):
        self.closed = True


def invoke(resource, values, streaming, seed=0):
    runtime, caller, _ = resource
    calls = []
    body = FragmentedSSE(values, seed)

    def handler(request):
        calls.append(request)
        return httpx.Response(200, stream=body, headers={"Content-Type": "text/event-stream"})

    with TestClient(app_for(runtime, handler)) as client:
        response = client.post(
            "/v1/chat/completions", headers=headers(caller), json=PAYLOAD | {"stream": streaming}
        )
    assert len(calls) == 1 and body.closed
    row = runtime.store.records({})[0]
    assert row["upstream_attempts"] == 1
    return response, row


def downstream_text(response, streaming):
    if not streaming:
        return response.json()["choices"][0]["message"]["content"]
    decoder, pieces, usages, finishes = SSEDecoder(), [], [], []
    assert response.text.count("data: [DONE]") == 1
    for line in response.text.splitlines():
        raw = decoder.feed(line)
        if not raw or raw == "[DONE]":
            continue
        event = json.loads(raw)
        assert "error" not in event
        if event.get("usage"):
            usages.append(event["usage"])
        for choice in event["choices"]:
            pieces.append(choice["delta"].get("content", ""))
            if choice.get("finish_reason"):
                finishes.append(choice["finish_reason"])
    assert finishes == ["stop"] and len(usages) == 1
    return "".join(pieces)


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("aggregate", [False, True])
@pytest.mark.parametrize("seed", [7, 31, 99])
def test_completed_items_survive_empty_aggregate_and_fragmented_unicode(
    resource, streaming, aggregate, seed
):
    values = completed_stream(aggregate=aggregate)
    response, row = invoke(resource, values, streaming, seed)
    assert response.status_code == 200
    assert downstream_text(response, streaming) == "你好 🐍\n42"
    assert row["status"] == "success" and row["usage_final"] and row["usage_known"]
    assert row["total_tokens"] == 15 and row["delivery_status"] == "sent"


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("shape", ["snapshot_only", "missing_tail", "refusal", "no_content"])
def test_completed_snapshot_recovers_only_missing_text(resource, streaming, shape):
    expected = "Hello world"
    values = completed_stream(texts=(expected,), reasoning=False)
    if shape == "snapshot_only":
        values = [
            v
            for v in values
            if v["type"] in {"response.created", "response.output_item.done", "response.completed"}
        ]
    elif shape == "missing_tail":
        values = [v for v in values if v.get("delta") != expected[1:]]
    elif shape == "refusal":
        for value in values:
            value["type"] = value["type"].replace("output_text.", "refusal.")
            if value["type"] == "response.refusal.done":
                value["refusal"] = value.pop("text")
            parts = [value["part"]] if "part" in value else value.get("item", {}).get("content", [])
            for part in parts:
                part["type"] = "refusal"
                part["refusal"] = part.pop("text")
    else:
        values = [values[0], values[-1]]
        expected = ""
    response, row = invoke(resource, numbered(values), streaming)
    assert response.status_code == 200 and row["status"] == "success"
    assert downstream_text(response, streaming) == expected


FAULTS = [
    "no_response_completed",
    "failed",
    "incomplete",
    "no_item_done",
    "partial_second_item",
    "wrong_delta",
    "wrong_text_done",
    "wrong_part_done",
    "wrong_item_done",
    "wrong_aggregate",
    "duplicate_item_done",
    "duplicate_part_done",
    "duplicate_sequence",
    "reordered_items",
    "wrong_item_id",
    "reused_item_id",
    "wrong_part_index",
    "bool_index",
    "negative_index",
    "huge_index",
    "index_gap",
    "late_delta",
    "late_part",
    "unfinished_item",
    "tool",
    "wrong_response_id",
    "wrong_response_model",
    "missing_output",
    "malformed_output",
]


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("fault", FAULTS)
def test_incomplete_conflicting_or_misidentified_items_never_finish(resource, streaming, fault):
    values = completed_stream(aggregate=fault == "wrong_aggregate")
    delta = next(v for v in values if v["type"] == "response.output_text.delta")
    text_done = next(v for v in values if v["type"] == "response.output_text.done")
    part_done = next(v for v in values if v["type"] == "response.content_part.done")
    item_done = next(
        v
        for v in values
        if v["type"] == "response.output_item.done" and v["item"]["type"] == "message"
    )
    if fault == "no_response_completed":
        values.pop()
    elif fault in {"failed", "incomplete"}:
        values[-1]["type"] = "response." + fault
        values[-1]["response"]["status"] = fault
    elif fault in {"no_item_done", "partial_second_item"}:
        position = 1 if fault == "no_item_done" else 2
        values = [
            v
            for v in values
            if not (v["type"] == "response.output_item.done" and v["output_index"] == position)
        ]
    elif fault == "wrong_delta":
        delta["delta"] = "wrong"
    elif fault == "wrong_text_done":
        text_done["text"] = "wrong"
    elif fault == "wrong_part_done":
        part_done["part"]["text"] = "wrong"
    elif fault == "wrong_item_done":
        item_done["item"]["content"][0]["text"] = "wrong"
    elif fault == "wrong_aggregate":
        values[-1]["response"]["output"][1]["content"][0]["text"] = "wrong"
    elif fault in {"duplicate_item_done", "duplicate_part_done"}:
        value = item_done if fault == "duplicate_item_done" else part_done
        values.insert(values.index(value) + 1, copy.deepcopy(value))
    elif fault == "reordered_items":
        for value in values:
            if value.get("output_index") in {1, 2}:
                value["output_index"] = 3 - value["output_index"]
    elif fault == "wrong_item_id":
        delta["item_id"] = "msg_other"
    elif fault == "reused_item_id":
        for value in values:
            if value.get("output_index") == 2:
                if "item_id" in value:
                    value["item_id"] = "msg_test_1"
                if "item" in value:
                    value["item"]["id"] = "msg_test_1"
    elif fault in {"wrong_part_index", "bool_index", "negative_index", "huge_index"}:
        delta["content_index"] = {
            "wrong_part_index": 1,
            "bool_index": True,
            "negative_index": -1,
            "huge_index": 1000000,
        }[fault]
    elif fault == "index_gap":
        for value in values:
            if "output_index" in value:
                value["output_index"] += 1
    elif fault in {"late_delta", "late_part"}:
        values.insert(
            values.index(item_done) + 1,
            copy.deepcopy(delta if fault == "late_delta" else part_done),
        )
    elif fault == "unfinished_item":
        item_done["item"]["status"] = "in_progress"
    elif fault == "tool":
        item_done["item"]["type"] = "function_call"
    elif fault == "wrong_response_id":
        values[-1]["response"]["id"] = "resp_other"
    elif fault == "wrong_response_model":
        values[-1]["response"]["model"] = "gpt-other"
    elif fault == "missing_output":
        values[-1]["response"].pop("output")
    elif fault == "malformed_output":
        values[-1]["response"]["output"] = {}
    values = numbered(values)
    if fault == "duplicate_sequence":
        values[2]["sequence_number"] = values[1]["sequence_number"]
    response, row = invoke(resource, values, streaming)
    assert "data: [DONE]" not in response.text
    assert row["status"] == "failed"
    assert response.status_code == (200 if streaming else 502)
    assert row["total_tokens"] == (
        15
        if fault
        in {
            "failed",
            "incomplete",
            "no_item_done",
            "partial_second_item",
            "wrong_aggregate",
            "reordered_items",
            "index_gap",
            "missing_output",
            "malformed_output",
        }
        else None
    )


@pytest.mark.parametrize("streaming", [False, True])
def test_text_evidence_is_local_to_each_request_and_not_reused(resource, streaming):
    runtime, caller, _ = resource
    first = completed_stream(texts=("first",), reasoning=False)
    empty = [first[0], first[-1]]
    responses = iter([first, empty])

    def handler(_):
        return httpx.Response(
            200,
            stream=FragmentedSSE(next(responses)),
            headers={"Content-Type": "text/event-stream"},
        )

    with TestClient(app_for(runtime, handler)) as client:
        for expected in ("first", ""):
            response = client.post(
                "/v1/chat/completions",
                headers=headers(caller),
                json=PAYLOAD | {"stream": streaming},
            )
            assert downstream_text(response, streaming) == expected
    assert len(runtime.store.records({})) == 2
    assert runtime.store.summary({})["summary"]["known_tokens"]["total_tokens"] == 30


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("aggregate", [False, True])
def test_multiple_parts_and_reasoning_preserve_text_order(resource, streaming, aggregate):
    values = completed_stream(texts=("A", "B"), aggregate=aggregate)
    # Combine the two visible messages into two parts of one message, preserving
    # the wire text and the independent reasoning item at output index zero.
    first_done = next(
        v for v in values if v["type"] == "response.output_item.done" and v["output_index"] == 1
    )
    second_done = next(
        v for v in values if v["type"] == "response.output_item.done" and v["output_index"] == 2
    )
    values.remove(first_done)
    values = [
        v
        for v in values
        if not (v["type"] == "response.output_item.added" and v["output_index"] == 2)
    ]
    for value in values:
        if value.get("output_index") == 2:
            value["output_index"] = 1
            if "item_id" in value:
                value.update(item_id="msg_test_1", content_index=1)
    second_done["item"] = copy.deepcopy(first_done["item"])
    second_done["item"]["content"].append({"type": "output_text", "text": "B"})
    if aggregate:
        values[-1]["response"]["output"] = [
            values[-1]["response"]["output"][0],
            copy.deepcopy(second_done["item"]),
        ]
    values.insert(
        2,
        {
            "type": "response.content_part.done",
            "output_index": 0,
            "item_id": "rs_test",
            "content_index": 0,
            "part": {"type": "reasoning_text", "text": "private-test-reasoning"},
        },
    )
    response, row = invoke(resource, numbered(values), streaming)
    assert response.status_code == 200 and row["status"] == "success"
    assert downstream_text(response, streaming) == "AB"
    assert "private-test-reasoning" not in response.text


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("shape", ["oversize_text", "too_many_parts", "duplicate_identity"])
def test_snapshot_limits_cannot_bypass_the_text_stream_limits(
    resource, monkeypatch, streaming, shape
):
    from selark.providers import responses_text

    values = completed_stream(texts=("abcde",), reasoning=False)
    item_done = next(v for v in values if v["type"] == "response.output_item.done")
    if shape == "oversize_text":
        monkeypatch.setattr(responses_text, "MAX_TEXT_BYTES", 4)
    elif shape == "too_many_parts":
        item_done["item"]["content"] *= 65
    else:
        values.insert(
            -1,
            {
                "type": "response.output_item.done",
                "output_index": 1,
                "item": copy.deepcopy(item_done["item"]),
            },
        )
    response, row = invoke(resource, numbered(values), streaming)
    assert "[DONE]" not in response.text and row["status"] == "failed"
    assert response.status_code == (200 if streaming else 502)
