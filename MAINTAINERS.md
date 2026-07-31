# Maintainers

## Today

**One maintainer.** Every commit in this repository is from the same person, who
also runs QueryHub in production and is therefore the first to notice when it
breaks. That is worth stating plainly rather than leaving an evaluator to work it
out from `git log`: if you are deciding whether to put this between your
developers and your production databases, the bus factor is one, and you should
weigh that against what the alternatives offer.

What that means in practice:

- **Security reports get answered.** See [SECURITY.md](SECURITY.md) for the
  private channel and the response commitment.
- **Issues and pull requests get read**, but there is no SLA. A well-scoped PR
  with a test lands faster than an issue describing the same thing.
- **The audit contract does not change on a whim.** Everything else pre-1.0
  might; see the versioning section of [README.md](README.md#versioning-and-support).

## Areas, and where help is genuinely wanted

Not a wish list — these are the parts where a second pair of eyes would change
the product, in rough order of value:

| Area | Files | What is wanted |
| --- | --- | --- |
| **Engine layer** | `engines.py`, `mssql_exec.py`, `ast_safety.py` | A MySQL/MariaDB spec against the existing contract is the single most-requested capability. The contract is small and documented; the work is a dialect, a keyword classification and a driver path. |
| **SQL safety** | `query_safety.py`, `tests/corpus/` | Adversarial review. Every payload that defeats a guard is worth more than a feature. The corpus is designed to be appended to. |
| **Web surface** | `src/queryhub/web/`, `QueryHubWeb/` | Accessibility, keyboard paths, and reviewing the React for the things a single author stops seeing. |
| **Packaging & deployment** | `Dockerfile`, `docker-compose.yml`, `deploy/` | Kubernetes/Helm does not exist. Neither does a published image. |
| **PII detection** | `pii.py` | Region packs. The generic pack (IBAN, card, email, E.164) plus one country's identifiers is all there is; adding a region is a data file and tests. |

Deliberately **not** listed as a first contribution: splitting
`slack_app/handlers.py`. It is a large file and it will get restructured, but it
is the worst possible place to start and would burn whoever took it.

## If this project stops

Also worth writing down, since the honest answer is short:

- It is Apache-2.0, with no CLA and no copyright assignment. Fork it.
- There is no hosted service, no license server, and no phone-home, so nothing
  stops working when the maintainer does. A running install keeps running.
- The data is in your own Postgres, in a documented schema
  ([docs/SCHEMA.md](docs/SCHEMA.md)), and the migrations are plain SQL. Nothing
  is locked in a proprietary format.
- Recovery and key custody are documented independently of the maintainer:
  [docs/DISASTER_RECOVERY.md](docs/DISASTER_RECOVERY.md).

## Becoming a maintainer

There is no committee to petition. Send a few good pull requests in one area,
then ask. Review rights for an area follow demonstrated judgement in it — which
in a security tool means catching problems, not just adding features.
