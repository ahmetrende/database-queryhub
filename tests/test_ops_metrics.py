"""GET /metrics — the gate, the format, and the failure behaviour.

There was no operational telemetry at all: nothing to scrape, so "is the queue
backing up" had no answer short of opening psql. web/metrics.py is PRODUCT
metrics for the admin dashboard, which is a different question.

What is worth testing here is not the arithmetic — the numbers come from SQL and
a test that mocks the SQL only checks the mock. It is:

  * the gate. This endpoint reports queue depth, fleet size and user counts, so
    the default must be closed, disabled must look like absent (404, not 403 —
    a 403 confirms it exists), and enabling it without setting a token must not
    publish it.
  * the exposition format, which is a protocol. A duplicated `# TYPE` line makes
    the whole payload invalid, and Prometheus rejects the scrape rather than
    partially accepting it.
  * partial failure. When the reason someone is scraping is that things are
    already broken, one failed collector must not turn the response into a 500.
"""
import logging

import pytest
from starlette.testclient import TestClient

from queryhub import config as cfg
from queryhub import db
from queryhub.web import app as web_app
from queryhub.web import ops_metrics


@pytest.fixture
def settings(monkeypatch):
    """bot_config values this test controls, on top of conftest's defaults."""
    values: dict[str, str] = {}

    def fake_get_setting(key, default=None):
        if key in values:
            return values[key]
        if default is None:
            raise KeyError(key)
        return default

    monkeypatch.setattr(cfg, "get_setting", fake_get_setting)
    return values


@pytest.fixture
def client(monkeypatch, settings):
    logging.disable(logging.CRITICAL)
    monkeypatch.setattr(db, "init_pool", lambda: None)
    from queryhub.slack_app import notifications
    monkeypatch.setattr(notifications, "dm_all_admins", lambda *a, **k: None)
    # A rendered payload that needs no database.
    monkeypatch.setattr(ops_metrics, "render",
                        lambda: "# HELP x t\n# TYPE x gauge\nx 1\n")
    with TestClient(web_app.create_app()) as c:
        yield c


# ------------------------------------------------------------------ the gate


def test_disabled_by_default_and_looks_absent(client):
    """No setting at all -> 404. Not 403: a 403 tells a prober the endpoint is
    there and only the credential is missing."""
    r = client.get("/metrics")
    assert r.status_code == 404


def test_explicitly_off_is_also_404(client, settings):
    settings["web_metrics_enabled"] = "off"
    assert client.get("/metrics").status_code == 404


def test_enabled_without_a_token_requires_a_session(client, settings):
    """The important half of the default. Someone flips the key on to try it,
    does not read as far as the token, and the endpoint must still not be open
    to anyone who can reach the port."""
    settings["web_metrics_enabled"] = "on"
    settings["web_metrics_token"] = ""
    r = client.get("/metrics")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthenticated"


def test_a_correct_bearer_token_is_accepted(client, settings):
    settings["web_metrics_enabled"] = "on"
    settings["web_metrics_token"] = "s3cret-token"
    r = client.get("/metrics", headers={"authorization": "Bearer s3cret-token"})
    assert r.status_code == 200
    assert r.text.startswith("# HELP")
    assert r.headers["content-type"].startswith("text/plain")


@pytest.mark.parametrize("header", [
    None,                          # no credential
    "Bearer wrong-token",          # wrong credential
    "Bearer ",                     # empty credential
    "s3cret-token",                # right value, missing the scheme
    "Basic s3cret-token",          # wrong scheme
    "Bearer s3cret-token-longer",  # correct value as a PREFIX
    "Bearer s3cret",               # correct value's prefix
])
def test_a_wrong_or_malformed_token_is_refused(client, settings, header):
    settings["web_metrics_enabled"] = "on"
    settings["web_metrics_token"] = "s3cret-token"
    headers = {"authorization": header} if header else {}
    assert client.get("/metrics", headers=headers).status_code == 401


def test_the_token_comparison_is_constant_time(settings):
    """A bearer credential compared with `==` leaks its length and its prefix
    through timing. Asserted structurally — a timing measurement in a unit test
    is flaky, but the presence of compare_digest is not."""
    import inspect
    from queryhub.web import app as app_mod
    src = inspect.getsource(app_mod.create_app)
    assert "compare_digest" in src, \
        "the metrics token must not be compared with =="


# ------------------------------------------------------- the exposition format


