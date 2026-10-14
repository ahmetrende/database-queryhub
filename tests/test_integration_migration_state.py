"""The pending-migration check against a real ledger.

CI applies every migration before the integration suite, so the first pass
(one login that owns everything) must find nothing pending. The second pass
runs as the split runtime login, which cannot read the ledger: the check must
say "unknown", not raise, and leave the pooled connection usable.
"""
import os

import pytest

from queryhub import db, migration_state

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("QH_RUN_INTEGRATION"),
        reason="set QH_RUN_INTEGRATION=1 (with a reachable control DB) to run"),
]


def test_the_check_reads_the_real_ledger():
    db.init_pool()
    shipped = migration_state.shipped_versions()
    assert shipped, "the checkout has no migrations directory"
    if os.environ.get("BOT_DB_MIGRATOR_USER"):
        # Split roles: the runtime login has no access to schema_migrations,
        # and the catalog says so (False, not an error).
        assert migration_state.ledger_readable() is False
        assert migration_state.applied_versions() is None
        assert migration_state.pending(shipped) is None
    else:
        assert migration_state.ledger_readable() is True
        assert migration_state.pending(shipped) == []
    # The catalog read ran no failing statement: the next query still works.
    assert db.fetch_one("SELECT 1 AS ok")["ok"] == 1
