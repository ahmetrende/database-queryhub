"""A new target is named after its instance, not its hostname.

On AWS the endpoint's first label IS the instance name. On Huawei it is the
instance ID (`<32 hex>in03.internal.<region>....`), so importing by hostname
gave 19 targets names nobody could read, while the inventory held the real
one in `v_server.db_instance_identifier`.
"""
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import import_targets_from_inventory as imp  # noqa: E402

HUAWEI = "0123456789abcdef0123456789abcdefin03.internal.example-1.postgresql.rds.example.com"
AWS = "prod-orders.abc123.example-1.rds.example.com"


def _server(endpoint, ident, deleted=False):
    return {"endpoint": endpoint, "db_instance_identifier": ident, "is_deleted": deleted}


def test_a_huawei_endpoint_takes_the_instance_name():
    servers = [_server(HUAWEI, "acme-prod-ledger-cloud-012345")]
    assert imp.name_for_endpoint(HUAWEI, servers) == "acme-prod-ledger-cloud-012345"


def test_an_aws_endpoint_names_the_same_either_way():
    assert imp.name_for_endpoint(AWS, [_server(AWS, "prod-orders")]) == "prod-orders"
    assert imp.name_for_endpoint(AWS, []) == "prod-orders"


def test_without_an_inventory_name_the_first_label_is_used():
    assert imp.name_for_endpoint(HUAWEI, []) == HUAWEI.split(".", 1)[0]


def test_a_deleted_instance_does_not_lend_its_name():
    servers = [_server(HUAWEI, "old-name", deleted=True)]
    assert imp.name_for_endpoint(HUAWEI, servers) == HUAWEI.split(".", 1)[0]


def test_a_name_that_is_not_a_valid_alias_is_not_used():
    for bad in ("has space", "-leading-dash", "", "x" * 64):
        assert imp.name_for_endpoint(HUAWEI, [_server(HUAWEI, bad)]) == HUAWEI.split(".", 1)[0]


def test_the_rds_import_step_names_through_it():
    src = (SCRIPTS / "import_targets_from_inventory.py").read_text(encoding="utf-8")
    assert "targets.claim_alias(name_for_endpoint(endpoint, servers)" in src
