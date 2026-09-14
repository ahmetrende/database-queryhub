"""User-facing error scrubbing.

`scrub()` strips sensitive details out of psycopg / libpq errors before
they reach a Slack message: hostnames, role names, ports, the "LINE n:"
caret context (which often points at our wrappers, not the user's
query), and the DETAIL/HINT trailers that can echo connection metadata.

Used by both pre_flight (modal field errors) and executor (DM after
execution failure) so the same scrubbing rules apply everywhere.
"""
from __future__ import annotations

import re

# Patterns that carry connection / role detail. Matched on the *scrubbed*
# message, not the raw exception, so each substitution is whitespace-safe.
_CONN_DETAIL_PATTERNS = [
    # `host=foo.example.com port=5432 user=queryhub_rw_payments`
    re.compile(r'\b(host|hostaddr|port|user|dbname)\s*=\s*\S+', re.IGNORECASE),
    # `for user "queryhub_rw_payments"` — surfaces the role name
    re.compile(r'\bfor user\s+"[^"]+"', re.IGNORECASE),
    # libpq's own phrasing: `connection to server at "db.example.internal"
    # (192.0.2.10), port 5432 failed: ...`. Matching the PHRASE rather than the
    # hostname is what makes this complete — the per-domain patterns below
    # only cover the providers someone remembered to add, and they already
    # missed a whole cloud once (a fleet moved to a provider not listed here
    # and its hostnames sailed through). Redacts the parenthesised address
    # with it. Deliberately not "any quoted dotted token": SQL errors quote
    # relation names the same way, and `relation "public.users" does not
    # exist` has to stay readable.
    re.compile(r'\bserver at\s+"[^"]*"(\s*\([^)]*\))?', re.IGNORECASE),
    # Quoted hostnames on the clouds and private suffixes this fleet uses.
    re.compile(r'"[^"\s]*\.(?:rds\.amazonaws\.com|myhuaweicloud\.com)"(:\d+)?',
               re.IGNORECASE),
    # Private / internal TLDs — never routable, always ours.
    re.compile(r'"[^"\s]*\.(?:internal|local|lan|corp|intranet)"(:\d+)?',
               re.IGNORECASE),
    # IPv4 in private RFC1918 ranges — internal addresses
    re.compile(r'\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))(?:\.\d{1,3}){2,3}\b'),
    # SQL Server / Azure SQL: hostnames + ODBC connection-string fragments
    # that can echo host/role/password if an error surfaces the DSN.
    re.compile(r'"[^"]*\.database\.windows\.net"(:\d+)?', re.IGNORECASE),
    re.compile(r'\b(server|address|addr|uid|pwd|driver)\s*=\s*[^;"\s]+',
               re.IGNORECASE),
]


def scrub(raw: str | Exception) -> str:
    """Return a Slack-modal- and DM-safe error message (plain text, ≤500
    chars, no mrkdwn). Strips libpq's verbose error envelope to its first
    informative line, drops the LINE/DETAIL/HINT trailers (which often
    point at our EXPLAIN wrapper rather than the user's query), removes
    connection-metadata patterns (host=..., for user "...", RDS hostnames),
    capitalizes the first letter, and caps length."""
    text = str(raw).strip()
    msg = ""
    # 1. Prefer an explicit ERROR: / FATAL: line if present.
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("ERROR:") or line.startswith("FATAL:"):
            _, _, after = line.partition(":")
            msg = after.strip()
            break
    # 2. Otherwise take the first non-empty line that is NOT itself a
    #    LINE/DETAIL/HINT trailer.
    if not msg:
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if (stripped.startswith("LINE ")
                    or stripped.startswith("DETAIL:")
                    or stripped.startswith("HINT:")
                    or stripped.startswith("CONTEXT:")
                    or set(stripped) <= {"^", " "}):  # caret-only line
                continue
            msg = stripped
            break
    # 3. Last-ditch fallback.
    if not msg:
        msg = " ".join(text.split())

    # SQL Server (pyodbc/ODBC) envelope: drop the driver bracket prefixes
    # (`[Microsoft][ODBC Driver 18 for SQL Server][SQL Server]`) and the
    # trailing `(NNN) (SQLExecDirectW)` code tuple, leaving the core message.
    msg = re.sub(r"\[(?:Microsoft|SQL Server[^\]]*|ODBC[^\]]*)\]", "", msg)
    msg = re.sub(r"\s*\(\d+\)\s*\(SQL[A-Za-z]+\)\.?\s*$", "", msg)

    # Drop trailing LINE/DETAIL/HINT noise that may have been concatenated
    # onto the first line (psycopg sometimes returns the full envelope
    # as a single string with embedded newlines OR collapsed to spaces).
    msg = re.sub(r"\s*LINE\s+\d+\s*:.*$", "", msg, flags=re.DOTALL)
    msg = re.sub(r"\s*DETAIL\s*:.*$", "", msg, flags=re.DOTALL)
    msg = re.sub(r"\s*HINT\s*:.*$", "", msg, flags=re.DOTALL)
    msg = re.sub(r"\s*CONTEXT\s*:.*$", "", msg, flags=re.DOTALL)

    for pat in _CONN_DETAIL_PATTERNS:
        msg = pat.sub("[redacted]", msg)
    msg = re.sub(r"\s+", " ", msg).strip()

    # Database errors quote the offending VALUE, so the error path could hand
    # back row data that the result path would have masked:
    #   invalid input syntax for type integer: "12345678901"
    #   value too long for type character varying(8): "jane@example.com"
    # DETAIL/HINT (where a unique violation puts the key) is dropped above, but
    # the primary message carries values too. Run the same content detectors
    # here, so masking is a property of everything we return — not only of
    # result files.
    try:
        from . import pii
        if pii.is_enabled():
            msg = str(pii.mask_value(msg, set()))
    except Exception:      # masking must never break error reporting
        pass

    if msg and msg[0].islower():
        msg = msg[0].upper() + msg[1:]
    return msg[:500]
