# Install on Kubernetes (Helm)

The chart in `deploy/helm/queryhub` runs QueryHub on a Kubernetes cluster.
It uses one image for the web app, the Slack bot and the scheduled scripts.
For a host install with systemd, read [INSTALL.md](INSTALL.md).

## What runs

**One pod.** The Deployment has these containers:

| Container | Kind | What it does |
|---|---|---|
| `init` | init container | Applies the pending migrations. Creates the first admin when you set `init.adminUser`. |
| `web` | app | The web app (`python -m queryhub.web`) on port `http` (8080). |
| `bot` | app | The Slack bot (`python -m queryhub.main`). Health port `health` (8081). |

**Always one replica, with strategy `Recreate`.** The chart has no replica value.
The reasons:

1. The bot recovers interrupted requests at start. Two copies would do that work twice.
2. `Recreate` stops the old pod before the new pod starts. So two copies never run.
3. The result volume is `ReadWriteOnce`. Such a volume is available on one node only.

**CronJobs for the scheduled scripts.** Each enabled entry in `jobs` becomes one
CronJob. The defaults in `values.yaml` are:

| Job | Schedule | Default | Purpose |
|---|---|---|---|
| `connection-health` | `* * * * *` | on | Checks that QueryHub can connect to each target and stores the answer. |
| `schema-catalog` | `0 * * * *` | on | Refreshes the schema catalog of the targets. |
| `clickhouse-catalog` | `* * * * *` | off | Reads the ClickHouse targets that are due. For ClickHouse targets only. |
| `expire-stale-pending` | `0 * * * *` | on | Cancels pending requests older than `pending_expiry_hours`, when `pending_expiry_enabled` is on. |
| `cleanup-results` | `0 2 * * *` | on | Deletes result files older than `results_ttl_hours`. |

Each CronJob runs one job at a time (`concurrencyPolicy: Forbid`). A failed run
does not retry. The next scheduled run is the retry. `cleanup-results` mounts the
result volume, so it runs on the node of the app pod. Times are UTC unless you
set `jobDefaults.timeZone`.

**Vanilla profile (no Slack).** Set `slack.enabled: false`. The pod then has no
`bot` container, and the web app runs the scheduler. The app selects the
profile from `SLACK_BOT_TOKEN`, so in this profile the chart sets it to an empty
value. A token in the `env` Secret then has no effect. With `slack.enabled: true`,
the Secret must hold the token.

## Build the image

The chart never uses a public image or `latest`. Build the image from this
repository and push it to your registry.

```bash
docker build --build-arg QH_MSSQL_DRIVER=1 --build-arg QH_BUILD_SHA=$(git rev-parse HEAD) \
  -t registry.example.com/queryhub:<tag> .
docker push registry.example.com/queryhub:<tag>
```

- **`QH_MSSQL_DRIVER=1`** installs Microsoft ODBC Driver 18 for SQL Server. It
  also accepts the Microsoft EULA of that driver. The default `0` leaves the
  driver out. Only SQL Server targets need it. See
  [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).
- **`QH_BUILD_SHA`** puts the commit into the build stamp of the app.
- The image installs the Python extras `slack`, `mssql`, `aws` and `clickhouse`.

## Create the Secrets

Create the Secrets before the install. The chart reads them by name and never
stores a credential in a value. An External Secrets or a Secrets Store CSI setup
can create the same Secrets.

```bash
kubectl create namespace queryhub
```

**1. The shared environment (required).** Every QueryHub container gets it.

```bash
kubectl -n queryhub create secret generic queryhub-env --from-env-file=queryhub.env
```

Write one `KEY=value` line for each key, with no quotes. `kubectl` keeps
the quotes, so a quote becomes part of the value.

```text
BOT_DB_HOST=db.internal.example.com
BOT_DB_PORT=5432
BOT_DB_NAME=queryhub
BOT_DB_USER=queryhub
BOT_DB_PASSWORD=<password>
SLACK_BOT_TOKEN=xoxb-<token>
SLACK_APP_TOKEN=xapp-<token>
QH_ADMIN_PASSWORD=<bootstrap password>
```

The Slack keys are for the Slack profile only. `QH_ADMIN_PASSWORD` is optional.
Without it, the `init` container generates a password and prints it once in its log.

**2. The web-only environment (optional).** Only the `web` container gets it.

```bash
kubectl -n queryhub create secret generic queryhub-web-env --from-env-file=queryhub-web.env
```

It holds `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET` and `OIDC_<ID>_ISSUER`,
`OIDC_<ID>_CLIENT_ID`, `OIDC_<ID>_CLIENT_SECRET`. Never put these keys in the
shared Secret. The Slack SDK switches the bot to OAuth mode when it sees a client id.

**3. The migrator environment (optional).** Only the `init` container gets it.
Use it when you split the metadata roles.

```bash
kubectl -n queryhub create secret generic queryhub-migrator-env --from-env-file=queryhub-migrator.env
```

It holds `BOT_DB_MIGRATOR_USER`, `BOT_DB_MIGRATOR_PASSWORD` and `BOT_DB_OWNER_ROLE`.

