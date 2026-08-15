"""X-Forwarded-For is client-spoofable without a trusted proxy, so
the login throttle and audit trail must key on the real peer address unless
web_trusted_proxy is explicitly enabled."""
from dba_slack_bot import config as cfg
from dba_slack_bot.web import deps


def _req(xff=None, peer="127.0.0.1"):
    headers = {}
    if xff is not None:
        headers["x-forwarded-for"] = xff
    client = type("C", (), {"host": peer})() if peer else None
    return type("R", (), {"headers": headers, "client": client})()


def test_xff_ignored_by_default():
    # web_trusted_proxy defaults off -> ignore the spoofable header.
    assert deps.client_ip(_req(xff="9.9.9.9", peer="127.0.0.1")) == "127.0.0.1"


def test_xff_trusted_only_when_proxy_configured(monkeypatch):
    monkeypatch.setattr(cfg, "get_setting",
                        lambda k, d=None: "on" if k == "web_trusted_proxy" else d)
    monkeypatch.setattr(cfg, "get_int", lambda k, d=None: 1)
    # The RIGHTMOST hop wins, not the first. This test used to assert
    # "9.9.9.9" — it encoded the bug: nginx appends the real peer, so the
    # leftmost entry is whatever the client chose to send.
    assert deps.client_ip(_req(xff="9.9.9.9, 1.1.1.1")) == "1.1.1.1"


def test_peer_used_when_no_client():
    assert deps.client_ip(_req(xff=None, peer=None)) is None


def test_target_ssl_defaults_to_require():
    # Unchanged behavior: sslmode=require, no rootcert, until opted in.
    assert cfg.target_ssl_kwargs() == {"sslmode": "require"}


def test_target_ssl_verify_full_opt_in(monkeypatch):
    vals = {"target_ssl_mode": "verify-full", "target_ssl_rootcert": "/ca.pem"}
    monkeypatch.setattr(cfg, "get_setting", lambda k, d=None: vals.get(k, d))
    assert cfg.target_ssl_kwargs() == {
        "sslmode": "verify-full", "sslrootcert": "/ca.pem"}


# ---------------------------------------------------------------------------
# Which hop to read. Taking the leftmost entry meant reading the attacker's own
# string: nginx's standard proxy_add_x_forwarded_for APPENDS the real peer to
# whatever the client sent, so a spoofed header arrives as "9.9.9.9, <real-ip>"
# and [0] is "9.9.9.9". The throttle then counts per attacker-chosen key and
# audit_log records a forged address.
# ---------------------------------------------------------------------------

def _proxied(monkeypatch, hops=1):
    monkeypatch.setattr(cfg, "get_setting",
                        lambda k, d=None: "on" if k == "web_trusted_proxy" else d)
    monkeypatch.setattr(cfg, "get_int", lambda k, d=None: hops)


def test_spoofed_leftmost_hop_is_ignored(monkeypatch):
    """The exact attack: the client sets the header, nginx appends the truth."""
    _proxied(monkeypatch)
    assert deps.client_ip(
        _req(xff="9.9.9.9, 203.0.113.7", peer="198.51.100.1")) == "203.0.113.7"


def test_two_proxies_reads_two_from_the_right(monkeypatch):
    _proxied(monkeypatch, hops=2)
    assert deps.client_ip(
        _req(xff="9.9.9.9, 203.0.113.7, 198.51.100.9", peer="198.51.100.1")) == "203.0.113.7"


def test_single_entry_header_is_still_used(monkeypatch):
    """One proxy, an honest client: the only entry IS the client."""
    _proxied(monkeypatch)
    assert deps.client_ip(_req(xff="203.0.113.7", peer="198.51.100.1")) == "203.0.113.7"


def test_chain_shorter_than_configured_hops_degrades_instead_of_erroring(monkeypatch):
    _proxied(monkeypatch, hops=5)
    assert deps.client_ip(_req(xff="203.0.113.7", peer="198.51.100.1")) == "203.0.113.7"


def test_hop_count_out_of_range_falls_back_to_one(monkeypatch):
    for bad in (0, -3, 99):
        _proxied(monkeypatch, hops=bad)
        assert deps.client_ip(
            _req(xff="9.9.9.9, 203.0.113.7", peer="198.51.100.1")) == "203.0.113.7", bad


def test_header_of_only_commas_falls_back_to_the_peer(monkeypatch):
    _proxied(monkeypatch)
    assert deps.client_ip(_req(xff=" , , ", peer="198.51.100.1")) == "198.51.100.1"
