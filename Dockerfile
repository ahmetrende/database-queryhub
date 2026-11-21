# QueryHub in a container: one image for every process.
#
# Two stages so the runtime image carries no Node and no build tooling. The
# frontend is built HERE, at image build time: a fresh clone that skipped
# `npm run build` used to serve nothing, so the build cannot be optional.
#
# The default command runs the web app. The same image also runs the Slack bot
# (`python -m queryhub.main`) and every scheduled script under scripts/.
# The image carries the Python extras of a full install (Slack, SQL Server,
# AWS, ClickHouse), and the environment chooses the profile at run time: with
# no SLACK_BOT_TOKEN the web app runs the vanilla profile.
#
# Kubernetes: deploy/helm/queryhub runs this image. Read deploy/KUBERNETES.md.

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
FROM node:26-alpine@sha256:aadf416b2cdce311a8811ba3f0608a61b77dbf997500e2eafe781b51f6a0b019 AS frontend
WORKDIR /build

# The build reads QueryHub.html (design CSS) and the shared .jsx files through
# symlinks in app/src, so the whole QueryHubWeb tree has to be present.
COPY QueryHubWeb/ ./QueryHubWeb/
WORKDIR /build/QueryHubWeb/app
RUN npm ci --no-audit --no-fund && npm run build

# ---------------------------------------------------------------- runtime
FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS runtime

# libpq for psycopg and the unixODBC library for pyodbc. No compiler: every
# locked package ships wheels.
#
# QH_MSSQL_DRIVER=1 also installs Microsoft's ODBC Driver 18 for SQL Server,
# which a SQL Server target needs. It is off by default because the driver is
# not open source: it carries a Microsoft EULA, and the build accepts that
# EULA (ACCEPT_EULA=Y) when you turn it on. See THIRD_PARTY_NOTICES.md.
ARG QH_MSSQL_DRIVER=0
RUN set -eu; \
    apt-get update; \
    apt-get install -y --no-install-recommends libpq5 libodbc2; \
    if [ "$QH_MSSQL_DRIVER" = "1" ]; then \
        apt-get install -y --no-install-recommends ca-certificates curl; \
        . /etc/os-release; \
        curl -fsSL -o /tmp/packages-microsoft-prod.deb \
            "https://packages.microsoft.com/config/debian/${VERSION_ID}/packages-microsoft-prod.deb"; \
        dpkg -i /tmp/packages-microsoft-prod.deb; \
        rm /tmp/packages-microsoft-prod.deb; \
        apt-get update; \
        ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18; \
        apt-get purge -y curl; \
        apt-get autoremove -y; \
    fi; \
    rm -rf /var/lib/apt/lists/*

# Run as a non-root user. The master key lives on a volume this user owns.
RUN useradd --create-home --uid 10001 queryhub
WORKDIR /app

# Dependencies first, from the hash lock (scripts/lock_image_deps.sh). Every
# file is checked against a recorded hash, and two builds of one commit install
# the same wheels. Resolving the ranges in pyproject.toml at build time gave
# neither. Its own layer, so a source change does not reinstall them.
COPY docker/requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock \
 && rm /tmp/requirements.lock

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

# --no-deps: everything it needs is installed above. --no-build-isolation: the
# build backend is in the lock too, so the build fetches nothing unlocked.
RUN pip install --no-cache-dir --no-deps --no-build-isolation . \
 && mkdir -p /var/lib/queryhub/results /var/lib/queryhub/imports /etc/queryhub \
 && chown -R queryhub:queryhub /app /var/lib/queryhub /etc/queryhub

COPY docker/entrypoint.sh /usr/local/bin/queryhub-entrypoint
RUN chmod +x /usr/local/bin/queryhub-entrypoint

USER queryhub
ENV PYTHONUNBUFFERED=1 \
    MASTER_KEY_PATH=/etc/queryhub/master.key \
    QH_RESULTS_DIR=/var/lib/queryhub/results \
    QH_IMPORTS_DIR=/var/lib/queryhub/imports \
    QH_WEB_STATIC_DIR=/app/QueryHubWeb/app/dist

# Build identity for the build stamp, since the installed package has no .git
# to ask (web/build_info.py). Late on purpose: a new value rebuilds nothing
# above this line. The image digest cannot be known here; set QH_IMAGE_DIGEST
# when running the image to show it too.
ARG QH_BUILD_SHA=""
ARG QH_BUILD_VERSION=""
ENV QH_BUILD_SHA=${QH_BUILD_SHA} \
    QH_BUILD_VERSION=${QH_BUILD_VERSION}

EXPOSE 8080
# The image has a health endpoint, so use it rather than guessing at readiness.
HEALTHCHECK --interval=10s --timeout=3s --start-period=40s --retries=6 \
    CMD python -c "import urllib.request,sys; \
        sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).status == 200 else 1)"

ENTRYPOINT ["queryhub-entrypoint"]
CMD ["python", "-m", "queryhub.web"]
