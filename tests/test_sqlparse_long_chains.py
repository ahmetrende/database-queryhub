"""A long operator chain is refused in well under a second.

sqlparse 0.5 built each group's text by flattening the whole subtree beneath
it. An operator chain nests one group per operator, so the cost was cubic in
the chain's length: 1,000 chained `+` took 7 s to refuse, from a statement of
4 KB, and analyze() runs on every classify call the editor makes. sqlparse 0.6
joins the children's texts instead, and requirements.txt asks for it. These
tests fail if a sqlparse release brings the cost back.
"""
import time

import pytest

from queryhub import query_safety as qs


@pytest.mark.parametrize("sql_text", [
    "SELECT 1" + " + 1" * 1000,
    "SELECT 'a'" + " || 'a'" * 1000,
    "SELECT 1" + " = 1" * 1000,
    "SELECT f(1)" + " + f(1)" * 1000,
    "SELECT 1 + 1" + " = 1 + 1" * 1000,
    "SELECT d" + " -> 'k'" * 1000,
], ids=["plus", "concat", "comparison", "function-operands", "mixed", "json"])
def test_a_long_chain_is_refused_in_well_under_a_second(sql_text):
    """On sqlparse 0.5 these took 7 to 14 s each; on 0.6, about 0.1 s."""
    started = time.perf_counter()
    report = qs.analyze(sql_text)
    elapsed = time.perf_counter() - started
    assert report.blocked
    assert "nested too deeply" in report.blockers[0]
    assert elapsed < 1.0, f"{elapsed:.2f}s"
