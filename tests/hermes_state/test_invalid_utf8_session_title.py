"""Regression tests for corrupt legacy session titles."""

from hermes_state import SessionDB


def test_list_sessions_rich_tolerates_invalid_utf8_title(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        session_id = db.create_session("bad-title-session", "cli")
        assert db._conn is not None
        with db._lock:
            db._conn.execute(
                "UPDATE sessions SET title = CAST(X'0000008A' AS TEXT) WHERE id = ?",
                (session_id,),
            )

        sessions = db.list_sessions_rich(limit=10)
    finally:
        db.close()

    row = next(s for s in sessions if s["id"] == session_id)
    assert "\ufffd" in row["title"]