@pytest.fixture
def rendered(monkeypatch):
    """Render with every collector fed from stubs, so the format is exercised
    without a database."""
    monkeypatch.setattr(ops_metrics.db, "fetch_all", lambda sql, params=None: [
        {"status": "completed", "origin": "web", "tier": "ro", "n": 5,
         "oldest": 12, "enabled": True},
    ])
    monkeypatch.setattr(ops_metrics.db, "fetch_one", lambda sql, params=None: {
        "wait_sum": 100, "wait_count": 5, "exec_sum": 20, "exec_count": 5,
        "n": 7,
    })
    monkeypatch.setattr(ops_metrics.build_info, "build",
                        lambda: {"version": "r1", "sha": "abc1234"})
    return ops_metrics.render()


def test_every_metric_declares_type_exactly_once(rendered):
    """A repeated `# TYPE` for one metric name makes the payload invalid and
    Prometheus drops the whole scrape. Easy to hit, because samples for one
    metric can come from more than one query."""
    types = [ln.split()[2] for ln in rendered.splitlines()
             if ln.startswith("# TYPE ")]
    assert len(types) == len(set(types)), \
        f"duplicate TYPE declarations: {sorted({t for t in types if types.count(t) > 1})}"


def test_every_sample_line_is_well_formed(rendered):
    for line in rendered.splitlines():
        if line.startswith("#") or not line:
            continue
        name, _, value = line.rpartition(" ")
        assert name, f"no metric name in {line!r}"
        float(value)  # raises if the value is not a number


def test_help_and_type_precede_their_samples(rendered):
    """Order is part of the format's readability contract, and a sample before
    its TYPE is what an untyped (default 'untyped') metric looks like."""
    declared: set[str] = set()
    for line in rendered.splitlines():
        if line.startswith("# TYPE "):
            declared.add(line.split()[2])
        elif line and not line.startswith("#"):
            base = line.split("{")[0].split(" ")[0]
            assert base in declared, f"{base} sampled before its TYPE line"


def test_the_payload_ends_with_a_newline(rendered):
    assert rendered.endswith("\n")


def test_label_values_are_escaped(monkeypatch):
    """A target alias or status containing a quote would otherwise produce an
    unparseable line. Values come from the database, so this is not
    hypothetical — an alias is operator-supplied text."""
    monkeypatch.setattr(ops_metrics.db, "fetch_all", lambda sql, params=None: [
        {"status": 'we"ird', "origin": "back\\slash", "tier": "ro", "n": 1,
         "oldest": 0, "enabled": True}])
    monkeypatch.setattr(ops_metrics.db, "fetch_one", lambda sql, params=None: None)
    monkeypatch.setattr(ops_metrics.build_info, "build", lambda: {})
    out = ops_metrics.render()
    assert 'status="we\\"ird"' in out
    assert 'origin="back\\\\slash"' in out


# ------------------------------------------------------------ partial failure


def test_one_broken_collector_does_not_break_the_scrape(monkeypatch):
    """The moment this endpoint matters most is when something is already
    wrong. A 500 then is the least useful possible answer."""
    logging.disable(logging.CRITICAL)

    def boom(*a, **k):
        raise RuntimeError("relation does not exist")

    monkeypatch.setattr(ops_metrics.db, "fetch_all", boom)
    monkeypatch.setattr(ops_metrics.db, "fetch_one", boom)
    monkeypatch.setattr(ops_metrics.build_info, "build",
                        lambda: {"version": "r1", "sha": "abc"})

    out = ops_metrics.render()
    # Still a valid payload, and the build info collector still contributed.
    assert "queryhub_build_info" in out
    # ...and the failure is REPORTED, not hidden: a dashboard of zeros because
    # every query broke looks exactly like a healthy idle system.
    errors = [ln for ln in out.splitlines()
              if ln.startswith("queryhub_scrape_errors ")]
    assert errors, "no scrape-error metric emitted"
    assert float(errors[0].split()[1]) > 0


def test_queue_states_report_zero_rather_than_disappearing(monkeypatch):
    """An empty queue must still emit the series. A series that vanishes is
    indistinguishable from a broken exporter, and alerts on both `absent()` and
    `> 0` misfire when it does."""
    monkeypatch.setattr(ops_metrics.db, "fetch_all", lambda sql, params=None: [])
    monkeypatch.setattr(ops_metrics.db, "fetch_one", lambda sql, params=None: None)
    monkeypatch.setattr(ops_metrics.build_info, "build", lambda: {})
    out = ops_metrics.render()
    for state in ("pending", "approved", "executing", "scheduled"):
        assert f'queryhub_requests_in_state{{status="{state}"}} 0' in out
