"""Linux/WSL filesystem and process primitives."""

import fcntl
import os
import stat
import sys
import tempfile
from pathlib import Path

from selark.errors import ConfigError

IS_WINDOWS = False


def config_directory():
    root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config").expanduser()
    if not root.is_absolute():
        root = Path.home() / ".config"
    return root / "selark"


def data_directory():
    root = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share").expanduser()
    if not root.is_absolute():
        root = Path.home() / ".local/share"
    return root / "selark"


def reject_links(path):
    if Path(path).is_symlink():
        raise ConfigError("私有路径不能是符号链接。")


def protect(path, *, directory=False):
    reject_links(path)
    os.chmod(path, 0o700 if directory else 0o600)


def private_directory(path):
    reject_links(path)
    Path(path).mkdir(mode=0o700, parents=True, exist_ok=True)
    protect(path, directory=True)


def _check(info, *, directory=False):
    valid = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not valid or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ConfigError("私有路径所有者或权限不安全；目录应为 0700，文件请执行 chmod 600。")


def check_private(path, *, directory=False):
    reject_links(path)
    info = Path(path).lstat()
    _check(info, directory=directory)
    return info


def open_private(path, flags, mode=0o600):
    reject_links(path)
    fd = os.open(path, (flags & ~os.O_TRUNC) | os.O_NOFOLLOW, mode)
    try:
        _check(os.fstat(fd))
        if flags & os.O_TRUNC:
            os.ftruncate(fd, 0)
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_private(path, *, limit=None):
    with os.fdopen(open_private(path, os.O_RDONLY), "rb") as source:
        data = source.read() if limit is None else source.read(limit + 1)
    if limit is not None and len(data) > limit:
        raise ConfigError("私有文件超过大小限制。")
    return data


def temporary_file(parent):
    return tempfile.mkstemp(prefix=".selark-", dir=parent)


def replace(source, target):
    os.replace(source, target)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def lock(fd, *, nonblocking=False):
    fcntl.flock(fd, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))


def process_identity(pid):
    try:
        process = Path(f"/proc/{pid}")
        if process.stat().st_uid != os.getuid():
            return None
        fields = (process / "stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return {
            "pid": pid,
            "start_ticks": fields[19],
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        }
    except (OSError, IndexError):
        return None


def detached_options(directory):
    return {"cwd": "/", "start_new_session": True, "umask": 0o077}


def python_command(arguments):
    return [os.path.abspath(sys.executable), *arguments], {}
