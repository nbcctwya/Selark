"""Process controls for isolated acceptance fixtures, never a user's service."""

import os
import signal

from selark.service import running_process


def crash_service(directory, data_dir):
    process = running_process(directory, data_dir)
    assert process is not None, "Isolated fixture has no owned process"
    if os.name != "nt":
        os.kill(process["pid"], signal.SIGKILL)
        return
    import win32api
    import win32con
    import win32event

    from selark.platforms import windows

    with windows.handle(
        win32api.OpenProcess(
            windows.PROCESS_QUERY_LIMITED_INFORMATION
            | win32con.SYNCHRONIZE
            | win32con.PROCESS_TERMINATE,
            False,
            process["pid"],
        )
    ) as value:
        identity = windows.identity_from_handle(value, process["pid"])
        assert identity and all(process.get(k) == v for k, v in identity.items())
        win32api.TerminateProcess(value, 1)
        assert win32event.WaitForSingleObject(value, 5000) == win32event.WAIT_OBJECT_0
