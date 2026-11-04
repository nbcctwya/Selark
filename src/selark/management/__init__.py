"""Public entry points for local management commands.

Implementation is organized by parsing, connection setup, diagnostics and dispatch.
Existing ``selark.management`` imports remain valid.
"""

from selark.config import server_url as server_url
from selark.management.arguments import MANAGEMENT_COMMANDS as MANAGEMENT_COMMANDS
from selark.management.arguments import add_commands as add_commands
from selark.management.arguments import add_filters as add_filters
from selark.management.arguments import policy_file as policy_file
from selark.management.arguments import positive as positive
from selark.management.arguments import query_filters as query_filters
from selark.management.connections import (
    configure_connection_interactively as configure_connection_interactively,
)
from selark.management.connections import discover_connection as discover_connection
from selark.management.connections import probe as probe
from selark.management.connections import read_key as read_key
from selark.management.diagnostics import doctor as doctor
from selark.management.handlers import emit as emit
from selark.management.handlers import run as run
