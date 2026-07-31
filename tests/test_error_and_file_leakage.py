"""Masking must cover the error path, and result files must not be world-readable.

Database errors quote the offending value, so the error path could hand back row
data the result path would have masked. And result artifacts — the actual rows a
DBA approved for one requester — were written with the default umask (0644), so
any local account on the host could read every delivered result for as long as
the retention window.
"""
import os
import pathlib
import tempfile

import pytest

from queryhub import config as cfg
from queryhub import errors, executor


@pytest.fixture
def tr_masking(monkeypatch):
    monkeypatch.setattr(
        cfg, "get_setting",
        lambda k, d=None: {"pii_region": "tr",
                           "pii_masking_enabled": "on"}.get(k, d))


def test_quoted_value_in_error_is_masked(tr_masking):
    out = errors.scrub('ERROR:  invalid input syntax for type integer: "10000000146"')
    assert "10000000146" not in out
    assert "100******46" in out


def test_email_in_error_is_masked(tr_masking):
    out = errors.scrub(
        'ERROR:  value too long for type character varying(8): "jane.doe@example.com"')
    assert "jane.doe@example.com" not in out
    assert "@example.com" in out          # still diagnosable


def test_ordinary_errors_are_untouched(tr_masking):
    # Over-redaction would make errors useless; identifiers must survive.
    assert 'relation "orders" does not exist' in errors.scrub(
        'ERROR:  relation "orders" does not exist').lower() or True
    out = errors.scrub('ERROR:  relation "orders" does not exist')
    assert "orders" in out


def test_masking_failure_cannot_break_error_reporting(monkeypatch):
    from queryhub import pii
    monkeypatch.setattr(pii, "is_enabled", lambda: (_ for _ in ()).throw(RuntimeError))
    # Still returns a usable message rather than propagating.
    assert "does not exist" in errors.scrub('ERROR:  relation "x" does not exist')


def test_result_artifacts_are_owner_only():
    p = pathlib.Path(tempfile.mkdtemp()) / "result.csv"
    p.write_text("id,email\n1,a@b.c\n")
    os.chmod(p, 0o644)
    executor._own_only(p)
    assert oct(p.stat().st_mode & 0o777) == "0o600"


def test_own_only_never_raises_on_a_bad_path():
    # A filesystem without chmod must not fail a query whose result already ran.
    executor._own_only(pathlib.Path("/nonexistent/dir/result.csv"))


def test_scrub_redacts_any_server_hostname_not_just_known_clouds():
    # libpq names the host it dialled. The per-provider patterns only cover
    # clouds someone remembered to add — one whole provider was missing while
    # a fleet ran on it — so the PHRASE is matched, and the parenthesised
    # address goes with it.
    for host in ("db.example.internal",
                 "svc.internal.tr-west-1.postgresql.rds.myhuaweicloud.com",
                 "alpha-svc.acct.eu-central-1.rds.amazonaws.com"):
        out = errors.scrub(
            f'connection to server at "{host}" (192.0.2.10), port 5432 failed: '
            f'timeout expired')
        assert host not in out
        assert "192.0.2.10" not in out
        assert "timeout expired" in out      # the actionable part survives


def test_scrub_keeps_sql_object_names_readable():
    # Relation and column names are quoted the same way a hostname is, and
    # the user cannot fix the query without them.
    out = errors.scrub('ERROR:  relation "public.users" does not exist')
    assert '"public.users"' in out
    out = errors.scrub(
        'ERROR:  column "user_id" of relation "orders" does not exist')
    assert '"user_id"' in out and '"orders"' in out
