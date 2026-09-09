"""`/sql whoami` and `/sql roles` name the teams a person is actually in.

Both read `p_metrics_who_can_what`, whose teams column came from `teams` /
`team_members`. The pod cutover emptied those, so the column was NULL for all
29 people: everyone was told they belong to no team. A pod grant IS a team
grant, so they also could not see where their own access comes from -- on the
two subcommands that exist to answer exactly that.

Measured on the live view: 0 of 29 people had a team before, 25 after. The
remaining four have no membership in either model.

A UNION rather than a branch on the switch. A view cannot read `bot_config`
without a function call per row, and the union needs no switch to be correct:
whichever side is empty contributes nothing, and during a migration both are
true at once.
"""
import pathlib
import re

MIG = (pathlib.Path(__file__).resolve().parents[1]
       / "migrations" / "117_whoami_reads_both_models.sql").read_text(encoding="utf-8")


def _teams_cte() -> str:
    m = re.search(r"teams_per_user AS \((.*?)\n        \)", MIG, re.S)
    assert m, "teams_per_user CTE is gone"
    return m.group(1)


def test_both_models_feed_the_teams_column():
    cte = _teams_cte()
    assert "FROM team_members tm" in cte, "the legacy side is still a source"
    assert "FROM team_member m" in cte, "so is the new one"


def test_the_union_deduplicates():
    """A team the migration-109 mirror carries exists on both sides under the
    same name; without DISTINCT the person is shown it twice."""
    cte = _teams_cte()
    assert "UNION" in cte
    assert "array_agg(DISTINCT" in cte


def test_deleted_rows_do_not_come_back():
    """`team` and `team_member` soft-delete. The legacy tables did not, so the
    old CTE had nothing to filter and the new half must."""
    cte = _teams_cte()
    assert "NOT t_2.is_deleted" in cte
    assert "NOT m.is_deleted" in cte
    assert "NOT i.is_deleted" in cte


def test_the_label_is_what_a_person_calls_the_team():
    """This string is read by a human in Slack. A pod's `name` is the code the
    importer writes; `display_name` is what everybody says out loud."""
    assert "COALESCE(t_2.display_name, t_2.name)" in _teams_cte()


def test_only_slack_identities_are_joined():
    """The view is keyed by `slack_user_id`; joining any other provider's
    external id would put a foreign key in that column."""
    cte = _teams_cte()
    assert "i.provider = 'slack'" in cte


def test_the_other_columns_were_not_touched():
    """The view carries admin scope and per-user grants too. This change is
    about one CTE; a rewrite that quietly dropped a column would take
    `/sql whoami` with it."""
    for col in ("is_admin", "admin_max_tier", "user_grants", "is_bypass",
                "admin_scope_team_ids", "admin_scope_target_ids"):
        assert col in MIG, col
