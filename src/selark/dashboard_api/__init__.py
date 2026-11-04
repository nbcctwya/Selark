"""Local management API; served only inside the dashboard session boundary."""

from selark.dashboard_api.routes import management_router

__all__ = ["management_router"]
