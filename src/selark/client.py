"""HTTP-only Selark client. This module never imports a provider or reads server.json."""

import json
import re
import sys
import time

import httpx

from selark.config import ClientConfig
from selark.sse import SSEDecoder, sse_lines


class ClientError(ValueError):
    pass


class SelarkClient:
    def __init__(self, config: ClientConfig, *, transport=None):
        self.config = config
        self.http = httpx.Client(
            base_url=config.url,
            headers={"Authorization": f"Bearer {config.access_token.get_secret_value()}"},
            timeout=httpx.Timeout(660, connect=5, pool=5),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.http.close()

    def failure(self, message, response, code=None):
        message = str(message).replace(self.config.access_token.get_secret_value(), "[REDACTED]")
        message = "".join(c for c in message if c.isprintable())[:800]
        identity = response.headers.get("x-request-id", "")
        suffix = ""
        if isinstance(code, str) and re.fullmatch(r"[a-z0-9_]{1,80}", code):
            suffix += f" [{code}]"
        if re.fullmatch(r"[A-Za-z0-9_-]{1,100}", identity):
            suffix += f"；请求 {identity}，本机可用 selark records show {identity} 查看"
        retry = response.headers.get("retry-after", "")
        if retry.isascii() and retry.isdigit() and len(retry) <= 6:
            suffix += f"；建议等待 {retry} 秒后核对状态"
        return ClientError(message + suffix)

    def check(self, response: httpx.Response) -> None:
        if response.status_code == 200:
            return
        code = None
        try:
            error = response.json()["error"]
            message = error["message"]
            code = error.get("code")
            if not isinstance(message, str):
                raise ValueError
            message = message.replace(self.config.access_token.get_secret_value(), "[REDACTED]")
        except (ValueError, KeyError, TypeError):
            message = "Selark 返回了错误响应。"
        raise self.failure(f"HTTP {response.status_code}：{message}", response, code)

    def models(self, connection_id=None) -> dict:
        response = self.http.get(
            "/v1/models", params={"connection_id": connection_id} if connection_id else None
        )
        self.check(response)
        try:
            data = response.json()
            if not isinstance(data["data"], list):
                raise ValueError
            return data
        except (ValueError, KeyError, TypeError):
            raise ClientError("Selark 模型列表响应无效。") from None

    def ask(self, payload: dict, *, raw_json: bool = False) -> None:
        started = time.monotonic()
        usage = None
        model = payload.get("model")
        finish_reason = None
        connection = payload.get("connection_id")
        if payload.get("stream"):
            done = False
            decoder = SSEDecoder()
            with self.http.stream("POST", "/v1/chat/completions", json=payload) as response:
                request_id = response.headers.get("x-request-id")
                connection = response.headers.get("x-selark-connection", connection)
                if response.status_code != 200:
                    response.read()
                    self.check(response)
                if not response.headers.get("content-type", "").startswith("text/event-stream"):
                    raise ClientError("Selark 未返回 SSE 流。")
                try:
                    for line in sse_lines(response):
                        event = decoder.feed(line)
                        if event is None:
                            continue
                        if event == "[DONE]":
                            done = True
                            break
                        data = json.loads(event)
                        if "error" in data:
                            message = str(data["error"].get("message", "流式请求失败"))
                            raise self.failure(message, response, data["error"].get("code"))
                        model = data.get("model", model)
                        if isinstance(data.get("usage"), dict):
                            usage = data["usage"]
                        for choice in data["choices"]:
                            content = choice["delta"].get("content")
                            if content:
                                print(content, end="", flush=True)
                            if choice.get("finish_reason"):
                                finish_reason = choice["finish_reason"]
                except ClientError:
                    print(file=sys.stdout)
                    raise
                except (ValueError, KeyError, TypeError, AttributeError):
                    raise self.failure("Selark SSE 数据无效；输出可能不完整。", response) from None
                except httpx.HTTPError:
                    raise self.failure(
                        "Selark 流连接中断或超时；输出可能不完整，未自动重发。", response
                    ) from None
            print()
            if not done or finish_reason is None:
                raise self.failure("Selark 流提前结束；输出可能不完整。", response)
        else:
            response = self.http.post("/v1/chat/completions", json=payload)
            request_id = response.headers.get("x-request-id")
            connection = response.headers.get("x-selark-connection", connection)
            self.check(response)
            try:
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                if not isinstance(content, str):
                    raise ValueError
                model = data.get("model", model)
                usage = data.get("usage")
                finish_reason = data["choices"][0].get("finish_reason")
            except (ValueError, KeyError, TypeError, IndexError):
                raise ClientError("Selark 对话响应无效。") from None
            print(json.dumps(data, ensure_ascii=False) if raw_json else content)
        print(
            json.dumps(
                {
                    "request_id": request_id,
                    "model": model,
                    "connection_id": connection,
                    "duration_ms": round((time.monotonic() - started) * 1000, 2),
                    "finish_reason": finish_reason,
                    "usage": usage,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        if finish_reason == "length":
            print("提示：达到输出 token 上限，回复可能被截断。", file=sys.stderr)
