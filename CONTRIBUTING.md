# Contributing

Contributions should be small and opinionated, like the bot itself. Thanks for
considering a contribution.

## Ways to help

- **Bug reports** — open an issue with the smallest reproducer that you can
  write.
- **Feature requests** — open an issue that describes the use case before you
  write code. We may already have a planned approach in
  `docs/OPERATIONS.md` or in the migration history.
- **Pull requests** — see below.

## Pull requests

- Open one PR per change. We review small, focused diffs faster.
- Bash / Python / SQL: follow the style of the files that you edit.
  Python is PEP-8-ish. Comments explain *why*. Code explains *what*.
- Update `docs/OPERATIONS.md` and `docs/SCHEMA.md` if your change
  affects bot behavior or the DB schema.
- Migrations are append-only, numbered sequentially (`migrations/NNN_*.sql`),
  and idempotent (`CREATE ... IF NOT EXISTS`, `INSERT ... ON CONFLICT DO NOTHING`,
  etc.). Never edit a committed migration. Add a new one.
- Commit messages: write an imperative subject ("fix CSV streaming
  truncation"). In the body, explain the *why* and any user-visible impact.

## Local dev

One command starts the same stack that CI runs:

```bash
docker compose up            # app + metadata DB + a seeded demo target
```

Sign in at http://localhost:8080 as `demo-admin` / `queryhub-demo`. The demo
target holds ~10k generated rows and the three per-tier roles. With it, you can
try the whole submit → approve → execute → masked-result path with no
provisioning. `scripts/ci_demo_roundtrip.py` runs that path end-to-end. It is the
fastest way to check that a change did not break the path.

For the Python side without containers:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,slack]"
pytest                       # ~7s, no database needed
ruff check src tests scripts
python scripts/check_mypy_baseline.py   # fails on NEW mypy errors only
```

**Changing a Python dependency.** The container image installs
`docker/requirements.lock`, not the ranges in `pyproject.toml`. The lock pins
every package with its hashes. After you edit a dependency in `pyproject.toml`:

1. Run `scripts/lock_image_deps.sh`. It needs [uv](https://docs.astral.sh/uv/).
2. Commit the lock in the same change.

`tests/test_image_dependency_lock.py` fails until the two agree. The release
workflow audits the lock with `pip-audit`, and it stops on a known
vulnerability.

The suite is hermetic by construction. A unit test that tries to open a real
database connection fails with a named error. It does not hang. If you need a
real connection, mark the test `@pytest.mark.integration`. Then run the suite
with `QH_RUN_INTEGRATION=1`.

Frontend:

```bash
cd QueryHubWeb/app && npm ci && npm run build
```

`src/index.css` is generated from `QueryHub.html`'s `<style>` plus
`src/index.local.css`. Edit those two sources, not the generated file.
`deploy/INSTALL.md` covers a real install. You do not need a real install to
contribute.

## Good first contributions

These are things that the project actually wants. This is not a wish list. Each
one is real and self-contained, and each one lands as a small PR:

1. **A `pii_region` pack for one more country.** `pii.py` has a generic pack
   (IBAN, card, email, E.164) and one region. A new region is a detector plus
   tests. The checksum algorithm is the interesting part.
2. **A MySQL/MariaDB engine spec.** `engines.py` documents the contract.
   `mssql_exec.py` is the worked example of a non-Postgres engine. The safety
   spec (dialect, keyword classification, dangerous functions) can land before
   the driver path. It is useful on its own.
3. **More tautology corpus entries.** By design, you can append entries to
   `tests/corpus/tautologies.txt`. Find a WHERE clause that cannot filter rows
   and that the checks do not catch yet. That is a genuinely valuable PR.
4. **Timezone hardcoding.** A single timezone appears as a default in several code
   paths and in operator SQL snippets in `docs/OPERATIONS.md`. Make them read the
   configured display timezone.
5. **Grid keyboard paths.** The result grid now supports arrow-key navigation.
   Column resize and sort are still mouse-only.
6. **`docs/SCHEMA.md` drift.** We regenerate it by hand. A script that emits it
   from the live schema would be welcome, so that it cannot become stale again.

Deliberately **not** on this list: splitting `slack_app/handlers.py`. It is a
large file, and we will restructure it. But it is the worst possible first
contribution.

## Security

If you find a security issue, report it as SECURITY.md describes, through GitHub's
private vulnerability reporting. Do not open a public issue. Sensitive concerns include:

- SQL injection / sandbox escape in `query_safety.py`
- Authz bypass (admin / bypass / team grants)
- Secret handling in `crypto.py` or `secrets_store.py`

## License

When you submit a contribution, you agree that your code is licensed under
the [Apache License 2.0](LICENSE) of this project
(`SPDX-License-Identifier: Apache-2.0`).
