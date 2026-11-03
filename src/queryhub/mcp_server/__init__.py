"""QueryHub's MCP surface: a third door, for programs.

Slack and the web are doors for people. This one is for a program -- an
assistant that needs data and should not be holding a production credential to
get it. Claude Code reaches it through the portal (the IDP panel), which calls
`POST /mcp` (`web/routes_mcp.py`) with its identity assertion. Nothing about
governance changes behind it: the same grant resolution, the same
per-statement RO/RW/DDL classification, the same approval flow, the same audit
and the same PII masking. This package adds a transport, not a second set of
rules.

Two shapes are load-bearing and worth stating before anyone extends this.

**Identity belongs to the transport, never to a tool.** No tool here takes a
"who am I" argument. The door binds the person that the portal proves, and
the tools ask `caller.acting_principal` for it. Had the identity been a tool
parameter instead, any client could have named whoever it liked.

**The tier ceiling is one decision in one place.** `policy.max_tier()` is asked
before anything is submitted, and it answers `ro` unless the operator has
deliberately widened it. Not scattered `if tier != "ro"` checks -- those are how
one of them gets forgotten.

No MCP SDK is imported: the door speaks the protocol itself, so the package
needs no extra dependency.
"""
