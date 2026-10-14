"""User-facing error scrubbing.

`scrub()` strips sensitive details out of psycopg / libpq errors before
they reach a Slack message: hostnames, role names, ports, the "LINE n:"
caret context (which often points at our wrappers, not the user's
query), and the DETAIL/HINT trailers that can echo connection metadata.

Used by both pre_flight (modal field errors) and executor (DM after
execution failure) so the same scrubbing rules apply everywhere.
"""
from __future__ import annotations

import ast
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
# Each phrasing is here because an engine writes it. A sweep on a real
# PostgreSQL 17 and a real SQL Server 2022, with a sentinel value in every
# column type, found every phrasing of those two below. A message that names an
# identifier (`relation "orders" does not exist`) is in `_NAME_ECHO_PATTERNS`
# instead, with a different rule.
#
# The pattern for a value in the middle of a sentence scans lazily to the
# closing quote and the words after it. A value can hold a quote of its own
# (`"Jordan "Ray""`), so the closing quote is the one the words follow. A
# message that a driver or the work limit cut short has no closing quote. Then
# the rest of the text is the value, and it is hidden to the end. SQL Server
# cuts a message at 2047 characters, so a long text cell loses its closing
# quote this way. The `|$` branch also keeps the cost linear: the first match
# of a repeated prefix takes the rest of the text, so the scan never starts
# again at a later copy of the prefix.
_DATA_ECHO_PATTERNS = [
    # PostgreSQL. The value is the last thing on the line.
    re.compile(r'(?P<pre>\binvalid input syntax for\b[^:]{1,80}:\s*)"(?P<v>.*?)"?$',
               re.IGNORECASE),
    re.compile(r'(?P<pre>\binvalid input value for enum\b[^:]{1,80}:\s*)"(?P<v>.*?)"?$',
               re.IGNORECASE),
    # `date/time field value out of range: "2020-13-45"`, `timestamp out of
    # range: "5874898-01-01"`, `interval field value out of range: "..."`,
    # `time zone displacement out of range: "..."`. make_date() and make_time()
    # write the value without quotes: `time field value out of range: 25:00:00`.
    re.compile(r'(?P<pre>\b(?:date/time|date|time|interval|timestamp|time zone displacement)'
               r'(?: field value)? out of range:\s*)"?(?P<v>.*?)"?$', re.IGNORECASE),
    re.compile(r'(?P<pre>\bmalformed (?:array|range|multirange|record) literal:\s*)'
               r'"(?P<v>.*?)"?$', re.IGNORECASE),
    re.compile(r'(?P<pre>\bunrecognized time zone name:\s*)"(?P<v>.*?)"?$', re.IGNORECASE),
    re.compile(r'(?P<pre>\binvalid hexadecimal digit:\s*)"(?P<v>.*?)"?$', re.IGNORECASE),
    re.compile(r'(?P<pre>\binvalid byte sequence for encoding\s+"[^"]*":\s*)(?P<v>.+)$',
               re.IGNORECASE),
    # Functions that take a name, a unit, an encoding, a size, a format or a
    # privilege from a column: `invalid cidr value: "..."`, `unrecognized
    # encoding: "..."` (encode), `invalid size: "..."` (pg_size_bytes), `string
    # is not a valid identifier: "..."` (parse_ident), `syntax error in
    # tsquery: "..."`, `unrecognized privilege type: "..."`, `date format is not
    # recognized: "..."` (jsonpath .date()), `unrecognized key word: "..."`
    # (aclitem), `invalid value for parameter "work_mem": "..."` (set_config).
    re.compile(r'(?P<pre>\b(?:invalid cidr value|unrecognized encoding|invalid size|'
               r'string is not a valid identifier|unrecognized privilege type|'
               r'unrecognized headline parameter|invalid parameter list format|'
               r'unrecognized key word|unrecognized weight|invalid regular expression option|'
               r'syntax error in (?:tsquery|tsvector)|\w+ format is not recognized|'
               r'invalid value for parameter "[^"]{1,80}"):\s*)"(?P<v>.*?)"?$', re.IGNORECASE),
    re.compile(r'(?P<pre>\b(?:invalid (?:source|destination) encoding name|'
               r'unrecognized object type|invalid configuration parameter name|'
               r'unrecognized format\(\) type specifier)\s+)"(?P<v>.*?)"?$', re.IGNORECASE),
    # The number is the value: `requested character too large for encoding:
    # 1234567` (chr), `index 1234567 out of valid range, 0..22` (get_byte),
    # `large object 1234567 does not exist`, `collation with OID 1234567 does
    # not exist`, `transaction ID 1234567 is in the future`.
    re.compile(r'(?P<pre>\brequested character (?:too large|not valid) for encoding:\s*)'
               r'(?P<v>.+)$', re.IGNORECASE),
    re.compile(r'(?P<pre>\bindex\s+)(?P<v>-?\d+)(?P<suf>\s+out of valid range\b)(?:,\s*\S+)?',
               re.IGNORECASE),
    re.compile(r'(?P<pre>\b(?:large object|with OID|transaction ID)\s+)(?P<v>\d+)\b',
               re.IGNORECASE),
    # The value sits in the middle of the sentence. `invalid value "Jo" for
    # "YYYY"` comes before the plain `value "..."` form on purpose: the plain
    # form would take `Jo" for "YYYY` as the value.
    re.compile(r'(?P<pre>\binvalid value\s+)"(?P<v>.*?)(?:"(?P<suf>\s+for\s+")|$)',
               re.IGNORECASE),
    re.compile(r'(?P<pre>\bvalue\s+)"(?P<v>.*?)(?:"(?P<suf>\s+is out of range\b)|$)',
               re.IGNORECASE),
    # The float form starts the message with the quote: `"1e999" is out of
    # range for type real`. So does `"Z" is not a valid binary digit`. Anchored
    # at the start, so the closing quote of a name cannot start a value.
    re.compile(r'(?P<pre>^)"(?P<v>.*?)"(?P<suf>\s+is (?:out of range for type|'
               r'not a valid (?:binary|hexadecimal) digit)\b)', re.IGNORECASE),
    re.compile(r'(?P<pre>\btime zone\s+)"(?P<v>.*?)(?:"(?P<suf>\s+not recognized\b)|$)',
               re.IGNORECASE),
    # date_trunc() and date_part() with a unit from a column.
    re.compile(r'(?P<pre>\bunit\s+)"(?P<v>.*?)(?:"(?P<suf>\s+not (?:recognized|supported)\b)|$)',
               re.IGNORECASE),
    # jsonpath `.integer()` and the like on a string item.
    re.compile(r'(?P<pre>\bargument\s+)"(?P<v>.*?)(?:"(?P<suf>\s+of jsonpath item method\b)|$)',
               re.IGNORECASE),
    re.compile(r'(?P<pre>\blog format\s+)"(?P<v>.*?)(?:"(?P<suf>\s+is not supported\b)|$)',
               re.IGNORECASE),
    # pgcrypto: `Cannot use "...": No such hash algorithm`.
    re.compile(r'(?P<pre>\bCannot use\s+)"(?P<v>.*?)(?:"(?P<suf>:\s*No such\b)|$)',
               re.IGNORECASE),
    re.compile(r'(?P<pre>\binvalid symbol\s+)"(?P<v>.*?)(?:"(?P<suf>\s+found while decoding\b)|$)',
               re.IGNORECASE),
    re.compile(r'(?P<pre>\bsyntax error in hstore, near\s+)"(?P<v>.*?)'
               r'(?:"(?P<suf>\s+at position\b)|$)', re.IGNORECASE),
    # to_ascii(): `Jordan Ray is not a valid encoding name`, no quotes at all.
    re.compile(r'(?P<pre>^)(?P<v>.+?)(?P<suf>\s+is not a valid encoding name\b)',
               re.IGNORECASE),
    re.compile(r'(?P<pre>\bcould not (?:stat|open|read) (?:file|directory)\s+)"(?P<v>.*?)'
               r'(?:"(?P<suf>:)|$)', re.IGNORECASE),
    # convert_to(): one character of the value, as bytes.
    re.compile(r'(?P<pre>\bcharacter with byte sequence\s+)(?P<v>.+?)'
               r'(?:(?P<suf>\s+in encoding\b)|$)', re.IGNORECASE),
    # psycopg, when it cannot build a Python value from a column.
    re.compile(r"(?P<pre>\b(?:timestamp|date|time) too (?:large|small)\b[^:]{0,60}:\s*)"
               r"'(?P<v>.*?)'?$", re.IGNORECASE),
    # `can't parse time '24:00:00': hour must be in 0..23`. The text after the
    # value repeats parts of it, so everything after the quote goes.
    re.compile(r"(?P<pre>\bcan't parse (?:date|time|timetz|timestamp|timestamptz|interval|"
               r"month):?\s+)'(?P<v>.*)$", re.IGNORECASE),
    # SQL Server.
    re.compile(r"(?P<pre>\bConversion failed when converting the \w+ value\s+)"
               r"'(?P<v>.*?)(?:'(?P<suf>\s+to data type\b)|$)", re.IGNORECASE),
    re.compile(r"(?P<pre>\bThe conversion of the \w+ value\s+)"
               r"'(?P<v>.*?)(?:'(?P<suf>\s+overflowed\b)|$)", re.IGNORECASE),
    re.compile(r"(?P<pre>\bThe duplicate key value is\s*)\((?P<v>.*?)"
               r"(?:\)(?P<suf>(?=\.(?:\s|\(|$)|$))|$)", re.IGNORECASE),
    re.compile(r"(?P<pre>\bTruncated value:\s*)'(?P<v>.*?)(?:'(?P<suf>(?=\.|\s|$))|$)",
               re.IGNORECASE),
    # PARSE(): `Error converting string value 'X' into data type int using culture ''`.
    re.compile(r"(?P<pre>\bError converting string value\s+)"
               r"'(?P<v>.*?)(?:'(?P<suf>\s+into data type\b)|$)", re.IGNORECASE),
    # FORMAT(), PARSE() and AT TIME ZONE with a culture or a zone from a column.
    re.compile(r"(?P<pre>\bThe (?:culture|time zone) parameter\s+)"
               r"'(?P<v>.*?)(?:'(?P<suf>\s+provided\b)|$)", re.IGNORECASE),
    # `Arithmetic overflow error for data type tinyint, value = 1234567.`
    re.compile(r"(?P<pre>\bArithmetic overflow error for data type \w+, value =\s*)"
               r"(?P<v>.+?)(?P<suf>\.?)$", re.IGNORECASE),
    # CONVERT() with a style from a column: `1234567 is not a valid style number
    # when converting ...`, `The style 1234567 is not supported for ...`.
    re.compile(r"(?P<pre>(?:^|\s))(?P<v>\S+)(?P<suf>\s+is not a valid style number\b)",
               re.IGNORECASE),
    re.compile(r"(?P<pre>\bThe style\s+)(?P<v>\S+)(?P<suf>\s+is not supported for conversions\b)",
               re.IGNORECASE),
    # hierarchyid and geography parse the text in .NET and echo it.
    re.compile(r"(?P<pre>\bthe input string\s+)'(?P<v>.*?)(?:'(?P<suf>\s+is not a valid\b)|$)",
               re.IGNORECASE),
    re.compile(r"(?P<pre>\bThe label\s+)(?P<v>.*?)"
               r"(?:(?P<suf>\s+in the input well-known text\b)|$)", re.IGNORECASE),
    re.compile(r'(?P<pre>\bThe invalid escape character\s+)"(?P<v>.*?)'
               r'(?:"(?P<suf>\s+was specified\b)|$)', re.IGNORECASE),
    # JSON functions: `Unexpected character 'Z' is found at position 0.`
    re.compile(r"(?P<pre>\bUnexpected character\s+)'(?P<v>.*?)"
               r"(?:'(?P<suf>\s+is found at position\b)|$)", re.IGNORECASE),
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

# Messages that echo a NAME. A name is what the requester typed, and they cannot
# fix a typo without it. But a cast to regclass, current_setting() or
# query_to_xml() with a column as the argument puts a VALUE where the name
# goes: `SELECT full_name::regclass FROM customers` fails with `relation
# "jordan ray" does not exist`. So a name that is not in the requester's SQL is
# hidden, and a name that is in it stays. The comparison ignores case, double
# quotes and brackets, because the server folds and re-quotes an identifier.
# Measured on the 265 errors stored in 180 days: all 58 messages of these
# shapes named something that was in the SQL. A name from the body of a
# function the requester called is not in the SQL, so it is hidden with them.
# `operator does not exist: text = integer` is not here: its names are types,
# and they are never in the SQL as written.
_NAME_ECHO_PATTERNS = [
    re.compile(r'(?P<pre>\b(?:relation|column|type|schema|role|database|function|'
               r'collation|language|tablespace|server|foreign-data wrapper|cursor|'
               r'text search (?:configuration|dictionary|parser|template))\s+)'
               r'"(?P<v>.*?)(?:"(?P<suf>\s+(?:of relation\b|for encoding\b|does not exist\b))|$)',
               re.IGNORECASE),
    re.compile(r'(?P<pre>\bunrecognized configuration parameter\s+)"(?P<v>.*?)"?$',
               re.IGNORECASE),
    # Also from query_to_xml() of a column, and from jsonpath input: `syntax
    # error at or near " " of jsonpath input`.
    re.compile(r'(?P<pre>\bsyntax error at or near\s+)"(?P<v>.*?)(?:"(?=\s|$)|$)',
               re.IGNORECASE),
    # A cast to regoper: `operator does not exist: jordan-ray`. The type
    # mismatch `operator does not exist: text = integer` has spaces, this has
    # none (a name with a space is `invalid name syntax` instead).
    re.compile(r'(?P<pre>\boperator does not exist:\s*)(?P<v>\S+)$', re.IGNORECASE),
    # SQL Server.
    re.compile(r"(?P<pre>\b(?:Invalid object name|Invalid column name|"
               r"Could not find stored procedure|Incorrect syntax near|The object|"
               r"No item by the name of)\s+)'(?P<v>.*?)(?:'(?=\.|\s|$)|$)", re.IGNORECASE),
    # `Database 'x' does not exist.` starts the message. The `in database 'x'`
    # of sp_help names the database of the request, and stays.
    re.compile(r"(?P<pre>^Database\s+)'(?P<v>.*?)(?:'(?P<suf>\s+does not exist\b)|$)",
               re.IGNORECASE),
]

# pyodbc gives its message as the repr of a tuple: `('42000', "[42000]
# [Microsoft]...message. (102) (SQLExecDirectW)")`, and the executor passes it
# as `ProgrammingError: ('42000', ...)`. The repr writes a quote inside the
# message as `\'`, which breaks a pattern that looks for the quotes around a
# value. So the code takes the message out of the tuple first.
_DRIVER_TUPLE = re.compile(r"^(?:\w+: )?(\('[0-9A-Za-z]{5}', .*\))$", re.DOTALL)


def _unwrap_driver_tuple(text: str) -> str:
    """Return the message inside a pyodbc error tuple, or `text` unchanged."""
    m = _DRIVER_TUPLE.match(text)
    if not m:
        return text
    try:
        parts = ast.literal_eval(m.group(1))
    except Exception:
        return text
    if (isinstance(parts, tuple) and len(parts) == 2
            and all(isinstance(p, str) for p in parts)):
        return parts[1]
    return text


_QUOTES = str.maketrans("", "", '"[]`')


def _bare(name: str) -> str:
    """Normalise a name for the comparison with the SQL: no quotes, one case."""
    return " ".join(name.translate(_QUOTES).split()).lower()


def _hide_data_values(msg: str, query: str | None) -> str:
    """Replace each value a database echoed from its own data with `[hidden]`.

    A value that appears in `query` stays. The requester typed it, so showing it
    tells them nothing new, and it is how they find the bad literal in a long
    IN list. Whitespace counts as one space on both sides, because the message
    is already collapsed.

    A name (`_NAME_ECHO_PATTERNS`) is hidden only when `query` is given and
    does not contain it. Without the SQL there is nothing to compare with, and
    the callers without it (connection tests, catalog reads) run no SQL that a
    requester wrote. A connection failure (`FATAL`) names the database of the
    request, not a value, so the rule skips it.
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

    if query is None or re.search(r"\bFATAL\b", msg):
        return msg
    bare_query = _bare(query)

    def _swap_name(m: re.Match[str]) -> str:
        name = _bare(m.group("v"))
        if not name or name in bare_query:
            return m.group(0)
        groups = m.groupdict()
        return f"{groups.get('pre') or ''}{_HIDDEN}{groups.get('suf') or ''}"

    for pat in _NAME_ECHO_PATTERNS:
        msg = pat.sub(_swap_name, msg)
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
    the requester wrote as `query`: a value found in it stays visible, and a
    name that is not in it is hidden too (see `_NAME_ECHO_PATTERNS`). Pass
    `requester_data=True` where the values came from the requester's own upload,
    such as a CSV import, and nothing is hidden."""
    text = _unwrap_driver_tuple(str(raw).strip())
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

    # SQL Server (pyodbc/ODBC) envelope: drop the SQLSTATE (`[42000]`), the
    # driver bracket prefixes (`[Microsoft][ODBC Driver 18 for SQL Server][SQL
    # Server]`) and the trailing `(NNN) (SQLExecDirectW)` code tuple, leaving
    # the core message.
    msg = re.sub(r"^\[[0-9A-Z]{5}\]\s*", "", msg)
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
