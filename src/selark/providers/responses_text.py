"""Bounded text evidence for Responses streams, including omitted terminal output."""

from dataclasses import dataclass, field

from selark.providers.mimo import protocol_error, response_identity

MAX_TEXT_BYTES = 8 * 1024 * 1024
MAX_ITEMS = 64
MAX_PARTS = 64


def index(value, limit):
    if type(value) is not int or not 0 <= value < limit:
        raise protocol_error("Responses 回复序号无效。")
    return value


def text_part(part):
    if not isinstance(part, dict):
        raise protocol_error()
    kind = part.get("type")
    if kind not in ("output_text", "refusal"):
        raise protocol_error("此适配仅支持 Responses 文本回复。")
    value = part.get("text" if kind == "output_text" else "refusal")
    if not isinstance(value, str):
        raise protocol_error()
    return kind, value


def message_parts(item):
    if item.get("role") != "assistant" or not isinstance(item.get("content"), list):
        raise protocol_error("此适配仅支持 Responses 文本回复。")
    if len(item["content"]) > MAX_PARTS:
        raise protocol_error("Responses 文本分块数量超过本地上限。")
    return [text_part(part) for part in item["content"]]


@dataclass
class Part:
    kind: str
    deltas: list[str] = field(default_factory=list)
    final: str | None = None
    completed_events: set[str] = field(default_factory=set)

    def check(self, value):
        if not value.startswith("".join(self.deltas)) or (
            self.final is not None and value != self.final
        ):
            raise protocol_error("Responses 完成文本与已接收片段不一致。")


@dataclass
class Item:
    identity: str
    kind: str
    added: bool = False
    done: bool = False
    parts: dict[int, Part] = field(default_factory=dict)
    final: list[tuple[str, str]] = field(default_factory=list)

    def check(self, parts):
        if self.done and parts != self.final:
            raise protocol_error("Responses 终态与已完成消息不一致。")
        for position, part in self.parts.items():
            if position >= len(parts) or parts[position][0] != part.kind:
                raise protocol_error("Responses 终态缺少已接收的文本分块。")
            part.check(parts[position][1])


class ResponseText:
    """Never infer response completion from deltas or item completion alone."""

    def __init__(self):
        self.items = {}
        self.identities = {}
        self.saved_bytes = 0

    def item(self, position, identity, kind):
        position = index(position, MAX_ITEMS)
        if not response_identity(identity) or kind not in ("message", "reasoning"):
            raise protocol_error("Responses 消息身份或类型无效。")
        if identity in self.identities and self.identities[identity] != position:
            raise protocol_error("Responses 消息身份重复。")
        item = self.items.get(position)
        if item is None:
            item = self.items[position] = Item(identity, kind)
            self.identities[identity] = position
        elif (item.identity, item.kind) != (identity, kind):
            raise protocol_error("Responses 流切换了消息身份或类型。")
        return item

    def part(self, event, kind):
        item = self.item(event.get("output_index"), event.get("item_id"), "message")
        if item.done:
            raise protocol_error("Responses 已完成消息后出现额外文本事件。")
        position = index(event.get("content_index"), MAX_PARTS)
        part = item.parts.setdefault(position, Part(kind))
        if part.kind != kind:
            raise protocol_error("Responses 文本分块类型变化。")
        return part

    def reserve(self, size):
        self.saved_bytes += size
        # Deltas, part finals and item finals each carry at most one text copy.
        if self.saved_bytes > 3 * MAX_TEXT_BYTES:
            raise protocol_error("上游响应超过本地文本缓存上限。")

    def delta(self, event):
        kind = "refusal" if event["type"] == "response.refusal.delta" else "output_text"
        part = self.part(event, kind)
        value = event.get("delta")
        if not isinstance(value, str) or part.completed_events:
            raise protocol_error("Responses 完成文本后出现额外片段。")
        self.reserve(len(value.encode()))
        part.deltas.append(value)

    def part_event(self, event):
        event_type = event["type"]
        if event_type.startswith("response.content_part."):
            raw = event.get("part")
            if isinstance(raw, dict) and raw.get("type") == "reasoning_text":
                item = self.item(event.get("output_index"), event.get("item_id"), "reasoning")
                index(event.get("content_index"), MAX_PARTS)
                if item.done or not isinstance(raw.get("text"), str):
                    raise protocol_error("Responses 思考分块无效或乱序。")
                return  # Reasoning never becomes assistant-visible answer text.
            kind, value = text_part(event.get("part"))
        else:
            kind = "refusal" if event_type == "response.refusal.done" else "output_text"
            key = "refusal" if kind == "refusal" else "text"
            kind, value = text_part({"type": kind, key: event.get(key)})
        part = self.part(event, kind)
        if event_type.endswith(".added"):
            if part.completed_events or part.deltas:
                raise protocol_error("Responses 文本分块添加事件乱序。")
            return
        if event_type in part.completed_events:
            raise protocol_error("Responses 文本完成事件重复。")
        part.check(value)
        if part.final is None:
            self.reserve(len(value.encode()))
        part.final = value
        part.completed_events.add(event_type)

    def item_event(self, event):
        value = event.get("item")
        if not isinstance(value, dict):
            raise protocol_error()
        item = self.item(event.get("output_index"), value.get("id"), value.get("type"))
        if item.done:
            raise protocol_error("Responses 消息完成事件重复或乱序。")
        if event["type"] == "response.output_item.added":
            if item.added:
                raise protocol_error("Responses 消息添加事件重复。")
            item.added = True
            return
        if value.get("status") not in (
            ("completed",) if item.kind == "message" else (None, "completed")
        ):
            raise protocol_error("Responses 消息尚未完成。")
        parts = message_parts(value) if item.kind == "message" else []
        item.check(parts)
        self.reserve(sum(len(text.encode()) for _, text in parts))
        item.final, item.done = parts, True

    def finish(self, output):
        if not isinstance(output, list) or len(output) > MAX_ITEMS:
            raise protocol_error()
        final = []
        if output:
            for position, value in enumerate(output):
                if not isinstance(value, dict):
                    raise protocol_error()
                item = self.item(position, value.get("id"), value.get("type"))
                parts = message_parts(value) if item.kind == "message" else []
                item.check(parts)
                final.extend(text for _, text in parts)
            if any(position >= len(output) for position in self.items):
                raise protocol_error("Responses 终态缺少已接收的消息。")
        else:
            # Observed SIWC completion can omit the aggregate output. Only complete,
            # indexed item snapshots may replace it; partial deltas never qualify.
            if set(self.items) != set(range(len(self.items))) or any(
                not item.done for item in self.items.values()
            ):
                raise protocol_error("Responses 空终态缺少完整的消息完成证据。")
            for position in sorted(self.items):
                final.extend(text for _, text in self.items[position].final)
        if sum(len(text.encode()) for text in final) > MAX_TEXT_BYTES:
            raise protocol_error("上游响应超过 8 MiB 上限。")
        return "".join(final)
