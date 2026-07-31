"""Cloud-provider label derived from a target host (synthetic hosts only)."""
from queryhub import targets


def test_cloud_provider_from_host():
    assert targets.cloud_provider(
        "alpha-svc.acct.eu-central-1.rds.amazonaws.com") == "AWS"
    assert targets.cloud_provider(
        "beta-svc.internal.tr-west-1.postgresql.rds.myhuaweicloud.com") == "Huawei"
    assert targets.cloud_provider("db.internal.corp.example") == ""
    assert targets.cloud_provider(None) == ""


def test_label_with_provider():
    assert targets.label_with_provider("x", "h.eu.rds.amazonaws.com") == "x (AWS)"
    assert targets.label_with_provider(
        "x", "h.tr-west-1.postgresql.rds.myhuaweicloud.com") == "x (Huawei)"
    assert targets.label_with_provider("x", "on-prem-box") == "x"   # unknown → bare
