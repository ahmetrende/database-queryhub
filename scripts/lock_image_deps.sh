#!/usr/bin/env bash
# Regenerate docker/requirements.lock: every package the container image
# installs, pinned, with the hash of every file PyPI has for it.
#
# The image used to resolve `pip install .` against the ranges in
# pyproject.toml at build time, so two builds of one commit could ship
# different wheels, and nothing checked what was downloaded. The Dockerfile now
# installs this file with --require-hashes, and then the package itself with
# --no-deps.
#
# Run it after changing a dependency in pyproject.toml, and commit the result
# in the same change; tests/test_image_dependency_lock.py fails until the two
# agree. `--universal` keeps both image architectures (amd64, arm64) covered,
# and the Python version follows the Dockerfile's runtime base image.
set -euo pipefail
cd "$(dirname "$0")/.."
py=$(sed -n 's/^FROM python:\([0-9.]*\)-slim.* AS runtime$/\1/p' Dockerfile)
[ -n "$py" ] || { echo "cannot read the runtime Python version from the Dockerfile" >&2; exit 2; }
# The image carries the extras that a full install uses (Slack, SQL Server,
# AWS Secrets Manager, ClickHouse), so one image serves every profile: the
# profile is chosen at run time by the environment, not at build time.
uv pip compile pyproject.toml docker/build-backend.in \
  --extra slack --extra mssql --extra aws --extra clickhouse \
  --generate-hashes --universal --python-version "$py" \
  --output-file docker/requirements.lock
