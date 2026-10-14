"""User-facing error scrubbing.

`scrub()` strips sensitive details out of psycopg / libpq errors before
they reach a Slack message: hostnames, role names, ports, the "LINE n:"
caret context (which often points at our wrappers, not the user's
query), and the DETAIL/HINT trailers that can echo connection metadata.

Used by both pre_flight (modal field errors) and executor (DM after
execution failure) so the same scrubbing rules apply everywhere.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

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


_HIDDEN = "[hidden]"

# The caller sees at most 500 characters. A database can echo a value of a
# megabyte, and some detectors are slower than linear on a long run of one
# character (200 KB of "x" took 38 seconds). So the code caps the work. The hiding
# step reads at most _WORK_LIMIT characters. The content detectors read at most
# _DETECTOR_LIMIT characters of the rest.
_WORK_LIMIT = 8_000
_DETECTOR_LIMIT = 1_000

# Messages that echo a VALUE the database read, not a name. Every pattern has
# the named group `v`, the echoed value, and may have `pre` and `suf`, the
# words around it. The words stay, so the requester still learns the kind of
# error and the type. Only `v` goes.
#
# A cast such as `SELECT full_name::int FROM customers WHERE id = 1` fails with
# the value of that row in the message. The result file would have masked the
# same column. Content detectors catch an e-mail or an IBAN. They cannot catch
# a name, an address or a salary, so the code matches these phrasings by their words.
#
# Each phrasing is here because an engine writes it. A message that names an
# identifier (`relation "orders" does not exist`) is not here: a requester
# cannot fix a query without that name.
_DATA_ECHO_PATTERNS = [
    # PostgreSQL. The value is the last thing on the line.
    re.compile(r'(?P<pre>\binvalid input syntax for\b[^:]{1,80}:\s*)"(?P<v>.*?)"?$',
               re.IGNORECASE),
    re.compile(r'(?P<pre>\binvalid input value for enum\b[^:]{1,80}:\s*)"(?P<v>.*?)"?$',
               re.IGNORECASE),
    re.compile(r'(?P<pre>\bdate/time field value out of range:\s*)"(?P<v>.*?)"?$',
               re.IGNORECASE),
    re.compile(r'(?P<pre>\bmalformed array literal:\s*)"(?P<v>.*?)"?$', re.IGNORECASE),
    re.compile(r'(?P<pre>\bunrecognized time zone name:\s*)"(?P<v>.*?)"?$', re.IGNORECASE),
    re.compile(r'(?P<pre>\binvalid hexadecimal digit:\s*)"(?P<v>.*?)"?$', re.IGNORECASE),
    re.compile(r'(?P<pre>\binvalid byte sequence for encoding\s+"[^"]*":\s*)(?P<v>.+)$',
               re.IGNORECASE),
    # The value sits in the middle of the sentence. A message that a driver or the
    # work limit cut short has no closing quote. Then the rest of the text is the
    # value, and it is hidden to the end. SQL Server cuts a message at 2047
    # characters, so a long text cell loses its closing quote this way.
    re.compile(r'(?P<pre>\bvalue\s+)"(?P<v>[^"]*)(?:"(?P<suf>\s+is out of range\b)|$)',
               re.IGNORECASE),
    # The float form has no `value` before the quote, so it needs its closing
    # quote. Without that rule the closing quote of any name would start a value.
    re.compile(r'(?P<pre>(?:^|\s))"(?P<v>[^"]*)"(?P<suf>\s+is out of range for type\b)',
               re.IGNORECASE),
    re.compile(r'(?P<pre>\binvalid value\s+)"(?P<v>[^"]*)(?:"(?P<suf>\s+for\s+")|$)',
               re.IGNORECASE),
    re.compile(r'(?P<pre>\btime zone\s+)"(?P<v>[^"]*)(?:"(?P<suf>\s+not recognized\b)|$)',
               re.IGNORECASE),
    # psycopg, when it cannot build a Python value from a column.
    re.compile(r"(?P<pre>\b(?:timestamp|date|time) too (?:large|small)\b[^:]{0,60}:\s*)"
               r"'(?P<v>.*?)'?$", re.IGNORECASE),
    # SQL Server.
    re.compile(r"(?P<pre>\bConversion failed when converting the \w+ value\s+)"
               r"'(?P<v>.*?)(?:'(?P<suf>\s+to data type\b)|$)", re.IGNORECASE),
    re.compile(r"(?P<pre>\bThe conversion of the \w+ value\s+)"
               r"'(?P<v>.*?)(?:'(?P<suf>\s+overflowed\b)|$)", re.IGNORECASE),
    re.compile(r"(?P<pre>\bThe duplicate key value is\s*)\((?P<v>.*?)"
               r"(?:\)(?P<suf>(?=\.(?:\s|\(|$)|$))|$)", re.IGNORECASE),
    re.compile(r"(?P<pre>\bTruncated value:\s*)'(?P<v>.*?)(?:'(?P<suf>(?=\.|\s|$))|$)",
               re.IGNORECASE),
    # ClickHouse.
    re.compile(r"(?P<pre>\bCannot parse string\s+)'(?P<v>.*?)(?:'(?P<suf>\s+as\b)|$)",
               re.IGNORECASE),
    re.compile(r"(?P<pre>\bCannot convert string\s+)'(?P<v>.*?)(?:'(?P<suf>\s+to type\b)|$)",
               re.IGNORECASE),
    re.compile(r"(?P<pre>\bbefore:\s*)'(?P<v>.*?)'?$", re.IGNORECASE),
    re.compile(r"(?P<pre>\b(?:Unknown element|Unexpected value)\s+)"
               r"'(?P<v>.*?)(?:'(?P<suf>\s+(?:for|in)\b)|$)", re.IGNORECASE),
    # Athena (Trino).
    re.compile(r"(?P<pre>\bCannot cast (?:\w+ )?)'(?P<v>.*?)(?:'(?P<suf>\s+to\b)|$)",
               re.IGNORECASE),
    re.compile(r"(?P<pre>\bValue cannot be cast to \w+:\s*)(?P<v>.+)$", re.IGNORECASE),
]


def _hide_data_values(msg: str, query: str | None) -> str:
    """Replace each value a database echoed from its own data with `[hidden]`.

    A value that appears in `query` stays. The requester typed it, so showing it
    tells them nothing new, and it is how they find the bad literal in a long
    IN list. Whitespace counts as one space on both sides, because the message
    is already collapsed.
    """
    typed = " ".join(query.split()) if query else ""

    def _swap(m: re.Match[str]) -> str:
        value = " ".join(m.group("v").split())
        if not value or (typed and value in typed):
            return m.group(0)
        groups = m.groupdict()
        return f"{groups.get('pre') or ''}{_HIDDEN}{groups.get('suf') or ''}"

    for pat in _DATA_ECHO_PATTERNS:
        msg = pat.sub(_swap, msg)
    return msg


def scrub(raw: str | Exception, *, query: str | None = None,
          requester_data: bool = False) -> str:
    """Return a Slack-modal- and DM-safe error message (plain text, ≤500
    chars, no mrkdwn). Strips libpq's verbose error envelope to its first
    informative line, drops the LINE/DETAIL/HINT trailers (which often
    point at our EXPLAIN wrapper rather than the user's query), removes
    connection-metadata patterns (host=..., for user "...", RDS hostnames),
    capitalizes the first letter, and caps length.

    With PII masking on, it also hides a value that the database read from its
    own data and put in the message (see `_DATA_ECHO_PATTERNS`). Pass the SQL
    the requester wrote as `query`: a value found in it stays visible. Pass
    `requester_data=True` where the values came from the requester's own upload,
    such as a CSV import, and nothing is hidden."""
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
    # the primary message carries values too. Two steps, so masking is a
    # property of everything we return, not only of result files:
    #   1. Hide a value the database echoed from its own data. The words of the
    #      message find it, so it works for a name, an address or a salary.
    #   2. Run the content detectors over the rest (e-mail, IBAN, card).
    # Each step has its own guard: a failure in one must not skip the other, and
    # neither may break error reporting.
    try:
        from . import pii
        masking = pii.is_enabled()
    except Exception:
        masking = False
    if masking:
        msg = msg[:_WORK_LIMIT]
        if not requester_data:
            try:
                msg = _hide_data_values(msg, query)
            except Exception:
                log.warning("errors: hiding echoed values failed", exc_info=True)
        try:
            msg = str(pii.mask_value(msg[:_DETECTOR_LIMIT], set()))
        except Exception:
            pass

    if msg and msg[0].islower():
        msg = msg[0].upper() + msg[1:]
    return msg[:500]
