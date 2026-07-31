"""QueryHub Web — the browser frontend's HTTP layer.

A thin FastAPI app in front of the SAME core the Slack bot uses
(core_submit, teams, auto_approve, executor, pii, audit). No security
logic lives here: this package does auth (pluggable providers +
sessions per AUTH.md) and JSON shaping per API_CONTRACT.md.
"""
