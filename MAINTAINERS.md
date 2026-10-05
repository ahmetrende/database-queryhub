# Maintainers

## Today

**One maintainer.** The same person made every commit in this repository. That
person also runs QueryHub in production, so they are the first to notice when it
breaks. It is worth saying this plainly, so that an evaluator does not have to
deduce it from `git log`. The bus factor is one. If you consider putting this
between your developers and your production databases, you should weigh that
against what the alternatives offer.

What that means in practice:

- **The maintainer answers security reports.** See [SECURITY.md](SECURITY.md)
  for the private channel and the response commitment.
- **The maintainer reads issues and pull requests**, but there is no SLA. A
  well-scoped PR with a test lands faster than an issue that describes the same
  thing.
- **The audit contract does not change without a good reason.** Before 1.0,
  everything else might change. See the versioning section of
  [README.md](README.md#versioning-and-support).

## Areas, and where help is genuinely wanted

These are the parts where a second reviewer would change the product, in rough
order of value. This is not a wish list:

| Area | Files | What is wanted |
| --- | --- | --- |
| **Engine layer** | `engines.py`, `mssql_exec.py`, `ast_safety.py` | A MySQL/MariaDB spec against the existing contract is the single most-requested capability. The contract is small and documented. The work is a dialect, a keyword classification and a driver path. |
| **SQL safety** | `query_safety.py`, `tests/corpus/` | Adversarial review. Every payload that defeats a guard is worth more than a feature. By design, you can append entries to the corpus. |
| **Web surface** | `src/queryhub/web/`, `QueryHubWeb/` | Accessibility, keyboard paths, and reviewing the React code for the things that a single author stops seeing. |
| **Packaging & deployment** | `Dockerfile`, `docker-compose.yml`, `deploy/` | Kubernetes/Helm does not exist. Each release publishes a container image to GHCR. |
| **PII detection** | `pii.py` | Region packs. The generic pack (IBAN, card, email, E.164) plus the identifiers of one country is all that exists. A new region is a data file and tests. |

Deliberately **not** listed as a first contribution: splitting
`slack_app/handlers.py`. It is a large file, and the project will restructure it.
But it is the worst possible place to start, and it would exhaust whoever took
it.

## If this project stops

The honest answer is short, so it is worth writing down:

- The license is Apache-2.0, with no CLA and no copyright assignment. Fork it.
- There is no hosted service, no license server, and no phone-home, so nothing
  stops working when the maintainer stops. A running install keeps running.
- The data is in your own Postgres, in a documented schema
  ([docs/SCHEMA.md](docs/SCHEMA.md)). The migrations are plain SQL. Nothing
  uses a proprietary format.
- [docs/DISASTER_RECOVERY.md](docs/DISASTER_RECOVERY.md) documents recovery
  and key custody, independently of the maintainer.

## Becoming a maintainer

Send a few good pull requests in one area. Then ask. There is no committee to
petition. Review rights for an area follow demonstrated judgement in that area.
In a security tool, that judgement means catching problems, not just adding
features.
