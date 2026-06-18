"""Writer-connection self-healing (PostgresExecutionMemory._writer_conn).

The writer is one bare long-lived connection (not pooled), so a Postgres
restart or dropped TCP session used to fail every learning write until the app
restarted. The _writer_conn property must detect a dead connection and reopen
it transparently; these tests kill the connection and verify recovery.
"""

import pytest


class TestWriterReconnect:
    def test_healthy_connection_is_returned_as_is(self, in_memory_em):
        first = in_memory_em._writer_conn
        assert in_memory_em._writer_conn is first  # no churn on healthy conns

    def test_reopens_after_connection_closed(self, in_memory_em):
        dead = in_memory_em._writer
        dead.close()

        conn = in_memory_em._writer_conn  # property must reopen
        assert conn is not dead
        assert not conn.closed
        assert conn.execute("SELECT 1").fetchone()[0] == 1
        conn.rollback()  # leave no open transaction behind
        in_memory_em._writer.close()  # tidy: fixture teardown restores the old attr

    def test_write_succeeds_after_connection_killed(self, in_memory_em):
        # The autouse _rename_test_thread_to_writer fixture satisfies the
        # writer-thread assertion inside update_daily_stats.
        in_memory_em._writer.close()

        # First write after the outage goes through the reopened connection.
        in_memory_em.update_daily_stats("passed")

        with in_memory_em.read_conn() as conn:
            row = conn.execute(
                "SELECT total_executions, total_passed FROM learning_stats"
            ).fetchone()
        assert (row[0], row[1]) == (1, 1)
        in_memory_em._writer.close()
