"""An empty streamed read must report 0 rows RETURNED, not -1 affected.

`cur.stream()` is lazy: `description` is only populated once a row has been
produced. A SELECT that matches nothing never produces one, so the executor
used to fall through to the DML branch and report the driver's rowcount —
which is -1 for a streamed statement. Users saw a clean SELECT finish as
"-1 rows affected" (seen live on request #1882).
"""
from queryhub import executor as ex


class _EmptyStreamCur:
    """Mimics psycopg: stream() yields nothing and leaves description None."""
    description = None
    rowcount = -1          # what the server reports for a streamed statement

    def stream(self, sql):
        return iter(())

    def execute(self, sql, prepare=False):    # pragma: no cover - not used here
        raise AssertionError("a streamable read must go through stream()")


def _stmt(sql="SELECT * FROM t WHERE 1=0", leading="SELECT"):
    return type("S", (), {"rewritten": sql, "leading": leading})()


def test_empty_streamed_select_reports_zero_rows_returned():
    res = ex._execute_main_statement(
        _EmptyStreamCur(), _stmt(), 1, 1882, True, 100, 1000,
        force_extended=True,
    )
    assert res.rowcount == 0            # never -1
    assert res.has_result_set is True   # "returned", not "affected"
    assert res.csv_path is None         # nothing to attach for an empty result


def test_dml_rowcount_of_minus_one_is_clamped():
    # A driver that cannot count must not leak -1 into the user-facing count.
    class _Cur:
        description = None
        rowcount = -1

        def execute(self, sql, prepare=False):
            pass

    res = ex._execute_main_statement(
        _Cur(), _stmt("UPDATE t SET x = 1", "UPDATE"), 1, 1, False, 100, 1000)
    assert res.rowcount == 0
    assert res.has_result_set is False
