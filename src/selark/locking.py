"""Inter-process coordination; callers perform blocking work off the event loop."""

import os
from contextlib import contextmanager

from selark.config import ConfigError
from selark.platforms import native


@contextmanager
def file_lock(path, *, nonblocking=False):
    fd = native.open_private(path, os.O_RDWR | os.O_CREAT)
    try:
        try:
            native.lock(fd, nonblocking=nonblocking)
        except BlockingIOError:
            raise ConfigError("该配置的 Selark 服务已在运行。") from None
        yield
    finally:
        os.close(fd)
