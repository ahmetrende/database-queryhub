"""The Helm chart (deploy/helm/queryhub) keeps the promises the app depends on.

The CI job `helm-chart` renders and validates the chart. These tests check the
parts that a render cannot see: that a value names a script or a module that
exists, that the chart follows the release version, and that nobody turns the
single pod into two.
"""
import pathlib
import re
import tomllib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
CHART = ROOT / "deploy" / "helm" / "queryhub"


def _values() -> dict:
    return yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))


def _chart() -> dict:
    return yaml.safe_load((CHART / "Chart.yaml").read_text(encoding="utf-8"))


def _template(name: str) -> str:
    return (CHART / "templates" / name).read_text(encoding="utf-8")


def test_the_chart_follows_the_release_version():
    """RELEASING.md: bump appVersion with pyproject."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = project["project"]["version"]
    base = re.match(r"\d+\.\d+\.\d+", version).group(0)
    assert _chart()["appVersion"] == base, (
        f"Chart.yaml appVersion is {_chart()['appVersion']}, pyproject says {version}")


def test_the_chart_never_guesses_an_image():
    image = _values()["image"]
    assert image["repository"] == "" and image["tag"] == ""
    helpers = _template("_helpers.tpl")
    assert 'required "Set image.repository' in helpers
    assert 'required "Set image.tag' in helpers


def test_the_app_runs_as_one_pod_that_stops_before_the_next_starts():
    """Two copies would both recover interrupted requests at start."""
    deployment = _template("deployment.yaml")
    assert re.search(r"^  replicas: 1$", deployment, re.M)
    assert "type: Recreate" in deployment
    assert ".Values.replicas" not in deployment


def test_the_containers_run_modules_that_exist():
    deployment = _template("deployment.yaml")
    modules = re.findall(r'args: \["python", "-m", "([a-z_.]+)"\]', deployment)
    assert sorted(modules) == ["queryhub.main", "queryhub.web"]
    for module in modules:
        parts = module.split(".")
        path = ROOT / "src" / pathlib.Path(*parts)
        assert path.with_suffix(".py").is_file() or (path / "__main__.py").is_file(), module


def test_every_scheduled_job_runs_a_script_that_exists():
    for name, job in _values()["jobs"].items():
        args = job["args"]
        assert args[0] == "python", name
        assert (ROOT / args[1]).is_file(), f"jobs.{name} runs {args[1]}, which does not exist"


def test_the_job_that_reads_result_files_mounts_the_volume():
    jobs = _values()["jobs"]
    assert jobs["cleanup-results"]["mountData"] is True
    assert not any(j.get("mountData") for n, j in jobs.items() if n != "cleanup-results")


def test_a_stopping_pod_outlives_the_longest_query():
    """The app waits for running queries before it exits. query_timeout_sec is
    300 by default, the same bound as TimeoutStopSec=330s in queryhub.service."""
    assert _values()["terminationGracePeriodSeconds"] > 330


def test_the_bot_health_port_is_the_one_the_bot_reads():
    deployment = _template("deployment.yaml")
    assert "name: QH_HEALTH_PORT" in deployment
    assert "value: {{ .Values.bot.healthPort | quote }}" in deployment
    assert (ROOT / "src" / "queryhub" / "health_server.py").is_file()


def test_the_web_only_secret_never_reaches_the_bot():
    """The Slack SDK switches the bot to OAuth mode when it sees a client id."""
    deployment = _template("deployment.yaml")
    bot = deployment[deployment.index("- name: bot"):]
    web = deployment[deployment.index("- name: web"):deployment.index("- name: bot")]
    assert ".Values.secrets.webEnv" in web
    assert ".Values.secrets.webEnv" not in bot


def test_the_migrator_login_stays_in_the_init_container():
    deployment = _template("deployment.yaml")
    init = deployment[deployment.index("- name: init"):deployment.index("containers:\n        - name: web")]
    assert ".Values.secrets.migratorEnv" in init
    assert deployment.count(".Values.secrets.migratorEnv") == 1
    assert ".Values.secrets.migratorEnv" not in _template("cronjobs.yaml")


def test_app_containers_and_jobs_never_apply_migrations():
    for name in ("deployment.yaml", "cronjobs.yaml"):
        text = _template(name)
        skips = len(re.findall(r'name: QH_SKIP_MIGRATIONS\n\s+value: "1"', text))
        applies = len(re.findall(r'name: QH_SKIP_MIGRATIONS\n\s+value: "0"', text))
        if name == "deployment.yaml":
            assert (skips, applies) == (2, 1)   # web + bot skip, init applies
        else:
            assert (skips, applies) == (1, 0)
            assert "QH_SKIP_IF_MIGRATIONS_PENDING" in text


def test_the_vanilla_profile_clears_a_slack_token_left_in_the_secret():
    """config.slack_enabled is bool(SLACK_BOT_TOKEN). With slack.enabled false
    and a token in the Secret, the web app would skip the scheduler and no
    process would run it."""
    helpers = _template("_helpers.tpl")
    block = helpers[helpers.index("{{- if not .Values.slack.enabled }}"):]
    block = block[:block.index("{{- end }}")]
    assert "- name: SLACK_BOT_TOKEN" in block
    assert 'value: ""' in block


def test_the_entrypoint_supports_what_the_chart_sets():
    entrypoint = (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
    for name in ("QH_MASTER_KEY_SOURCE", "QH_MASTER_KEY_AUTOGEN",
                 "QH_SKIP_MIGRATIONS", "QH_SKIP_IF_MIGRATIONS_PENDING"):
        assert name in entrypoint, name
    assert '"${1:-}" = "init"' in entrypoint


def test_the_entrypoint_removes_an_env_master_key_before_the_app_starts():
    """QH_MASTER_KEY is for injectors that set only environment variables. The
    key goes to a 0600 file, and the app process must not inherit the variable."""
    entrypoint = (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
    assert "QH_MASTER_KEY:-" in entrypoint
    unset = entrypoint.index("unset QH_MASTER_KEY\n")
    assert unset < entrypoint.index('exec "$@"')
    assert unset < entrypoint.index('"${1:-}" = "init"')
    assert "set QH_MASTER_KEY or QH_MASTER_KEY_SOURCE, not both" in entrypoint
