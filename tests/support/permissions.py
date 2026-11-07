"""Assert actual platform permissions, and deliberately widen them in negative tests."""

import os
import stat


def is_private(path, *, directory=False):
    if os.name != "nt":
        return stat.S_IMODE(path.stat().st_mode) == (0o700 if directory else 0o600)
    import win32api
    import win32con
    import win32security

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        user = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()
    allowed = {win32security.ConvertSidToStringSid(user), "S-1-5-18", "S-1-5-32-544"}
    descriptor = win32security.GetFileSecurity(str(path), win32security.DACL_SECURITY_INFORMATION)
    acl = descriptor.GetSecurityDescriptorDacl()
    if acl is None or acl.GetAceCount() == 0:
        return False
    return all(
        acl.GetAce(index)[0][0] == win32security.ACCESS_ALLOWED_ACE_TYPE
        and win32security.ConvertSidToStringSid(acl.GetAce(index)[-1]) in allowed
        for index in range(acl.GetAceCount())
    )


def make_private(path):
    from selark.platforms import native

    native.protect(path)


def make_public(path):
    if os.name != "nt":
        path.chmod(0o644)
        return
    import win32con
    import win32security

    descriptor = win32security.GetFileSecurity(str(path), win32security.DACL_SECURITY_INFORMATION)
    acl = descriptor.GetSecurityDescriptorDacl()
    acl.AddAccessAllowedAce(
        win32security.ACL_REVISION,
        win32con.GENERIC_READ,
        win32security.CreateWellKnownSid(win32security.WinWorldSid),
    )
    win32security.SetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION,
        None,
        None,
        acl,
        None,
    )
