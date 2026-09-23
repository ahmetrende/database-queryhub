"""Deleting a connection under the new access model.

The gate counted only the legacy grant tables. `access_grant.target_id` and
`role_assignment.scope_target_id` are RESTRICT, so a connection whose access
lived only in the new model passed the gate and died on a constraint error --
a 500 where the admin should have read "disabled instead". And the catalog
delete cleared `schema_tables` but not `schema_functions`, whose key is the
same kind, so a connection snapshotted with routines could not be deleted.
"""
import inspect

from queryhub import targets
from queryhub.web import ops_metrics
from queryhub.web import routes_admin as ra


def test_the_gate_counts_the_new_models_restrict_references():
    src = inspect.getsource(targets.reference_counts)
    assert "FROM access_grant WHERE target_id = %s" in src
    assert "FROM role_assignment WHERE scope_target_id = %s" in src
    assert "(target_id,) * 9" in src


def test_the_gate_refuses_on_them():
    src = inspect.getsource(ra.admin_delete_connection)
    assert '"access_grants", "approver_roles"' in src


def test_the_function_catalog_goes_before_the_target():
    src = inspect.getsource(targets.delete_in)
    assert src.index("DELETE FROM schema_functions") < src.index("DELETE FROM target_servers")
    assert src.index("DELETE FROM schema_tables") < src.index("DELETE FROM target_servers")


def test_the_grant_metric_counts_both_models():
    src = inspect.getsource(ops_metrics._control_plane)
    assert src.count("FROM access_grant") == 2
    assert "FROM team_target_grants \"\n" in src or "FROM team_target_grants" in src
