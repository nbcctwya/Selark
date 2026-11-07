"""Native Windows primitives: protected ACLs, handle locks and durable writes.

No chmod emulation or polling fallback. Unsupported filesystems fail explicitly.
Only this platform imports pywin32; Linux installations do not depend on it.
"""

import contextlib
import ctypes
import errno
import msvcrt
import os
import stat
import subprocess
import sys
from ctypes import wintypes
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

import ntsecuritycon
import pywintypes
import win32api
import win32con
import win32event
import win32file
import win32process
import win32security

from selark.errors import ConfigError

IS_WINDOWS = True
SHARE_ALL = win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE | win32con.FILE_SHARE_DELETE
OPEN_REPARSE_POINT = 0x00200000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class _RenameInfo(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("RootDirectory", wintypes.HANDLE),
        ("FileNameLength", wintypes.DWORD),
        ("FileName", wintypes.WCHAR * 1),
    ]


_rename = ctypes.WinDLL("kernel32", use_last_error=True).SetFileInformationByHandle
_rename.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
_rename.restype = wintypes.BOOL


@contextlib.contextmanager
def handle(value):
    try:
        yield value
    finally:
        value.Close()


@contextlib.contextmanager
def file_errors():
    """Expose ordinary OSError subclasses to the shared recovery/CLI code."""
    try:
        yield
    except pywintypes.error as error:
        code = error.winerror
        if code in (2, 3):
            raise FileNotFoundError(errno.ENOENT, "Private path does not exist") from None
        if code in (80, 183):
            raise FileExistsError(errno.EEXIST, "Private path already exists") from None
        if code in (5, 32):
            raise PermissionError(errno.EACCES, "Private path access denied or in use") from None
        raise OSError(code, "Windows filesystem operation failed") from None


@lru_cache(maxsize=1)
def user_sid():
    with handle(
        win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    ) as token:
        return win32security.GetTokenInformation(token, win32security.TokenUser)[0]


def security_attributes(*, directory=False):
    acl = win32security.ACL()
    inherit = win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE if directory else 0
    for sid in (
        user_sid(),
        win32security.CreateWellKnownSid(win32security.WinLocalSystemSid),
        win32security.CreateWellKnownSid(win32security.WinBuiltinAdministratorsSid),
    ):
        acl.AddAccessAllowedAceEx(win32security.ACL_REVISION, inherit, win32con.GENERIC_ALL, sid)
    descriptor = win32security.SECURITY_DESCRIPTOR()
    descriptor.SetSecurityDescriptorOwner(user_sid(), False)
    descriptor.SetSecurityDescriptorDacl(True, acl, False)
    descriptor.SetSecurityDescriptorControl(
        win32security.SE_DACL_PROTECTED, win32security.SE_DACL_PROTECTED
    )
    attributes = pywintypes.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = descriptor
    attributes.bInheritHandle = False
    return attributes


def config_directory():
    root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local").expanduser()
    if not root.is_absolute():
        root = Path.home() / "AppData/Local"
    return root / "Selark" / "config"


def data_directory():
    return config_directory().parent / "data"


def reject_links(path):
    """Reject junctions too, including parent components and UNC/device paths."""
    path = Path(path).absolute()
    if str(path).startswith("\\\\") or any(":" in part for part in path.parts[1:]):
        raise ConfigError("Windows 私有数据须使用本机磁盘路径，不能使用共享路径或数据流。")
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ConfigError("私有路径不能是符号链接、联接或重解析点。")


def _open_handle(
    path, access, *, creation=win32con.OPEN_EXISTING, directory=False, attributes=None
):
    reject_links(path)
    return win32file.CreateFile(
        str(path),
        access,
        SHARE_ALL,
        attributes,
        creation,
        OPEN_REPARSE_POINT | (win32con.FILE_FLAG_BACKUP_SEMANTICS if directory else 0),
        None,
    )


