"""Halting the fleet leaves a record naming who did it.

`/sql kill` writes `bot_config.kill_switch`, which stops every query on every
target on the next call. It wrote the row and then only `log.warning`, so the
most consequential toggle in the product left nothing an auditor can read:
journald rotates, `audit_log` does not. The web route beside it has always
written one, which is what made the gap visible -- the same action is audited
on one surface and not the other.

Three separate reviewers reported this independently, which is usually the
signal that a thing is really there.

The write and the audit row go in ONE transaction. A crash between them would
otherwise leave the fleet stopped with no record of who stopped it, which is
the exact moment somebody goes looking.
"""
import inspect

from queryhub.slack_app import subcommands


def _kill() -> str:
    return inspect.getsource(subcommands._handle_kill)


def test_the_toggle_writes_an_audit_row():
    src = _kill()
    assert 'audit.log_in(' in src
    assert '"kill_switch_set"' in src


def test_the_write_and_the_row_share_a_transaction():
    """Two statements, one commit. `db.execute` on its own cannot give that."""
    src = _kill()
    i = src.index("with db.transaction() as cur:")
    block = src[i:]
    assert "UPDATE bot_config" in block
    assert "audit.log_in(" in block
    assert "db.execute(" not in block


def test_the_row_records_what_it_changed_from():
    """"Somebody set it to on" is half an answer; an auditor reconstructing an
    outage needs to know it was off before, and which surface did it."""
    src = _kill()
    assert '"from": current' in src
    assert '"to": arg' in src
    assert '"via": "slack"' in src


def test_the_action_name_matches_the_web_route():
    """Both surfaces toggle the same switch. Two names for it means an
    auditor filtering `audit_log` sees half the history."""
    from queryhub.web import routes_admin
    assert '"kill_switch_set"' in inspect.getsource(routes_admin.set_kill)


def test_the_cache_is_dropped_after_the_commit_not_inside_it():
    """Invalidating inside the transaction would publish a value that a
    rollback then takes back, and the next call would read the old row while
    the cache says otherwise."""
    src = _kill()
    assert src.index("with db.transaction()") < src.index("cfg.invalidate_cache()")
