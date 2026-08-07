"""CI guard: every mutating route is auth-guarded (require_user/require_admin in
its dependency tree, by identity) OR explicitly allow-listed. A new unguarded,
non-allow-listed mutating endpoint fails this test."""

from fastapi import Depends, FastAPI
from fastapi.routing import APIRoute

from src.backend.auth.jwt_utils import require_user, require_admin
from src.backend.core import audit_log

_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}

# (1) Authentication / session endpoints — public by necessity, out of audit
# scope per spec §4 (login has no token yet; logout/-all are session events).
_AUTH_ALLOWLIST = {
    "/auth/register",
    "/auth/login",
    "/auth/logout",
    "/auth/logout-all",
    "/auth/forgot-password",
}
# (2) Machine endpoints — called service-to-service, attributed 'system'.
_MACHINE_ALLOWLIST = set(audit_log.MACHINE_SOURCES)
_ALLOWLIST = _AUTH_ALLOWLIST | _MACHINE_ALLOWLIST

_AUTH_DEPS = {require_user, require_admin}


def _walk_calls(dependant) -> set:
    """All dependency callables in a route's dependency tree (recursive)."""
    calls = set()
    if getattr(dependant, "call", None) is not None:
        calls.add(dependant.call)
    for sub in dependant.dependencies:
        calls |= _walk_calls(sub)
    return calls


def _is_attributable(route: APIRoute) -> bool:
    return bool(_walk_calls(route.dependant) & _AUTH_DEPS)


def _violations(app, allowlist) -> list[tuple[str, str]]:
    out = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        methods = route.methods & _MUTATING
        if not methods:
            continue
        if _is_attributable(route):
            continue
        if route.path in allowlist:
            continue
        for m in sorted(methods):
            out.append((m, route.path))
    return out


def test_real_app_has_no_unattributable_mutating_routes():
    from src.backend.main import app
    violations = _violations(app, _ALLOWLIST)
    assert violations == [], (
        "Mutating routes neither auth-guarded nor allow-listed: "
        f"{violations}. Add require_user/require_admin, or, if intentionally "
        "unauthenticated, add to the allow-list in this test."
    )


def test_param_level_depends_is_detected():
    """Proves a param-level Depends(require_user) is detected (not a false fail)."""
    app = FastAPI()

    @app.post("/thing")
    async def thing(user: dict | None = Depends(require_user)):
        return {}

    route = next(r for r in app.routes if isinstance(r, APIRoute) and r.path == "/thing")
    assert _is_attributable(route) is True
    assert _violations(app, set()) == []


def test_unguarded_route_fails():
    """An unguarded, non-allow-listed mutating route is flagged."""
    app = FastAPI()

    @app.post("/danger")
    async def danger():
        return {}

    assert _violations(app, set()) == [("POST", "/danger")]
