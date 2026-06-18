"""
Authentication package (Phase 1: login/signup, JWT, roles, Google SSO).

Self-contained module that owns the auth/users tables in PostgreSQL. Public
surface:

- db.init_auth_db / db.close_pool — startup/shutdown hooks (wired in main.py)
- endpoints.auth_router            — the /auth router
- jwt_utils.require_user / require_admin / get_current_user — FastAPI deps used
  to guard endpoints (enforcement toggled by settings.AUTH_ENFORCED)
"""
