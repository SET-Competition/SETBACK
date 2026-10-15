"""
Tests for db.py, and for every read that now pages through it.

PostgREST caps a response at Supabase's max-rows and says nothing about it -
a normal 200 with fewer rows. Only metrics used to page; the sync, the
scorer and credential rotation each had reads that would have truncated
silently once their tables passed 1,000 rows. The rotation one is the reason
these tests exist: it would have re-encrypted the first 1,000 credentials,
verified the same 1,000, and declared the retired key safe to delete.
"""

import pytest
from cryptography.fernet import Fernet

db = pytest.importorskip("db")
metrics = pytest.importorskip("metrics")
post_to_supabase = pytest.importorskip("post_to_supabase")
rotate_credentials = pytest.importorskip("rotate_credentials")
score = pytest.importorskip("score")
venue_common = pytest.importorskip("venue_common")

# Supabase's default max-rows.
SERVER_CAP = 1000


class _PagedTable:
    """A query object that serves `rows` in slices, honouring a server cap."""

    def __init__(self, rows, server_cap=None):
        self._rows = rows
        self._cap = server_cap
        self.ranges = []
        self._range = (0, len(rows))

    # the chainable bits the real queries use
    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def range(self, start, end):
        self._range = (start, end)
        return self

    def execute(self):
        start, end = self._range
        self.ranges.append((start, end))
        size = end - start + 1
        if self._cap is not None:
            size = min(size, self._cap)
        return type("R", (), {"data": self._rows[start:start + size]})


def _rows(n):
    return [{"i": i} for i in range(n)]


# ---------------------------------------------------------------------------
# fetch_all
# ---------------------------------------------------------------------------

def test_a_single_page_is_read_in_one_request():
    table = _PagedTable(_rows(10))
    assert db.fetch_all(lambda: table, "test") == _rows(10)
    assert len(table.ranges) == 2      # the data page, then the empty one


def test_every_row_is_read_when_there_are_several_pages():
    table = _PagedTable(_rows(2500))
    assert db.fetch_all(lambda: table, "test") == _rows(2500)


def test_a_server_cap_below_the_page_size_still_reads_everything():
    """
    The subtle one. Stepping by the page size ASKED FOR rather than the rows
    actually returned would skip everything between the server's cap and the
    stride - reading a fraction of the table while looking like a full scan.
    """
    table = _PagedTable(_rows(2500), server_cap=400)
    assert db.fetch_all(lambda: table, "test") == _rows(2500)


def test_pages_do_not_overlap_or_leave_holes():
    table = _PagedTable(_rows(2500), server_cap=400)
    db.fetch_all(lambda: table, "test")

    starts = [start for start, _ in table.ranges]
    assert starts == sorted(starts)
    assert starts[:4] == [0, 400, 800, 1200]


def test_an_empty_table_reads_cleanly():
    table = _PagedTable([])
    assert db.fetch_all(lambda: table, "test") == []


def test_reading_stops_at_the_row_ceiling(monkeypatch):
    """A million rows for one participant is a bug, not a trader."""
    monkeypatch.setattr(db, "MAX_FETCH_ROWS", 2000)
    table = _PagedTable(_rows(100_000))
    assert len(db.fetch_all(lambda: table, "test")) == 2000


# ---------------------------------------------------------------------------
# One client, one environment check
# ---------------------------------------------------------------------------

def test_every_module_shares_one_client():
    """One sync run used to hold two clients: post_to_supabase's and metrics'."""
    shared = db.client()
    for module in (post_to_supabase, metrics, rotate_credentials, score):
        assert module.supabase is shared, module.__name__


def test_a_missing_variable_is_named(monkeypatch):
    monkeypatch.delenv("SETAPI_TEST_ONLY_VARIABLE", raising=False)
    with pytest.raises(RuntimeError, match="SETAPI_TEST_ONLY_VARIABLE.*the hint"):
        db.require_env("SETAPI_TEST_ONLY_VARIABLE", hint="the hint")


# ---------------------------------------------------------------------------
# The reads that used to truncate
# ---------------------------------------------------------------------------

def _serve(monkeypatch, rows):
    """Route every table() call to a capped, paged fake."""
    table = _PagedTable(rows, server_cap=SERVER_CAP)
    monkeypatch.setattr(db.client(), "table", lambda name: table)
    return table


def test_the_sync_reads_every_credential_past_the_server_cap(monkeypatch):
    """
    At three venues a participant this cap is ~330 participants. Everyone
    past it used to be skipped silently, with the run still green.
    """
    rows = [{"participant_id": f"p{i // 3}", "exchange": "coinbase",
             "account_type": "spot", "id": i, "api_key": "x"} for i in range(2500)]
    _serve(monkeypatch, rows)

    grouped = post_to_supabase.get_all_api_keys()

    assert sum(len(v) for v in grouped.values()) == 2500


def test_the_sync_reads_every_participant_past_the_server_cap(monkeypatch):
    _serve(monkeypatch, [{"id": f"p{i}"} for i in range(1500)])
    assert len(post_to_supabase.get_active_participants()) == 1500


def test_the_scorer_scores_every_participant_past_the_server_cap(monkeypatch):
    _serve(monkeypatch, [{"id": f"p{i}"} for i in range(1500)])
    rows = score.db.fetch_all(
        lambda: score.supabase.table("participants").select("id").order("id"),
        "participants")
    assert len(rows) == 1500


def test_rotation_verification_sees_rows_past_the_server_cap(monkeypatch, capsys):
    """
    The case that made this module necessary. 1,500 credentials, the last 500
    still on the old key. An unpaged read saw only the first 1,000, found them
    all current, and told the operator to delete the old key - which would
    have stranded the other 500 permanently.
    """
    current = Fernet(venue_common.os.environ["FERNET_KEY"].encode())
    old = Fernet(Fernet.generate_key())
    rows = [{"id": i,
             "api_key": (current if i < 1000 else old).encrypt(b"k").decode(),
             "api_secret": None, "api_passphrase": None}
            for i in range(1500)]
    _serve(monkeypatch, rows)

    rotate_credentials.verify_current_key_only()
    out = capsys.readouterr().out

    assert "500 value(s) NOT on the current key" in out
    assert "safe to remove FERNET_KEYS_RETIRED" not in out


class _DeleteRecorder:
    """Records a delete().not_.in_(...).execute() chain."""

    def __init__(self, returned):
        self.calls, self._returned = [], returned

    def delete(self):
        self.calls.append("delete")
        return self

    @property
    def not_(self):
        self.calls.append("not")
        return self

    def in_(self, column, values):
        self.calls.append(("in", column, tuple(values)))
        return self

    def select(self, *a, **k):
        self.calls.append("select")
        return self

    def execute(self):
        return type("R", (), {"data": self._returned})


def test_retired_periods_are_removed_in_one_request_with_no_read(monkeypatch):
    recorder = _DeleteRecorder(returned=[{"period": "hourly"}, {"period": "hourly"}])
    monkeypatch.setattr(db.client(), "table", lambda name: recorder)

    score._drop_retired_periods()

    assert recorder.calls == ["delete", "not", ("in", "period", score.PERIODS)]
    assert "select" not in recorder.calls
