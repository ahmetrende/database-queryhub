# Third-party notices

QueryHub is Apache-2.0 (see [LICENSE](LICENSE)). It depends on, and its
container image redistributes, the software listed here. Nothing below changes
QueryHub's own licence; this file exists so a legal review can answer "what am I
also taking on?" without reading a lockfile.

Everything here was read from installed package metadata rather than typed from
memory, so it matches what actually ships. Versions are the floor each
dependency declares, or the resolved version where that is the point (psycopg).

## The part that matters: psycopg is LGPL-3.0

**`psycopg` 3.x, `psycopg-pool`, `psycopg-binary` — LGPL-3.0-only.**

These are the PostgreSQL driver and its connection pool. They are the only
copyleft components in the runtime path, and they are unavoidable for a
PostgreSQL gateway. What that means in practice:

- QueryHub **imports** psycopg; it does not modify or statically link it, and
  the LGPL's dynamic-use provisions apply to that shape of use. Using QueryHub,
  including inside a company, does not put an obligation on your own code.
- If you **redistribute** QueryHub (your own image, an internal package, a
  fork), the LGPL asks that recipients can replace psycopg with their own build
  of it. Because it is installed as an ordinary Python package, replacing it is
  `pip install psycopg==<your build>` — nothing in QueryHub pins it to a private
  copy or embeds it.
- `psycopg[binary]` additionally ships a **prebuilt libpq** inside the
  `psycopg-binary` wheel (libpq 18 as resolved here). libpq is under the
  PostgreSQL Licence, which is permissive. To avoid the bundled copy entirely,
  install `psycopg` without the `binary` extra and let it use the system libpq —
  QueryHub does not care which it gets.

If LGPL in the dependency tree is disqualifying for you, say so in an issue: the
driver already sits behind an engine seam (`engines.py`), so a permissively
licensed PostgreSQL driver is a spec, not a rewrite.

## Runtime dependencies

| Package | Licence | What it does here |
|---|---|---|
| `psycopg[binary]` | **LGPL-3.0-only** | PostgreSQL driver (see above) |
| `psycopg-pool` | **LGPL-3.0-only** | connection pool for the metadata DB |
| `cryptography` | Apache-2.0 OR BSD-3-Clause | Fernet encryption of stored target credentials |
| `PyJWT[crypto]` | MIT | web session tokens |
| `fastapi` | MIT | the web API |
| `uvicorn[standard]` | BSD-3-Clause | ASGI server |
| `httpx` | BSD-3-Clause | outbound HTTP (OIDC, Slack) |
| `sqlparse` | BSD-3-Clause | statement splitting in the safety pass |
| `sqlglot` | MIT | the AST safety pass and the parser cross-check |
| `openpyxl` | MIT | XLSX result export |
| `python-dotenv` | BSD-3-Clause | env-file loading |

Transitively, `certifi` (CA bundle) and `pathspec` are **MPL-2.0**. MPL is
file-level copyleft and neither is modified here.

## Optional extras

| Extra | Package | Licence |
|---|---|---|
| `slack` | `slack-bolt`, `slack-sdk` | MIT |
| `mssql` | `pyodbc` | MIT |
| `aws` | `boto3` | Apache-2.0 |

`pyodbc` needs Microsoft's ODBC Driver for SQL Server (`msodbcsql18`), which is
**not** installed by QueryHub and is **not** open source — it carries its own
Microsoft EULA, which you accept when you install it. SQL Server support is an
opt-in extra for exactly this reason.

## Development-only

Not shipped in the image, listed for completeness: `pytest`, `pytest-cov`,
`mypy`, `ruff` (MIT) and `hypothesis` (**MPL-2.0**).

## Engine logos

`QueryHubWeb/brand/engines/*.svg` are vendor logos in the shape of the
[Devicon](https://github.com/devicons/devicon) set (MIT). The **marks
themselves are trademarks of their owners** — PostgreSQL (PostgreSQL Community
Association), Microsoft SQL Server (Microsoft), ClickHouse (ClickHouse, Inc.).
They are used to identify which engine a connection speaks, which is nominative
use, and imply no endorsement or affiliation.

The `oracle`, `mysql` and `couchbase` marks were **removed on 2026-08-15**.
QueryHub has no engine spec for those three, so no connection could ever have
displayed them: they were vendor trademarks shipped to advertise capability the
product does not have. ClickHouse stays — that spec is real, even though
execution against it is refused today.

QueryHub's own logo and wordmark in `assets/` are **not** covered by the Apache
licence on the code — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

## How to regenerate this list

```bash
python3 -c "import importlib.metadata as m; print('\n'.join(sorted(f\"{d.metadata['Name']}=={d.version} {d.metadata.get('License-Expression') or ''}\" for d in m.distributions() if d.metadata['Name'])))"
```