**4. The master key (required).**

```bash
kubectl -n queryhub create secret generic queryhub-master-key --from-file=master.key=<path-to-master.key>
```

> **Keep a copy of the master key outside the cluster.** QueryHub encrypts every
> stored target credential with this key. If you lose the key, QueryHub cannot
> read any stored credential.

**A secret injector that sets only environment variables.** Some clusters inject
secrets with a webhook (a Vault webhook, for example) and cannot mount a file.
Put the key in `QH_MASTER_KEY` instead. The entrypoint writes it to
`MASTER_KEY_PATH` with mode 0600. Then it deletes `QH_MASTER_KEY` from the
environment before the app starts. Set `QH_MASTER_KEY` or
`QH_MASTER_KEY_SOURCE`, not both. When you set both, the entrypoint stops.

## Install

Write a values file. This example uses only placeholders.

```yaml
# my-values.yaml
image:
  repository: registry.example.com/queryhub
  tag: "<tag>"
secrets:
  env: queryhub-env
  webEnv: queryhub-web-env          # optional
  migratorEnv: queryhub-migrator-env  # optional
  masterKey:
    name: queryhub-master-key
web:
  baseUrl: https://queryhub.example.com
  ingress:
    enabled: true
    className: <ingress-class>
    hosts:
      - host: queryhub.example.com
        paths:
          - path: /
            pathType: Prefix
    tls:
      - secretName: queryhub-tls
        hosts:
          - queryhub.example.com
serviceAccount:
  annotations:
    eks.amazonaws.com/role-arn: arn:aws:iam::<account-id>:role/<role-name>
```

The IAM role annotation is for AWS access only (Secrets Manager, Athena, S3).

```bash
helm upgrade --install queryhub deploy/helm/queryhub -n queryhub --create-namespace -f my-values.yaml
```

Check the install:

1. Wait for the pod:
   `kubectl -n queryhub rollout status deployment/queryhub --timeout=10m`
2. Read the init log (migrations, first admin):
   `kubectl -n queryhub logs deployment/queryhub -c init`
3. Open `https://queryhub.example.com`. Without an ingress, run
   `kubectl -n queryhub port-forward service/queryhub 8080:80` and open `http://localhost:8080`.

The names above assume the release name `queryhub`. `helm status queryhub -n queryhub`
prints the same steps with the real names.

## Upgrade

1. Build and push the new image.
2. Set `image.tag` to the new tag.
3. Run the same `helm upgrade --install` command.

What happens then:

1. The old pod drains. It refuses new submissions and waits for the running
   queries, for at most `terminationGracePeriodSeconds` (360). If you raise
   bot_config `query_timeout_sec` above 300, raise this value by the same amount.
2. The `init` container of the new pod applies the new migrations.
3. The `web` and `bot` containers start.

While migrations are pending, `/readyz` answers 503, so the ingress sends no
traffic. A CronJob run can start with the new image before `init` applies the
migrations. That run does nothing and exits 0 (`QH_SKIP_IF_MIGRATIONS_PENDING`).
The next scheduled run does the work.

## Network and DNS

- **Databases.** The pod and the CronJob pods must reach the metadata database
  and every target database. Open the security groups and the allow lists for
  the pod network.
- **Slack.** The bot connects out to Slack (Socket Mode). It needs no inbound port.
- **Names that the cluster DNS cannot resolve.** Use a private DNS zone, or set
  `hostAliases`. The chart adds them to the app pod and to every CronJob pod.

```yaml
hostAliases:
  - ip: 192.0.2.10
    hostnames:
      - db1.internal.example.com
```

## Sign-in behind an ingress

1. Set `web.baseUrl` to the public https address. It becomes `WEB_BASE_URL`.
2. Register `<baseUrl>/api/auth/slack/callback` in the Slack app for Slack sign-in.
3. Register `<baseUrl>/api/auth/<provider-id>/callback` at each OIDC provider.
4. Set `web_trusted_proxy` to `on` after the move to the ingress.
5. Set `web_trusted_proxy_hops` to the number of proxies in front of the app (default 1).

With `web_trusted_proxy` on, the app reads the client IP from `X-Forwarded-For`.
The login throttle and the audit log use that IP. Never turn it on while clients
can reach the app directly. A client can then send a false header.

```sql
UPDATE bot_config SET value = 'on' WHERE key = 'web_trusted_proxy';
```

## Move from a host install (systemd)

Keep the same profile. If the host runs Slack, keep `slack.enabled: true`. A
move to the vanilla profile is a different change.

1. **Copy the environment to Secrets.** Put the shared keys in `queryhub-env`.
   Put the web-only keys (`SLACK_CLIENT_*`, `OIDC_*`) in `queryhub-web-env`. If
   the host keeps values in `secrets.enc`, write them as plain lines too. The
   pod has no `secrets.enc`.
2. **Copy the master key** into `queryhub-master-key`. Use the same file, or
   QueryHub cannot read the stored credentials.
