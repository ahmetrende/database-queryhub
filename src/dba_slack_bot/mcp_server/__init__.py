"""QueryHub's MCP surface: a third door, for programs.

Slack and the web are doors for people. This one is for a program -- an
assistant that needs data and should not be holding a production credential to
get it. Nothing about governance changes behind it: the same grant resolution,
the same per-statement RO/RW/DDL classification, the same approval flow, the
same audit and the same PII masking. This package adds a transport, not a
second set of rules.

Two shapes are load-bearing and worth stating before anyone extends this.

**Identity belongs to the transport, never to a tool.** No tool here takes a
"who am I" argument. The caller is resolved once, by `caller.acting_principal`,
and the tools ask for it. Today it comes from configuration -- one principal,
one operator. When a chat bot fronts this for many people, only that function
changes; every tool signature stays exactly as it is. Had the identity been a
tool parameter instead, adding the second caller would have meant rewriting the
whole surface, and any client could have named whoever it liked.

**The tier ceiling is one decision in one place.** `policy.max_tier()` is asked
before anything is submitted, and it answers `ro` unless the operator has
deliberately widened it. Not scattered `if tier != "ro"` checks -- those are how
one of them gets forgotten.

The SDK is not imported here or in `tools`. Only `server` needs it, so the
package installs, imports and tests without `queryhub[mcp]` present, and the
extra dependency is genuinely optional rather than nominally so.
"""
