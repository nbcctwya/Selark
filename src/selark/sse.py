"""Small SSE data decoder shared by the HTTP client and provider adapter."""


class SSEDecoder:
    def __init__(self):
        self.parts: list[str] = []
        self.size = 0

    def feed(self, line: str) -> str | None:
        if len(line) > 1_048_576:
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
            self.size += len(value)
            if self.size > 1_048_576:
                raise ValueError("SSE 事件过大")
            self.parts.append(value)
        return None