def _check_handle(value, *, directory=False):
    info = win32file.GetFileInformationByHandle(value)
    attrs = info[0]
    if (
        attrs & win32con.FILE_ATTRIBUTE_REPARSE_POINT
        or bool(attrs & win32con.FILE_ATTRIBUTE_DIRECTORY) != directory
    ):
        raise ConfigError("私有路径类型无效，不能是符号链接或重解析点。")
    if not directory and info[7] != 1:
        raise ConfigError("私有文件不能具有硬链接。")
    descriptor = win32security.GetSecurityInfo(
        value,
        win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION,
    )
    trusted = {
        win32security.ConvertSidToStringSid(user_sid()),
        "S-1-5-18",  # SYSTEM
        "S-1-5-32-544",  # Local administrators, equivalent to the POSIX root boundary.
    }
    owner = win32security.ConvertSidToStringSid(descriptor.GetSecurityDescriptorOwner())
    acl = descriptor.GetSecurityDescriptorDacl()
    if owner not in trusted or acl is None or not acl.GetAceCount():
        raise ConfigError("私有路径所有者或 ACL 不安全。")
    for index in range(acl.GetAceCount()):
        ace = acl.GetAce(index)
        kind = ace[0][0]
        # Our ACLs contain ordinary allow entries. Reject unfamiliar allow/object/callback
        # entries rather than accidentally accepting access granted to another principal.
        if (
            kind != win32security.ACCESS_ALLOWED_ACE_TYPE
            or win32security.ConvertSidToStringSid(ace[-1]) not in trusted
        ):
            raise ConfigError("私有路径 ACL 权限过宽；仅允许当前用户、SYSTEM 和管理员访问。")


def protect(path, *, directory=False):
    attributes = security_attributes(directory=directory)
    with (
        file_errors(),
        handle(
            _open_handle(path, win32con.READ_CONTROL | win32con.WRITE_DAC, directory=directory)
        ) as value,
    ):
        info = win32file.GetFileInformationByHandle(value)
        if (
            info[0] & win32con.FILE_ATTRIBUTE_REPARSE_POINT
            or bool(info[0] & win32con.FILE_ATTRIBUTE_DIRECTORY) != directory
            or (not directory and info[7] != 1)
        ):
            raise ConfigError("不能修改链接或重解析点的权限。")
        descriptor = win32security.GetSecurityInfo(
            value, win32security.SE_FILE_OBJECT, win32security.OWNER_SECURITY_INFORMATION
        )
        owner = win32security.ConvertSidToStringSid(descriptor.GetSecurityDescriptorOwner())
        if owner not in {
            win32security.ConvertSidToStringSid(user_sid()),
            "S-1-5-18",
            "S-1-5-32-544",
        }:
            raise ConfigError("私有路径不属于当前用户，拒绝修改权限。")
        win32security.SetSecurityInfo(
            value,
            win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION
            | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            attributes.SECURITY_DESCRIPTOR.GetSecurityDescriptorDacl(),
            None,
        )
        _check_handle(value, directory=directory)


def private_directory(path):
    path = Path(path)
    reject_links(path)
    if not path.parent.exists():
        private_directory(path.parent)
    try:
        with file_errors():
            win32file.CreateDirectory(str(path), security_attributes(directory=True))
    except FileExistsError:
        protect(path, directory=True)


def check_private(path, *, directory=False):
    with (
        file_errors(),
        handle(
            _open_handle(
                path,
                win32con.READ_CONTROL | ntsecuritycon.FILE_READ_ATTRIBUTES,
                directory=directory,
            )
        ) as value,
    ):
        _check_handle(value, directory=directory)
        return Path(path).stat()


def open_private(path, flags, mode=0o600):
    access = win32con.READ_CONTROL | win32con.GENERIC_READ
    if flags & (os.O_WRONLY | os.O_RDWR):
        access |= win32con.GENERIC_WRITE
    creation = win32con.OPEN_EXISTING
    if flags & os.O_CREAT:
        creation = win32con.CREATE_NEW if flags & os.O_EXCL else win32con.OPEN_ALWAYS
    with file_errors():
        value = _open_handle(path, access, creation=creation, attributes=security_attributes())
        try:
            _check_handle(value)
            fd = msvcrt.open_osfhandle(int(value), flags | os.O_BINARY)
            value.Detach()  # The CRT descriptor now owns the native handle.
        except BaseException:
            value.Close()
            raise
    try:
        if flags & os.O_TRUNC:
            os.ftruncate(fd, 0)
        if flags & os.O_APPEND:
            os.lseek(fd, 0, os.SEEK_END)
    except BaseException:
        os.close(fd)
        raise
    return fd


