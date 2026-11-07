"""Windowless login entry point; the registry contains paths, never credentials."""

import os
import sys
from pathlib import Path


def main():
    if os.name != "nt" or len(sys.argv) != 3:
        return 1
    directory, data_dir = map(Path, sys.argv[1:])
    if not directory.is_absolute() or not data_dir.is_absolute():
        return 1
    os.environ["SELARK_DATA_DIR"] = str(data_dir)
    from selark.service import manage_service

    manage_service(directory, "start", backend="process")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
