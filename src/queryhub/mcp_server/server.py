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
QueryHub runs SQL on production databases. Every statement is under review.
Nothing on this MCP server bypasses the review.

QueryHub processes each statement that you submit in these steps:
1. It classifies the statement.
2. It checks the statement against your own grants.
3. It auto-approves the statement, or it sends the statement to a human DBA.
4. It returns the results with personal data masked.
QueryHub audits every step.

This MCP server accepts RO (read-only) statements only, unless an operator
sets a higher maximum tier. `list_connections` shows the current maximum tier
in `maxTier`. If you want to know before you submit whether QueryHub accepts a
statement, call `classify_sql`. Otherwise, you learn it from a refusal.

For an auto-approved statement, `submit_query` waits for the result and
returns its first rows. Usually, one call is the whole exchange. If a human
must approve the statement, `submit_query` returns early with a request id.
Then poll `query_status`. Expect the approval to take minutes.

`describe_database` lists the table names. To get columns, pass `table` with
part of a table name. If you ask for everything on a large database, the
answer is megabytes of data and tells you almost nothing.

Do not paste credentials, personal data or secrets into a statement. Do not
retry a refused statement unchanged. The refusal tells you what to change.
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
        description="Runs reviewed SQL on production databases.",
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

    @server.tool(description="Lists the connections and databases that you may "
                             "query, with your tier on each connection. It also "
                             "returns the maximum tier that this MCP server "
                             "accepts (`maxTier`).")
    def list_connections() -> dict:
        return _wrap(tools.list_connections)()

    @server.tool(description="Returns the table names in a database. If you pass "
                             "`table`, it returns the columns of the tables whose "
                             "names contain that text.")
    def describe_database(connection: str, database: str,
                          table: str | None = None) -> dict:
        return _wrap(tools.describe_database)(connection, database, table)

    @server.tool(description="Returns the tier that a statement needs, and whether "
                             "this MCP server accepts the statement.")
    def classify_sql(connection: str, sql: str) -> dict:
        return _wrap(tools.classify_sql)(connection, sql)

    @server.tool(description="Submits a statement. If QueryHub auto-approves the "
                             "statement, the tool waits for the result.")
    def submit_query(connection: str, sql: str, database: str | None = None,
                     justification: str | None = None,
                     wait_seconds: int = tools.DEFAULT_WAIT_SECONDS) -> dict:
        return _wrap(tools.submit_query)(connection, database, sql,
                                         justification, wait_seconds)

    @server.tool(description="Returns the status of a request that you submitted.")
    def query_status(request_id: int) -> dict:
        return _wrap(tools.query_status)(request_id)

    @server.tool(description="Returns a page of rows from the result of a "
                             "completed request.")
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