3. **Build and push the image.**
4. **Stop the host install before the pod starts.** Stop and disable both
   services (`queryhub` and `queryhub-web`). Disable the host timers and cron
   entries of the scheduled scripts. Two copies against one metadata database
   run the same requests twice.
5. **Install, then check.** Run `helm upgrade --install`. Check that `/readyz`
   answers 200. Check the bot log for `Bolt app is running`. Then move DNS or
   the ingress to the new address.

```bash
kubectl -n queryhub logs deployment/queryhub -c bot | grep "Bolt app is running"
kubectl -n queryhub exec deployment/queryhub -c web -- python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/readyz').status)"
```

**Rollback:**

1. Stop the pod: `kubectl -n queryhub scale deployment/queryhub --replicas=0`.
2. Suspend the CronJobs (command below).
3. Start the host services again.

```bash
for cj in $(kubectl -n queryhub get cronjob -l app.kubernetes.io/instance=queryhub -o name); do
  kubectl -n queryhub patch "$cj" -p '{"spec":{"suspend":true}}'
done
```

Do not run `helm upgrade` during a rollback. The chart sets one replica, and an
upgrade can start the pod again.

## Files that the host install kept outside the repository

**The What's-new file.** The in-app What's-new page reads the file in bot_config
`web_changelog_path`. Put the file in a ConfigMap and mount it.

```bash
kubectl -n queryhub create configmap queryhub-changelog --from-file=changelog.json=<path-to-changelog.json>
```

```yaml
extraVolumes:
  - name: changelog
    configMap:
      name: queryhub-changelog
extraVolumeMounts:
  - name: changelog
    mountPath: /etc/queryhub-site
    readOnly: true
```

```sql
UPDATE bot_config SET value = '/etc/queryhub-site/changelog.json' WHERE key = 'web_changelog_path';
```

Do not mount anything at `/etc/queryhub`. The master key lives there.

## Health

| Container | Endpoint | Probe | Answer |
|---|---|---|---|
| `web` | `/healthz` on `http` | startup, liveness | 200 when the process answers. |
| `web` | `/readyz` on `http` | readiness | 503 while the metadata database is down or migrations are pending. |
| `bot` | `/healthz` on `health` (`QH_HEALTH_PORT`) | startup, liveness | 503 when the scheduler thread or the auth-event thread died. |

A drain stops the bot threads on purpose. So the bot `/healthz` counts a drain
as healthy, and the kubelet does not kill a pod that waits for its queries.
The body is JSON and names each check.

The bot `/healthz` does not check the Slack connection. The Slack SDK connects
again by itself, and a restart during a Slack outage does not help. Watch the
bot log for connection errors instead.

## Security defaults

- The containers run as the non-root user uid 10001.
- The root filesystem is read-only. `/tmp` is an `emptyDir`.
- The containers drop all capabilities and use the `RuntimeDefault` seccomp profile.
- The pod gets no service account token (`serviceAccount.automountToken: false`).
  The app never calls the Kubernetes API.
- The Secret mounts the master key read-only at `/var/run/secrets/queryhub/master-key`.
  The entrypoint copies it to an in-memory volume at `/etc/queryhub`, with mode 0600.
  `crypto.py` refuses a key that the group can read.
- `QH_MASTER_KEY_AUTOGEN` is `0`. A missing key stops the start. The pod never
  generates a new key.

## Values reference

The main values are below. `values.yaml` has every key with a comment.

| Value | Default | Meaning |
|---|---|---|
| `image.repository`, `image.tag` | `""` | Required. The image that you built. |
| `secrets.env` | `""` | Required. The shared environment Secret. |
| `secrets.webEnv` | `""` | The web-only environment Secret. |
| `secrets.migratorEnv` | `""` | The migrator environment Secret, for `init` only. |
| `secrets.masterKey.name`, `.key` | `""`, `master.key` | Required. The master key Secret and its key. |
| `slack.enabled` | `true` | Runs the `bot` container. `false` is the vanilla profile. |
| `web.baseUrl` | `""` | The public address. Becomes `WEB_BASE_URL`. |
| `web.ingress.*` | disabled | The Ingress: `className`, `annotations`, `hosts`, `tls`. |
| `bot.healthPort` | `8081` | The bot health port (`QH_HEALTH_PORT`). |
| `init.adminUser` | `""` | The first local admin account. |
| `persistence.*` | 20Gi, `ReadWriteOnce` | The result volume. `existingClaim` reuses a claim. |
| `terminationGracePeriodSeconds` | `360` | Must be longer than `query_timeout_sec` (300). |
| `serviceAccount.annotations` | `{}` | For example the IAM role for AWS access. |
| `hostAliases` | `[]` | Fixed name-to-address entries for every pod. |
| `env`, `extraEnv` | `LOG_FORMAT`, `LOG_LEVEL` | Plain environment for every container. |
| `extraVolumes`, `extraVolumeMounts` | `[]` | More volumes for every container. |
| `jobDefaults.*` | UTC | Time zone, history limits and resources of the CronJobs. |
| `jobs.<name>` | five jobs | `enabled`, `schedule`, `args`, `activeDeadlineSeconds`, `mountData`. |
