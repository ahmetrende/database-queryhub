"""Modular target policy: allow/deny globs over alias AND host.

All aliases/hosts here are synthetic — never put real target
aliases/hosts in tracked files (check_repo_clean forbids it)."""
import pytest

from dba_slack_bot import target_policy as tp


@pytest.fixture
def cfgkeys(monkeypatch):
    """Back the four policy keys with a mutable dict."""
    state = {k: "" for k in ("target_alias_allow_patterns",
                             "target_alias_deny_patterns",
                             "target_host_allow_patterns",
                             "target_host_deny_patterns")}
    monkeypatch.setattr(tp.cfg, "get_setting",
                        lambda key, default=None: state.get(key, default or ""))
    return state


def test_no_patterns_is_inert(cfgkeys):
    assert tp.is_enforced() is False
    assert tp.is_wanted("anything", "any.host") is True


def test_alias_allowlist(cfgkeys):
    cfgkeys["target_alias_allow_patterns"] = "alpha-*, beta-*"
    assert tp.is_enforced() is True
    assert tp.is_wanted("alpha-svc", "h.aws.example") is True
    assert tp.is_wanted("gamma-svc", "h.aws.example") is False


def test_host_allowlist_covers_a_whole_cloud(cfgkeys):
    # The scaling win: one host pattern allows every alias on that cloud.
    cfgkeys["target_alias_allow_patterns"] = "alpha-*"
    cfgkeys["target_host_allow_patterns"] = "*.cloudb.example"
    # alias doesn't match alpha-*, but host matches → wanted
    assert tp.is_wanted("randomservice", "svc.internal.cloudb.example") is True
    # neither alias nor host matches → not wanted
    assert tp.is_wanted("randomservice", "svc.clouda.example") is False


def test_deny_beats_allow_on_either_field(cfgkeys):
    cfgkeys["target_alias_allow_patterns"] = "alpha-*"
    cfgkeys["target_host_allow_patterns"] = "*.cloudb.example"
    cfgkeys["target_alias_deny_patterns"] = "*-test"
    cfgkeys["target_host_deny_patterns"] = "*.staging.cloudb.example"
    assert tp.is_wanted("alpha-svc", "h.clouda.example") is True
    assert tp.is_wanted("alpha-test", "h.clouda.example") is False        # alias-deny
    assert tp.is_wanted("svc", "db.staging.cloudb.example") is False      # host-deny
    assert tp.is_wanted("svc", "db.prod.cloudb.example") is True          # host-allow


def test_denylist_only_allows_rest(cfgkeys):
    cfgkeys["target_host_deny_patterns"] = "*.clouda.example"
    assert tp.is_enforced() is True
    assert tp.is_wanted("svc", "h.cloudb.example") is True
    assert tp.is_wanted("svc", "h.clouda.example") is False


@pytest.mark.parametrize("raw,n", [
    ("a-*, b-*", 2), ("a-*  b-*", 2), ("a-*\nb-*\n", 2), ("  a-*  ", 1), ("", 0)])
def test_pattern_parsing_separators(cfgkeys, raw, n):
    cfgkeys["target_host_allow_patterns"] = raw
    assert len(tp.patterns("host_allow")) == n
