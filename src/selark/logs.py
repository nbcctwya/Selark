"""Bounded private logs; never collect request bodies, headers or credential files."""

import logging
import os
from collections import deque
from logging.handlers import RotatingFileHandler

from selark.config import ConfigError, private_directory
from selark.platforms import native


class PrivateRotatingHandler(RotatingFileHandler):
    def _open(self):
        fd = native.open_private(self.baseFilename, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
        return os.fdopen(fd, "a", encoding="utf-8")


def configure_logging(data_dir, settings, *, background=False):
    directory = data_dir / "logs"
    private_directory(directory)
    for path in directory.glob("selark.log*"):
        native.check_private(path)
    handler = PrivateRotatingHandler(
        directory / "selark.log",
        maxBytes=settings.max_bytes,
        backupCount=settings.backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if background:
        for old in root.handlers[:]:
            root.removeHandler(old)
            old.close()
    root.addHandler(handler)
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    return handler


def read_logs(data_dir, *, lines=50):
    if not 1 <= lines <= 1000:
        raise ConfigError("日志行数须在 1–1000 之间。")
    directory = data_dir / "logs"
    path = directory / "selark.log"
    if not path.exists() and not path.is_symlink():
        return {"path": str(path), "lines": [], "note": "尚无持久日志；启动服务后创建。"}
    native.reject_links(directory)
    # Tail a bounded byte window even if a previous version left an oversized file.
    with os.fdopen(native.open_private(path, os.O_RDONLY), "rb") as source:
        info = os.fstat(source.fileno())
        offset = max(0, info.st_size - 1024 * 1024)
        source.seek(offset)
        if offset:
            source.readline()
        recent = list(
            deque(
                source.read(1024 * 1024).decode("utf-8", errors="replace").splitlines(),
                maxlen=lines,
            )
        )
    return {"path": str(path), "lines": recent}
