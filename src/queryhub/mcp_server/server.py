"""The MCP adapter: protocol in, `tools` functions out.

Deliberately thin. Everything decidable lives in `tools` and `policy`, which
import no SDK, so the reasoning is testable without a protocol and the optional
dependency stays genuinely optional. What is here is registration, argument
names and error shape -- nothing that decides who may do what.

Written against mcp 2.x, where the server class is `MCPServer` from
`mcp.server.mcpserver`. The 1.x name `FastMCP` is gone; the SDK keeps a stub at
the old path whose only job is to say so, because the bare import error gave no
hint that the installed SDK was a different major version.
"""
from __future__ import annotations

import logging

from . import tools

log = logging.getLogger(__name__)

INSTRUCTIONS = """\
QueryHub runs SQL against production databases under review.

Nothing here bypasses that. A statement you submit is classified, checked
against the caller's own grants, and either auto-approved or sent to a human
DBA; results come back with personal data masked, and every step is audited.

Read-only unless an operator has widened it -- `list_connections` reports the
current ceiling. Call `classify_sql` first if you want to know whether a
statement will be accepted before you submit it, rather than learning it from
a refusal.

Do not paste credentials, personal data or secrets into a query. Do not retry
a refused statement unchanged; the refusal names what to change.
"""


def build():
    """Construct the server with the tools registered.

    Imported lazily inside the function so that importing this module -- which
    the tests do -- does not require the SDK to be installed.
    """
    from mcp.server.mcpserver import MCPServer

    server = MCPServer(
        name="queryhub",
        title="QueryHub",
        description="Run reviewed SQL against production databases.",
        instructions=INSTRUCTIONS,
    )

    def _wrap(fn):
        """Turn a ToolError into a message the caller can act on.

        An exception escaping to the protocol becomes an opaque internal
        error, and an assistant that gets one retries the same call. A refusal
        has to arrive as words: which limit was hit, and what to do instead.
        """
        def inner(*a, **kw):
            try:
                return fn(*a, **kw)
            except tools.ToolError as e:
                return {"error": e.code, "message": e.message}
        inner.__name__ = fn.__name__
        inner.__doc__ = fn.__doc__
        return inner

    @server.tool(description="List the databases you may query, and at what tier.")
    def list_connections() -> dict:
        return _wrap(tools.list_connections)()

    @server.tool(description="Tables and columns of one database, from QueryHub's catalog.")
    def describe_database(connection: str, database: str) -> dict:
        return _wrap(tools.describe_database)(connection, database)

    @server.tool(description="What tier a statement needs, and whether this door accepts it.")
    def classify_sql(connection: str, sql: str) -> dict:
        return _wrap(tools.classify_sql)(connection, sql)

    @server.tool(description="Submit a statement for review and execution.")
    def submit_query(connection: str, sql: str, database: str | None = None,
                     justification: str | None = None) -> dict:
        return _wrap(tools.submit_query)(connection, database, sql, justification)

    @server.tool(description="Where a submitted query has got to.")
    def query_status(request_id: int) -> dict:
        return _wrap(tools.query_status)(request_id)

    @server.tool(description="A page of rows from a completed query.")
    def fetch_result(request_id: int, offset: int = 0, limit: int = 50) -> dict:
        return _wrap(tools.fetch_result)(request_id, offset, limit)

    return server


def main() -> None:
    """Run over stdio, which is the transport a local client speaks.

    No network listener in this release: the door opens for a program on this
    host, started by the client that uses it. An HTTP transport is what a
    remote bot will need, and it needs the identity work in `caller` first --
    shipping the listener before that would mean a port answering as one fixed
    user.
    """
    from .. import db
    db.init_pool()
    build().run(transport="stdio")
