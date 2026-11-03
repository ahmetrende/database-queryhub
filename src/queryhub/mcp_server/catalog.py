"""The MCP tools as data: names, descriptions, argument shapes and handlers.

One list feeds two readers. The MCP door (`web/routes_mcp.py`) answers
`tools/list` with `listing()` and runs `tools/call` through `call()`. The
portal (the IDP panel) embeds the same listing as a file:

    python -m queryhub.mcp_server.catalog --door idp > docs/mcp-tools.idp.json

The portal answers `initialize` with its own instructions, so the descriptions
carry the rules of QueryHub. The portal lists each tool as `queryhub_<name>`,
and the file names them bare. A text for the model, in a description or in an
answer, names a tool as the portal lists it (`named()`).

No SDK is imported here. `call()` imports the tools when a call runs, so the
command above needs no database and no environment.

`call()` is the only way the door reaches a tool. It accepts the arguments
that the catalog names and no others. Identity is never an argument: a client
cannot name a person.
"""
from __future__ import annotations

import argparse
import functools
import hashlib
import json
import re
import sys
from dataclasses import dataclass

#: The portal lists each tool as `queryhub_<name>`. A bare name is not a tool
#: that the model can call.
IDP_TOOL_PREFIX = "queryhub_"


def named(name: str) -> str:
    """The tool `name` in backticks, as the model calls it through the portal."""
    return f"`{IDP_TOOL_PREFIX}{name}`"


@dataclass(frozen=True)
class Tool:
    name: str
    # The text for the model. A tool named in backticks here is listed with
    # the prefix of the portal (`_names_on`).
    description: str
    # JSON-schema `properties`, in the order the client shows them.
    properties: dict[str, dict]
    required: tuple[str, ...]

    def describe(self) -> str:
        return _names_on(self.description)

    def input_schema(self) -> dict:
        props = {}
        for key, shape in self.properties.items():
            if "description" in shape:
                shape = {**shape, "description": _names_on(shape["description"])}
            props[key] = shape
        return {"type": "object", "properties": props,
                "required": list(self.required), "additionalProperties": False}


_STR = {"type": "string"}
_INT = {"type": "integer"}


#: The default and the maximum wait of `submit_query`, in seconds. Two calls
#: of at most 30 seconds each sit in front of the door: the portal to QueryHub,
#: and the call inside the portal that carries the person. The portal cancels
#: its call after 25 seconds. The wait counts from the start of the call, so it
#: ends before that. `tools.submit_query` clamps to this value.
WAIT_SECONDS = 20


TOOLS: tuple[Tool, ...] = (
    Tool(
        "list_connections",
        "Lists the connections and databases that you can query, with your "
        "tier on each database. Call this tool first. Use only the names that "
        "it returns. `maxTier` is the highest tier that QueryHub accepts. "
        "`resultMode` tells what you can read of a result.",
        {}, ()),
    Tool(
        "describe_database",
        "Returns the table names in a database. If you pass `table`, it "
        "returns the columns of the tables whose names contain that text.",
        {"connection": {**_STR, "description": "A connection name from `list_connections`."},
         "database": {**_STR, "description": "A database name from `list_connections`."},
         "table": {**_STR, "description": "Part of a table name. Optional."}},
        ("connection", "database")),
    Tool(
        "classify_sql",
        "Returns the tier that a statement needs, and whether QueryHub accepts "
        "the statement. This tool is optional. `submit_query` does the same "
        "checks.",
        {"connection": {**_STR, "description": "A connection name from `list_connections`."},
         "sql": {**_STR, "description": "One SQL statement."}},
        ("connection", "sql")),
    Tool(
        "submit_query",
        "Submits one statement for the person. If QueryHub auto-approves the "
        "statement, the tool waits for the result. Put no personal data and no "
        "secret in the statement. Show the person `requestUrl`. A person always "
        "approves a write (RW) and a schema change (DDL). If `decision` is "
        "`needs_approval`, poll `query_status`. Do not submit the same statement "
        "again. If QueryHub refuses the statement, read the reason and change "
        "the statement.",
        {"connection": {**_STR, "description": "A connection name from `list_connections`."},
         "sql": {**_STR, "description": "One SQL statement."},
         "database": {**_STR, "description": "A database name from `list_connections`."},
         "justification": {**_STR, "description": "Why the person needs this data."},
         "wait_seconds": {**_INT,
                          "description": (f"How long to wait for the result. Default "
                                          f"{WAIT_SECONDS}. Maximum {WAIT_SECONDS}."),
                          "maximum": WAIT_SECONDS}},
        ("connection", "sql", "database")),
    Tool(
        "query_status",
        "Returns the status of a request that you submitted. Approval takes "
        "minutes, so poll this tool. Do not submit the statement again. Show "
        "the person `requestUrl`.",
        {"request_id": {**_INT, "description": "The `requestId` from `submit_query`."}},
        ("request_id",)),
    Tool(
        "fetch_result",
        "Returns the row count and the column names of a completed request. "
        "While `resultMode` is `metadata_only`, you read no rows. Tell the "
        "person to open `requestUrl` to see the result. Do not ask the person "
        "to paste the result. Only in `masked_rows` mode does it also return "
        "a page of rows.",
        {"request_id": {**_INT, "description": "The `requestId` from `submit_query`."},
         "offset": {**_INT, "description": "The first row to return. Default 0. Used in `masked_rows` mode only."},
         "limit": {**_INT, "description": "How many rows. Default 50. Maximum 200. Used in `masked_rows` mode only."}},
        ("request_id",)),
)

