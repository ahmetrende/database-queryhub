"""Make sqlparse build a long operator chain in linear time.

sqlparse 0.5 builds `a + b + c` as one nested group per operator:
`Operation(Operation(a + b) + c)`. A group's constructor computes the group's
text by flattening the whole subtree beneath it, so each new operator re-reads
the entire chain. The time is cubic in the chain's length:
- 300 operators took 0.2 s;
- 1,000 took 7 s, from a statement of 4 KB;
- a chain of function calls or comparisons took up to 14 s.

sqlparse refuses such a chain anyway, since it nests deeper than its limit of
100 levels. But it only refuses after building it. query_safety.analyze parses
every statement the editor checks, so one such statement tied up a web worker
for seconds.

The fix computes a new group's text from its children's texts instead. The
string is the same. Grouping never changes a token's text; it only regroups
tokens, and a group extended in place recomputes its own text. So every child's
text already equals the text beneath it when its parent is built. With the fix,
the same 1,000-operator chain is refused in 0.04 s, and every decision sqlparse
makes is unchanged. tests/test_sqlparse_patch.py compares the two constructors
tree for tree, on the test suite's statements and on fuzzed ones. Before this
shipped, the same comparison also passed on all 7,283 stored requests.

sqlparse 0.6.0 made the same change upstream: its constructor joins the
children's texts. `apply()` recognises that constructor and leaves it in place.
It replaces only the 0.5 constructor this was measured against. Any other
constructor is left alone and fails the test above, so the change gets looked at
rather than being lost without notice.
"""
from __future__ import annotations

import inspect
import logging
import textwrap

from sqlparse import sql

log = logging.getLogger(__name__)

# sqlparse 0.5.5, sqlparse/sql.py, TokenList.__init__.
_MEASURED = """
def __init__(self, tokens=None):
    self.tokens = tokens or []
    [setattr(token, 'parent', self) for token in self.tokens]
    super().__init__(None, str(self))
    self.is_group = True
"""

# sqlparse 0.6.0, the same place: already joins the children's texts.
_FIXED_UPSTREAM = """
def __init__(self, tokens=None):
    self.tokens = tokens or []
    [setattr(token, 'parent', self) for token in self.tokens]
    super().__init__(None, ''.join(token.value for token in self.tokens))
    self.is_group = True
"""

ORIGINAL = sql.TokenList.__init__


def init_from_children(self, tokens=None):
    """TokenList.__init__, with the text joined from the children's texts.

    `super()` in the original is Token for every group class: TokenList comes
    directly before Token in each of their MROs.
    """
    self.tokens = tokens or []
    for token in self.tokens:
        token.parent = self
    sql.Token.__init__(self, None, "".join(token.value for token in self.tokens))
    self.is_group = True


def source_of(init) -> str | None:
    try:
        return textwrap.dedent(inspect.getsource(init)).strip()
    except (OSError, TypeError):
        return None


def joins_children(init) -> bool:
    """Whether `init` builds a group's text from its children's texts."""
    return (init is init_from_children
            or source_of(init) == _FIXED_UPSTREAM.strip())


def apply() -> bool:
    """Make a group's text come from its children. True when it does.

    Installs `init_from_children` over the measured 0.5 constructor. Leaves the
    0.6 constructor alone, since it already does this. Leaves any other
    constructor alone too, with a warning.
    """
    current = sql.TokenList.__init__
    if joins_children(current):
        return True
    if source_of(current) != _MEASURED.strip():
        log.warning("sqlparse TokenList.__init__ is not one sqlparse_patch "
                    "knows; leaving it alone")
        return False
    setattr(sql.TokenList, "__init__", init_from_children)
    return True
