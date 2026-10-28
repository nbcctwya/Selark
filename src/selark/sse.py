"""Small SSE data decoder shared by the HTTP client and provider adapter."""

import re

MAX_SSE_BYTES = 1_048_576
NEWLINE = re.compile(b"[\r\n]")


class SSELines:
    """Enforce a UTF-8 byte limit while receiving a line, even without a newline."""

    def __init__(self):
        self.buffer = bytearray()
        self.skip_lf = False
        self.first = True

    def append(self, chunk, start, end):
        if len(self.buffer) + end - start > MAX_SSE_BYTES:
            raise ValueError("SSE 数据行过大")
        self.buffer.extend(memoryview(chunk)[start:end])

    def take(self):
        line = self.buffer.decode("utf-8")
        self.buffer.clear()
        if self.first:
            line = line.removeprefix("\ufeff")
            self.first = False
        return line

    def feed(self, chunk):
        start = 0
        while start < len(chunk):
            if self.skip_lf:
                if chunk[start] == 10:
                    start += 1
                self.skip_lf = False
            match = NEWLINE.search(chunk, start)
            if match is None:
                self.append(chunk, start, len(chunk))
                return
            self.append(chunk, start, match.start())
            self.skip_lf = chunk[match.start()] == 13
            start = match.end()
            yield self.take()


async def async_sse_lines(response):
    lines = SSELines()
    async for chunk in response.aiter_bytes():
        for line in lines.feed(chunk):
            yield line
    if lines.buffer:
        yield lines.take()


def sse_lines(response):
    lines = SSELines()
    for chunk in response.iter_bytes():
        yield from lines.feed(chunk)
    if lines.buffer:
        yield lines.take()


class SSEDecoder:
    def __init__(self):
        self.parts: list[str] = []
        self.size = 0

    def feed(self, line: str) -> str | None:
        if len(line.encode("utf-8")) > MAX_SSE_BYTES:
            raise ValueError("SSE 事件过大")
        if line == "":
            result = "\n".join(self.parts) if self.parts else None
            self.parts = []
            self.size = 0
            return result
        if line.startswith("data:"):
            value = line[5:]
            if value.startswith(" "):
                value = value[1:]
            # Include separators: many empty data fields must not grow an unbounded list.
            self.size += len(value.encode("utf-8")) + bool(self.parts)
            if self.size > MAX_SSE_BYTES:
                raise ValueError("SSE 事件过大")
            self.parts.append(value)
        return None
