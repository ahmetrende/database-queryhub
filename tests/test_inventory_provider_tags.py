"""The inventory importer labels where a target runs.

It never wrote `tags`, so the provider label on a connection existed only where
someone had set it by hand or by a one-off pass. Every endpoint discovered after
that -- 11 of them on 2026-09-23, 5 on Huawei -- arrived with no provider and the
connection screen filed it under "Untagged". The inventory already knows the
answer: `v_server.cloud_provider`.
"""
import importlib.util
import inspect
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "import_targets_from_inventory",
    Path(__file__).resolve().parent.parent / "scripts" / "import_targets_from_inventory.py")
_mod = importlib.util.module_from_spec(_spec)
sys.modules["import_targets_from_inventory"] = _mod
_spec.loader.exec_module(_mod)

provider_tags = _mod.provider_tags
provider_by_endpoint = _mod.provider_by_endpoint
plan_provider_fills = _mod.plan_provider_fills


def _srv(ep, cp, deleted=False):
    return {"endpoint": ep, "cloud_provider": cp, "is_deleted": deleted}


# --- what an inventory row implies ------------------------------------------

def test_the_two_clouds_the_screen_knows_are_labelled():
    assert provider_tags("aws") == {"provider": "aws", "service": "RDS"}
    assert provider_tags("HUAWEI") == {"provider": "huawei", "service": "RDS"}


def test_an_unknown_provider_stays_an_honest_gap():
    assert provider_tags("clickhouse") == {}
    assert provider_tags(None) == {}
    assert provider_tags("") == {}


# --- which inventory row speaks for an endpoint ------------------------------

def test_a_live_instance_wins_over_a_deleted_one_at_the_same_endpoint():
    got = provider_by_endpoint([_srv("db1.example", "huawei", deleted=True),
                                _srv("db1.example", "aws")])
    assert got == {"db1.example": "aws"}


def test_a_deleted_instance_still_labels_an_endpoint_nothing_live_holds():
    assert provider_by_endpoint([_srv("gone.example", "huawei", deleted=True)]) == \
        {"gone.example": "huawei"}


def test_rows_without_an_endpoint_or_a_provider_are_skipped():
    assert provider_by_endpoint([_srv(None, "aws"), _srv("x.example", None)]) == {}


# --- what gets written -------------------------------------------------------

def test_an_untagged_target_gets_provider_and_service():
    plan = plan_provider_fills([{"id": 1, "host": "a.example", "tags": {}}],
                               {"a.example": "huawei"})
    assert plan == [(1, {"provider": "huawei", "service": "RDS"})]


def test_a_provider_someone_set_is_never_overwritten():
    plan = plan_provider_fills(
        [{"id": 1, "host": "a.example", "tags": {"provider": "onprem"}}],
        {"a.example": "aws"})
    assert plan == []


def test_only_absent_keys_are_added_and_the_rest_of_the_bag_is_kept():
    plan = plan_provider_fills(
        [{"id": 1, "host": "a.example", "tags": {"account": "000000000000", "service": "Aurora"}}],
        {"a.example": "aws"})
    assert plan == [(1, {"provider": "aws"})]


def test_a_target_outside_inventory_is_left_alone():
    assert plan_provider_fills([{"id": 1, "host": "b.example", "tags": {}}],
                               {"a.example": "aws"}) == []


# --- the statements ----------------------------------------------------------

_MAIN = inspect.getsource(_mod.main)


def test_a_new_target_is_inserted_with_its_bag():
    assert "password_encrypted, enabled, notes, tags) " in _MAIN
    assert "json.dumps(provider_tags(by_endpoint.get(endpoint)))" in _MAIN


def test_the_fill_merges_and_cannot_overwrite_a_provider():
    assert "|| %s::jsonb WHERE id = %s AND NOT (COALESCE(tags, '{}'::jsonb) ? 'provider')" in _MAIN


def test_a_run_that_labels_anything_leaves_an_audit_row():
    assert "'target_tags_filled'" in _MAIN


def test_inventory_is_read_once_per_run():
    assert _MAIN.count("_inventory_servers()") == 1
