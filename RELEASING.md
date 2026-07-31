# Releasing

The whole sequence, so that a release is a checklist rather than a memory
exercise, and so anyone who inherits the project can cut one.

## Before you tag

Everything here is also enforced by CI, so a green build on `main` covers most of
it. Run the three that CI cannot:

```bash
# 1. The package. Checks the BUILT sdist, in both directions — nothing licensed
#    or internal in it, nothing load-bearing missing from it.
python scripts/check_sdist_clean.py

# 2. The demo stack, which is the first thing a new user runs.
docker compose up --build -d
python scripts/ci_demo_roundtrip.py --base-url http://localhost:8080
docker compose down -v

# 3. The frontend actually builds from a clean tree (no stale dist).
git clean -xdn QueryHubWeb/app          # review, then -xdf if you mean it
(cd QueryHubWeb/app && npm ci && npm run build)
```

Then the content:

- [ ] `CHANGELOG.md` has a section for this version, written for someone
      upgrading — what changed, what breaks, what to do about it. Not a commit
      dump.
- [ ] Any migration added since the last release is idempotent and re-runnable
      (`python scripts/apply_migrations.py --dry-run` twice in a row on a restored
      copy of a real database, not an empty one).
- [ ] Anything in `docs/KNOWN_LIMITATIONS.md` that this release fixes is removed
      from it, and anything it introduces is added.
- [ ] The version in `pyproject.toml` matches the tag you are about to push.

## Tag and publish

```bash
# The tag is the trigger; the workflow does the rest.
git tag -s v0.2.0 -m "QueryHub v0.2.0"
git push origin v0.2.0
```

`.github/workflows/release.yml` then:

1. re-runs the full test suite and the vanilla-import gate — a tag is not
   exempt from CI,
2. runs `scripts/check_sdist_clean.py`,
3. builds the sdist and the wheel,
4. creates the GitHub Release with the CHANGELOG section for that version and
   attaches both artifacts,
5. publishes to PyPI **if** trusted publishing is configured (see below).

Tags are signed. `git tag -s` needs a signing key configured; an unsigned tag
for a security tool is a bad look and the workflow does not care either way, so
this is on you.

## PyPI: not configured yet

The workflow's publish step is opt-in and will be skipped until someone sets it
up. When you do, use **trusted publishing** (OIDC) rather than an API token —
GitHub exchanges a short-lived token per run, so there is no long-lived secret in
the repository to leak:

1. Reserve the project name on PyPI.
2. In the PyPI project settings, add a trusted publisher: this repository, the
   workflow filename `release.yml`, and the environment name `pypi`.
3. Create a GitHub environment called `pypi` (optionally with a required
   reviewer, which makes publishing a deliberate act).
4. Remove the `if: false` guard on the publish job.

Until then, the GitHub Release with attached artifacts *is* the release, and
`pip install git+https://...@v0.2.0` works.

## Versioning

See [README.md](README.md#versioning-and-support) for what the major version
does and does not guarantee, and for the support window. Two rules that live here because they
constrain what a release may contain:

- **Migrations are append-only.** A released migration is never edited, only
  superseded. The ledger stores a checksum precisely so an edit is caught.
- **The audit contract does not break in a minor release.** If a change would
  make an old `audit_log` row unreadable or ambiguous, it is a major version and
  needs a documented migration path for the historical rows.

## Container image (GHCR)

The `publish-image` job in `.github/workflows/release.yml` builds and pushes
`ghcr.io/<owner>/<repo>` on every `v*` tag: the version tag plus `latest`, for
`linux/amd64` and `linux/arm64`, using `GITHUB_TOKEN` — no registry secret in the
repository. It smoke-tests the pushed image by constructing the app inside it,
because publishing an image that cannot start fails for the user rather than for
us.

- [ ] First release only: the GHCR package is created private. Make it public in
      the repository's Packages settings, or `docker pull` fails for everyone
      with an authentication error that looks like the image does not exist.
- [ ] Update the example image tag in `README.md`, `.env.example` and
      `docker-compose.install.yml` to this version.
      `tests/test_release_docs.py` fails if you forget, which is how this
      became a checklist item: the README went on advertising 1.0.0 after
      1.0.1 shipped. Naming a version rather than `latest` is deliberate —
      see the next box — so the tag has to be maintained, not removed.
- [ ] Reference the **version** tag in any deployment, not `latest`. A SQL
      gateway should not change underneath you because a tag moved.

## After the release

- [ ] Bump `pyproject.toml` to the next `-dev` version so `main` is never
      mistaken for the release.
- [ ] Add an `## Unreleased` heading back to `CHANGELOG.md`.
- [ ] If the release changes anything an operator must do (a new required config
      key, a manual step), say so at the top of the release notes rather than in
      the middle of a list.
