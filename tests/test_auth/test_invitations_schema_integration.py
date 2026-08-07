import pytest
from src.backend.auth.db import get_pool
from src.backend.auth.invitations_db import init_invitations_db

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("auth_isolated_schema")]


def test_invitations_table_and_unique_open_index():
    init_invitations_db()
    # Both catalog queries filter on the current schema (as the sibling
    # test_status_schema_integration does). Matching on table name alone would
    # let an `invitations` table in ANY other schema satisfy the assertions, so
    # the test could pass without the isolated auth_test schema being migrated.
    with get_pool().connection() as conn:
        cols = {
            r["column_name"]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='invitations' AND table_schema = current_schema()"
            ).fetchall()
        }
    assert {"email", "org_id", "invited_by", "status", "token", "consumed_by"} <= cols
    with get_pool().connection() as conn:
        idx = {r["indexname"] for r in conn.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename='invitations' "
            "AND schemaname = current_schema()").fetchall()}
    assert "uq_open_invite" in idx
