"""HTTP-only Selark client. This module never imports a provider or reads server.json."""

import json
import sys
import time

import httpx

from selark.config import ClientConfig
from selark.sse import SSEDecoder


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

    def check(self, response: httpx.Response) -> None:
        if response.status_code == 200:
            return
        try:
            message = response.json()["error"]["message"]
            if not isinstance(message, str):
                raise ValueError
            message = message.replace(self.config.access_token.get_secret_value(), "[REDACTED]")
        except (ValueError, KeyError, TypeError):
            message = "Selark 返回了错误响应。"
        raise ClientError(f"HTTP {response.status_code}：{message}")

    def models(self) -> dict:
        response = self.http.get("/v1/models")
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
        if payload.get("stream"):
            done = False
            decoder = SSEDecoder()
            with self.http.stream("POST", "/v1/chat/completions", json=payload) as response:
                request_id = response.headers.get("x-request-id")
                if response.status_code != 200:
                    response.read()
                    self.check(response)
                if not response.headers.get("content-type", "").startswith("text/event-stream"):
                    raise ClientError("Selark 未返回 SSE 流。")
                try:
                    for line in response.iter_lines():
                        event = decoder.feed(line)
                        if event is None:
                            continue
                        if event == "[DONE]":
                            done = True
                            break
                        data = json.loads(event)
                        if "error" in data:
                            message = str(data["error"].get("message", "流式请求失败"))
                            raise ClientError(
                                message.replace(
                                    self.config.access_token.get_secret_value(), "[REDACTED]"
                                )
                            )
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
                    raise ClientError("Selark SSE 数据无效；输出可能不完整。") from None
            print()
            if not done or finish_reason is None:
                raise ClientError("Selark 流提前结束；输出可能不完整。")
        else:
            response = self.http.post("/v1/chat/completions", json=payload)
            request_id = response.headers.get("x-request-id")
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
