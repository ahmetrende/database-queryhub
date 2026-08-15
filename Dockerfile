# QueryHub — web (vanilla) profile in a container.
#
# Two stages so the runtime image carries no Node and no build tooling. The
# frontend is built HERE, at image build time: a fresh clone that skipped
# `npm run build` used to serve nothing, so the build cannot be optional.
#
# The image runs the web app only — the vanilla profile, which is the whole
# product without Slack. Add `[slack]` and run `python -m queryhub.main`
# alongside it if you want the Slack surface.

# ---------------------------------------------------------------- frontend
#
# Base images are pinned BY DIGEST as well as by tag. A tag is mutable: the
# `python:3.12-slim` that built the image you audited is not necessarily the one
# that builds it tomorrow, which makes a build unreproducible and a supply-chain
# swap invisible.
#
# The obvious objection to pinning is that it freezes base-image CVEs until
# somebody bumps the pin — which would be a worse trade for a security tool than
# a mutable tag. That is why `.github/dependabot.yml` now watches the `docker`
# ecosystem: the tag stays in the FROM line precisely so Dependabot recognises
# it and opens a PR when the digest moves. Pin without that and you have simply
# chosen to stop receiving patches.
#
# Digests resolved 2026-07-25. They are the multi-arch INDEX digests, not a
# single platform's manifest, so an arm64 build still works.
FROM node:25-alpine@sha256:bdf2cca6fe3dabd014ea60163eca3f0f7015fbd5c7ee1b0e9ccb4ced6eb02ef4 AS frontend
WORKDIR /build

# The build reads QueryHub.html (design CSS) and the shared .jsx files through
# symlinks in app/src, so the whole QueryHubWeb tree has to be present.
COPY QueryHubWeb/ ./QueryHubWeb/
WORKDIR /build/QueryHubWeb/app
RUN npm ci --no-audit --no-fund && npm run build

# ---------------------------------------------------------------- runtime
FROM python:3.14-slim@sha256:cea0e6040540fb2b965b6e7fb5ffa00871e632eef63719f0ea54bca189ce14a6 AS runtime

# libpq for psycopg, and nothing else. No compiler: psycopg[binary] ships wheels.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libpq5 \
 && rm -rf /var/lib/apt/lists/*

# Run as a non-root user. The master key lives on a volume this user owns.
RUN useradd --create-home --uid 10001 queryhub
WORKDIR /app

# THIRD_PARTY_NOTICES.md is in pyproject's license-files. Without it here,
# setuptools SILENTLY skips it — the build still succeeds and the image ships
# with no LGPL notice for psycopg, which is precisely the redistribution case
# where that notice matters. Verified by looking inside the built image.
COPY pyproject.toml MANIFEST.in README.md LICENSE NOTICE THIRD_PARTY_NOTICES.md ./
COPY src/ ./src/
COPY migrations/ ./migrations/
COPY scripts/ ./scripts/
COPY deploy/ ./deploy/
COPY QueryHubWeb/ ./QueryHubWeb/
COPY --from=frontend /build/QueryHubWeb/app/dist ./QueryHubWeb/app/dist

RUN pip install --no-cache-dir . \
 && mkdir -p /var/lib/queryhub /etc/queryhub \
 && chown -R queryhub:queryhub /app /var/lib/queryhub /etc/queryhub

COPY docker/entrypoint.sh /usr/local/bin/queryhub-entrypoint
RUN chmod +x /usr/local/bin/queryhub-entrypoint

USER queryhub
ENV PYTHONUNBUFFERED=1 \
    MASTER_KEY_PATH=/etc/queryhub/master.key \
    QH_RESULTS_DIR=/var/lib/queryhub/results \
    QH_WEB_STATIC_DIR=/app/QueryHubWeb/app/dist

EXPOSE 8080
# The image has a health endpoint, so use it rather than guessing at readiness.
HEALTHCHECK --interval=10s --timeout=3s --start-period=40s --retries=6 \
    CMD python -c "import urllib.request,sys; \
        sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).status == 200 else 1)"

ENTRYPOINT ["queryhub-entrypoint"]
CMD ["python", "-m", "queryhub.web"]
