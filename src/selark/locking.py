"""Linux inter-process coordination; callers perform blocking work off the event loop."""

import fcntl
import os
from contextlib import contextmanager

from selark.config import ConfigError


@contextmanager
def file_lock(path, *, nonblocking=False):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
        except BlockingIOError:
            raise ConfigError("该配置的 Selark 服务已在运行。") from None
        yield
    finally:
        os.close(fd)
