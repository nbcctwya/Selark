"""Frozen CLI entry point, also used by the desktop's owned session."""

import sys
from multiprocessing import freeze_support

from selark.cli import main

if __name__ == "__main__":
    freeze_support()
    if len(sys.argv) == 4 and sys.argv[1] == "--windows-login":
        from selark.platforms.windows_start import main as login_main

        sys.argv.pop(1)
        raise SystemExit(login_main())
    raise SystemExit(main())