BY_NAME = {t.name: t for t in TOOLS}

# A tool name in backticks, in a text of this file.
_NAME_IN_TEXT = re.compile("`(" + "|".join(re.escape(n) for n in BY_NAME) + ")`")


def _names_on(text: str) -> str:
    """`text` with each tool that it names in backticks named as the portal
    lists it."""
    return _NAME_IN_TEXT.sub(lambda m: named(m.group(1)), text)


def _type_ok(value, schema: dict) -> bool:
    if schema.get("type") == "string":
        return isinstance(value, str)
    if schema.get("type") == "integer":
        # A bool is an int in Python. It is not an integer here.
        return isinstance(value, int) and not isinstance(value, bool)
    return False


def call(name: str, arguments: dict | None) -> dict:
    """Run one tool by name. The caller must already be bound (`caller.acting_as`).

    Raises `tools.ToolError` for an unknown tool, an argument the tool does not
    name, a missing or blank required argument and a value of the wrong type.
    """
    from . import tools
    tool = BY_NAME.get(name)
    if tool is None:
        raise tools.ToolError("unknown_tool", f"There is no tool named '{name}'.")
    args = arguments or {}
    if not isinstance(args, dict):
        raise tools.ToolError("bad_arguments", "The arguments must be an object.")
    for key in args:
        if key not in tool.properties:
            raise tools.ToolError("bad_arguments",
                                  f"The tool '{name}' has no argument '{key}'.")
    for key in tool.required:
        # A blank string names nothing. For `database` it would send the
        # statement to the default database of the connection.
        value = args.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise tools.ToolError("bad_arguments",
                                  f"The tool '{name}' needs the argument '{key}'.")
    clean = {}
    for key, value in args.items():
        if value is None:
            continue
        if not _type_ok(value, tool.properties[key]):
            kind = tool.properties[key].get("type")
            raise tools.ToolError("bad_arguments",
                                  f"The argument '{key}' must be of type {kind}.")
        clean[key] = value
    # Looked up now, not at import, so a test that replaces a tool reaches it.
    return getattr(tools, tool.name)(**clean)


def listing() -> list[dict]:
    """The answer to `tools/list`: name, description and input schema, in
    catalog order."""
    return [{"name": t.name, "description": t.describe(),
             "inputSchema": t.input_schema()} for t in TOOLS]


def version(tools_: list[dict]) -> str:
    """The version of a listing: the sha256 of its canonical JSON. The portal
    reads it from the file and does not compute it again."""
    canonical = json.dumps(tools_, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def export() -> dict:
    """The catalog as a document: `version` and `tools`."""
    listed = listing()
    return {"version": version(listed), "tools": listed}


def render() -> str:
    """The text of the catalog file: UTF-8 JSON, two spaces of indent and a
    newline at the end."""
    return json.dumps(export(), indent=2, ensure_ascii=False) + "\n"


@functools.cache
def idp_version() -> str:
    """The version of the listing. The door sends it on every answer
    (`x-queryhub-catalog`), so the portal sees when its copy is old."""
    return export()["version"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m queryhub.mcp_server.catalog",
        description="Print the tool catalog that the portal embeds, as JSON.")
    # One value. The flag stays so that the command in the docs of the portal
    # keeps working.
    parser.add_argument("--door", choices=("idp",), default="idp",
                        help="idp: the file that the portal embeds (the default and only value).")
    parser.parse_args(argv)
    sys.stdout.write(render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