def read_private(path, *, limit=None):
    with os.fdopen(open_private(path, os.O_RDONLY), "rb") as source:
        data = source.read() if limit is None else source.read(limit + 1)
    if limit is not None and len(data) > limit:
        raise ConfigError("私有文件超过大小限制。")
    return data


def temporary_file(parent):
    name = Path(parent) / (".selark-" + uuid4().hex)
    return open_private(name, os.O_RDWR | os.O_CREAT | os.O_EXCL), str(name)


def replace(source, target):
    reject_links(source)
    reject_links(target)
    # Windows 10/11 FileRenameInfoEx gives atomic replacement even while another
    # reader holds the old inode. MoveFileEx fails in that case despite SHARE_DELETE.
    encoded = str(Path(target).absolute()).encode("utf-16-le")
    # FileName is NUL terminated; FileNameLength excludes the terminator.
    size = _RenameInfo.FileName.offset + len(encoded) + 2
    buffer = ctypes.create_string_buffer(size)
    info = ctypes.cast(buffer, ctypes.POINTER(_RenameInfo)).contents
    info.Flags = 0x1 | 0x2  # REPLACE_IF_EXISTS | POSIX_SEMANTICS; never ignore permissions.
    info.RootDirectory = None
    info.FileNameLength = len(encoded)
    ctypes.memmove(ctypes.addressof(buffer) + _RenameInfo.FileName.offset, encoded, len(encoded))
    with (
        file_errors(),
        handle(_open_handle(source, win32con.DELETE | win32con.READ_CONTROL)) as value,
    ):
        _check_handle(value)
        if not _rename(int(value), 22, buffer, size):  # FileRenameInfoEx
            raise ctypes.WinError(ctypes.get_last_error())


def sync_directory(path):
    # FlushFileBuffers requires a writable directory handle. A read-only handle gives
    # ERROR_ACCESS_DENIED. Never hide failure or silently weaken receipt durability.
    with (
        file_errors(),
        handle(
            _open_handle(path, win32con.GENERIC_READ | win32con.GENERIC_WRITE, directory=True)
        ) as value,
    ):
        win32file.FlushFileBuffers(value)


def lock(fd, *, nonblocking=False):
    flags = win32con.LOCKFILE_EXCLUSIVE_LOCK
    if nonblocking:
        flags |= win32con.LOCKFILE_FAIL_IMMEDIATELY
    try:
        win32file.LockFileEx(msvcrt.get_osfhandle(fd), flags, 0, 1, pywintypes.OVERLAPPED())
    except pywintypes.error as error:
        if error.winerror == 33:
            raise BlockingIOError(errno.EAGAIN, "Private file is locked") from None
        raise OSError(error.winerror, "Windows file lock failed") from None


def identity_from_handle(value, pid):
    if win32event.WaitForSingleObject(value, 0) != win32con.WAIT_TIMEOUT:
        return None
    with handle(win32security.OpenProcessToken(value, win32con.TOKEN_QUERY)) as token:
        owner = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        if owner != user_sid():
            return None
    created = win32process.GetProcessTimes(value)["CreationTime"]
    return {"pid": pid, "start_ticks": created.isoformat(), "boot_id": "windows"}


def process_identity(pid):
    try:
        with handle(
            win32api.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION | win32con.SYNCHRONIZE, False, pid
            )
        ) as value:
            return identity_from_handle(value, pid)
    except pywintypes.error:
        return None


def detached_options(directory):
    return {
        "cwd": str(directory),
        "creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
    }


def python_command(arguments):
    executable = os.path.abspath(sys.executable)
    environment = {}
    if not getattr(sys, "frozen", False) and sys.prefix != sys.base_prefix:
        # Use CPython multiprocessing's launcher strategy. The venv redirector is a
        # second waiting process with a different PID and complicates owned shutdown.
        executable = sys._base_executable
        environment["__PYVENV_LAUNCHER__"] = sys.executable
    return [executable, *arguments], environment
