"""
Tests for the run-outcome policy in post_to_supabase.

Only the pure parts: exit_code_for and _failure_threshold. The sync loop
itself talks to Supabase and Coinbase and isn't exercised here.
"""


import pytest

post_to_supabase = pytest.importorskip("post_to_supabase")

exit_code_for = post_to_supabase.exit_code_for


# ---------------------------------------------------------------------------
# exit_code_for
# ---------------------------------------------------------------------------

def test_clean_run_succeeds():
    assert exit_code_for(attempted=200, failed=0) == 0


def test_one_failure_out_of_many_still_succeeds():
    """
    The case this whole function exists for. One participant revoking their
    key must not make every subsequent run red - a permanently red job is one
    nobody looks at.
    """
    assert exit_code_for(attempted=200, failed=1) == 0


def test_total_failure_fails():
    """Wrong FERNET_KEY, Supabase down - nothing will fix itself."""
    assert exit_code_for(attempted=200, failed=200) == 1


def test_nothing_attempted_fails():
    """
    An empty participants table means registration is broken. Reporting
    success for having done nothing is how that goes unnoticed - it already
    happened once.
    """
    assert exit_code_for(attempted=0, failed=0) == 1


def test_zero_failures_succeeds_at_every_threshold():
    """A run with no failures is never an error, whatever the policy."""
    for threshold in (0.0, 0.25, 0.5, 1.0):
        assert exit_code_for(attempted=10, failed=0, threshold=threshold) == 0


def test_threshold_zero_fails_on_any_failure():
    assert exit_code_for(attempted=200, failed=1, threshold=0.0) == 1


@pytest.mark.parametrize("failed,expected", [
    (49, 0),    # under half
    (50, 1),    # exactly half - threshold is inclusive
    (51, 1),    # over half
])
def test_half_threshold_boundary(failed, expected):
    assert exit_code_for(attempted=100, failed=failed, threshold=0.5) == expected


def test_single_credential_failing_is_a_total_failure():
    """With one credential there is no partial - it worked or it didn't."""
    assert exit_code_for(attempted=1, failed=1) == 1
    assert exit_code_for(attempted=1, failed=0) == 0


def test_failed_never_exceeds_attempted_in_practice():
    """
    Guards the accounting change: `failed` counts CREDENTIALS, not steps. It
    used to count every failed step, and one credential can fail three of
    them - which would push the ratio above 1.0 and make the threshold
    meaningless.
    """
    assert exit_code_for(attempted=4, failed=4, threshold=1.0) == 1
    assert exit_code_for(attempted=4, failed=3, threshold=1.0) == 0


# ---------------------------------------------------------------------------
# _failure_threshold
# ---------------------------------------------------------------------------

@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv("SYNC_FAILURE_THRESHOLD", raising=False)
    return monkeypatch


def test_threshold_defaults_to_total_failure(clean_env):
    assert post_to_supabase._failure_threshold() == 1.0


def test_threshold_reads_the_environment(clean_env):
    clean_env.setenv("SYNC_FAILURE_THRESHOLD", "0.5")
    assert post_to_supabase._failure_threshold() == 0.5


@pytest.mark.parametrize("bad", ["half", "", "1.5", "-0.1", "abc"])
def test_unusable_threshold_falls_back_rather_than_raising(clean_env, bad):
    """
    A malformed alerting policy must not take down the sync. Getting the data
    in matters more than the exit code being exactly right.
    """
    clean_env.setenv("SYNC_FAILURE_THRESHOLD", bad)
    assert post_to_supabase._failure_threshold() == 1.0


# ---------------------------------------------------------------------------
# import_new_signups
#
# The website's form only inserts into pending_signups. Until a row is
# imported its credentials sit there in plaintext and the participant is not
# tracked, so every run imports first. It must never cost the sync anything.
# ---------------------------------------------------------------------------

def _fake_sign_ups(monkeypatch, import_pending_signups):
    import sys
    import types
    fake = types.ModuleType("sign_ups")
    fake.import_pending_signups = import_pending_signups
    monkeypatch.setitem(sys.modules, "sign_ups", fake)


def test_new_signups_are_imported_and_counted(monkeypatch):
    counts = {"pending": 2, "imported": 1, "rejected": 0, "deferred": 1}
    _fake_sign_ups(monkeypatch, lambda: counts)
    assert post_to_supabase.import_new_signups() == counts


def test_a_failing_import_never_reaches_the_sync(monkeypatch, caplog):
    def boom():
        raise RuntimeError("Supabase is down")
    _fake_sign_ups(monkeypatch, boom)

    assert post_to_supabase.import_new_signups() is None
    assert "stay pending for the next run" in caplog.text


def test_a_broken_sign_ups_module_never_reaches_the_sync(monkeypatch):
    """Imported inside the function, so even a module that cannot load is contained."""
    import sys
    monkeypatch.setitem(sys.modules, "sign_ups", None)     # makes `import` raise
    assert post_to_supabase.import_new_signups() is None


def test_signups_are_imported_before_the_sync_runs():
    """
    Order matters: imported first, a new participant's first snapshot is
    taken in this run instead of the next one. Read from the entrypoint
    itself, since it is a script block rather than a function.
    """
    import ast
    from pathlib import Path

    source = Path(post_to_supabase.__file__).read_text(encoding="utf-8")
    main = next(node for node in ast.parse(source).body
                if isinstance(node, ast.If) and "__main__" in ast.unparse(node.test))
    calls = [ast.unparse(n.func) for n in ast.walk(main) if isinstance(n, ast.Call)]
    assert calls.index("import_new_signups") < calls.index("sync_all_to_supabase")
