"""Report email writes change who receives spend data, so only admins may make them."""

from backend.routers import users_groups
from backend.routers.user import require_admin

WRITES = {
    ("POST", "/report-config/weekly-report"),
    ("DELETE", "/report-config/weekly-report/{email_or_id}"),
    ("POST", "/report-config/user-alert"),
    ("DELETE", "/report-config/user-alert/{email_or_id}"),
    ("POST", "/send-test-report"),
}


def test_report_writes_require_admin():
    gated = {
        (method, route.path)
        for route in users_groups.router.routes
        for method in getattr(route, "methods", set())
        if any(dep.call is require_admin for dep in route.dependant.dependencies)
    }
    assert WRITES <= gated
