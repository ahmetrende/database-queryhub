# Releasing

This is the whole sequence. It makes a release a checklist, not a memory
exercise. Anyone who inherits the project can cut a release with it.

## Before you tag

**A green CI run on the commit that you are about to tag covers the three checks
below.** CI runs all three on every push to `main`:

- the sdist check, in each `test` job
- the `demo-stack` job (the round trip, with the masked result asserted)
- the `frontend` job (a clean `npm ci` + build)

Look before you tag: `gh run list --commit <full sha>`. A short sha matches
nothing. Run the checks by hand only where CI cannot reach:

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

Then check the content:

- [ ] `CHANGELOG.md` has a section for this version, written for someone who
      upgrades: what changed, what breaks, and what to do about it. It is not a
      commit dump.
- [ ] Any migration added since the last release is idempotent and re-runnable.
      To check, run `python scripts/apply_migrations.py --dry-run` twice in a
      row on a restored copy of a real database, not an empty one.
- [ ] `docs/KNOWN_LIMITATIONS.md` no longer lists what this release fixes, and
      it lists what this release introduces.
- [ ] The version in `pyproject.toml` matches the tag that you are about to push.

## Tag and publish

```bash
# The tag is the trigger; the workflow does the rest.
git tag -s v0.2.0 -m "QueryHub v0.2.0"
git push origin v0.2.0
```

`.github/workflows/release.yml` then does these steps:

1. It re-runs the full test suite and the vanilla-import gate. A tag is not
   exempt from CI.
2. It runs `scripts/check_sdist_clean.py`.
3. It builds the sdist and the wheel.
4. It creates the GitHub Release with the CHANGELOG section for that version,
   and it attaches both artifacts.
5. It publishes to PyPI, **if** trusted publishing is configured (see below).

This project signs its tags. `git tag -s` needs a configured signing key. An
unsigned tag for a security tool looks bad, and the workflow accepts signed and
unsigned tags alike. So the signature is your responsibility.

## PyPI: not configured yet

The publish step of the workflow is opt-in. The workflow skips it until someone
configures it. When you configure it, use **trusted publishing** (OIDC), not an
API token. GitHub exchanges a short-lived token per run, so the repository holds
no long-lived secret that can leak:

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
does and does not guarantee, and for the support window. Two rules live here,
because they constrain what a release may contain:

- **Migrations are append-only.** You never edit a released migration. You only
  supersede it. The ledger stores a checksum precisely so that an edit is caught.
- **The audit contract does not break in a minor release.** If a change would
  make an old `audit_log` row unreadable or ambiguous, it is a major version. It
  also needs a documented migration path for the historical rows.

## Container image (GHCR)

The `publish-image` job in `.github/workflows/release.yml` builds and pushes
`ghcr.io/<owner>/<repo>` on every `v*` tag. It pushes the version tag plus
`latest`, for `linux/amd64` and `linux/arm64`. It uses `GITHUB_TOKEN`, so the
repository holds no registry secret. The job smoke-tests the pushed image: it
constructs the app inside the image. The reason: an image that cannot start
fails for the user, not for us.

- [ ] First release only: GHCR creates the package as private. Make it public in
      the Packages settings of the repository. If you do not, `docker pull` fails
      for everyone, with an authentication error that looks like the image does
      not exist.
- [ ] Update the example image tag in `README.md`, `.env.example` and
      `docker-compose.install.yml` to this version.
      If you forget, `tests/test_release_docs.py` fails. That is how this
      became a checklist item: the README continued to advertise 1.0.0 after
      1.0.1 shipped. Naming a version rather than `latest` is deliberate (see
      the next box). So you have to maintain the tag, not remove it.
- [ ] Reference the **version** tag in any deployment, not `latest`. A SQL
      gateway should not change without your knowledge because a tag moved.

## After the release

- [ ] Bump `pyproject.toml` to the next `-dev` version, so that nobody mistakes
      `main` for the release.
- [ ] Add an `## Unreleased` heading to `CHANGELOG.md` again.
- [ ] If the release changes anything that an operator must do, say so at the
      top of the release notes. Do not put it in the middle of a list. Examples:
      a new config key that is required, or a manual step.
