"""Bounded private logs; never collect request bodies, headers or credential files."""

import logging
import os
from collections import deque
from logging.handlers import RotatingFileHandler

from selark.config import ConfigError, private_directory


class PrivateRotatingHandler(RotatingFileHandler):
    def _open(self):
        fd = os.open(
            self.baseFilename, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            os.close(fd)
            raise ConfigError("日志文件所有者或权限不安全；请检查日志目录。")
        return os.fdopen(fd, "a", encoding="utf-8")


def configure_logging(data_dir, settings, *, background=False):
    directory = data_dir / "logs"
    private_directory(directory)
    for path in directory.glob("selark.log*"):
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_uid != os.getuid()
            or path.stat().st_mode & 0o077
        ):
            raise ConfigError("日志目录含不安全文件；请检查权限及符号链接。")
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
    if directory.is_symlink() or path.is_symlink():
        raise ConfigError("日志路径不能是符号链接。")
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ConfigError("日志文件权限或所有者不安全。")
    # Tail a bounded byte window even if a previous version left an oversized file.
    with path.open("rb") as source:
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
