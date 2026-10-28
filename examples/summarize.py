#!/usr/bin/env python3
"""Independent standard-library program; no Selark package or provider key required."""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="通过 Selark 将文章总结为一句话")
    default_dir = Path(
        os.environ.get(
            "SELARK_CONFIG_DIR",
            str(Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "selark"),
        )
    )
    parser.add_argument("--config", type=Path, default=default_dir / "client.json")
    parser.add_argument("--input-file", type=Path, help="省略时读取 stdin")
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        if set(config) != {"url", "access_token", "model"}:
            raise ValueError("需要仅包含 url、access_token、model 的客户端配置。")
        if not all(isinstance(v, str) and v for v in config.values()):
            raise ValueError("客户端配置必须包含非空字符串。")
        url = urlsplit(config["url"])
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or (url.scheme == "http" and url.hostname not in {"127.0.0.1", "localhost", "::1"})
        ):
            raise ValueError("Selark 地址无效；非本机地址须使用 HTTPS。")
        article = (
            args.input_file.read_text(encoding="utf-8") if args.input_file else sys.stdin.read()
        )
        if not article.strip():
            raise ValueError("文章不能为空。")
        payload = {
            "model": config["model"],
            "messages": [
                {"role": "system", "content": "请将用户提供的文章总结为一句中文，不超过80个字。"},
                {"role": "user", "content": article},
            ],
            "max_completion_tokens": 128,
            "stream": False,
        }
        request = urllib.request.Request(
            config["url"].rstrip("/") + "/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": "Bearer " + config["access_token"],
                "Content-Type": "application/json",
            },
            method="POST",
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=660) as response:
            data = json.load(response)
        print(data["choices"][0]["message"]["content"])
        print(
            json.dumps(
                {"model": data.get("model"), "usage": data.get("usage")}, ensure_ascii=False
            ),
            file=sys.stderr,
        )
        return 0
    except urllib.error.HTTPError as exc:
        print(f"Selark 请求失败（HTTP {exc.code}），请检查令牌、模型或服务日志。", file=sys.stderr)
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        print("无法连接 Selark 或连接中断，请确认服务已启动且地址正确。", file=sys.stderr)
    except (ValueError, KeyError, IndexError, TypeError, OSError):
        print("输入、客户端配置或 Selark 响应无效，请检查文件和配置格式。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
