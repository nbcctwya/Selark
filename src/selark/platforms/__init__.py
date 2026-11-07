"""OS capabilities only; configuration, protocols and accounting stay shared."""

import os

if os.name == "nt":
    from . import windows as native
else:
    from . import posix as native

__all__ = ["native"]
