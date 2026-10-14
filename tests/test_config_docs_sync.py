"""Every setting the code reads must be written down where an operator looks.

`tests/test_config_keys_seeded.py` makes sure that the admin UI can SHOW a
`bot_config` key. This file makes sure that a person can READ about it. When a
key or an environment variable exists only in the code, an operator learns about
it from a bug report, and an open-source user never learns about it.

Three checks, none of which needs a database:

- Every `bot_config` key that the code reads is in `docs/CONFIGURATION.md`.
- Every environment variable that the code reads is in `.env.example`,
  `README.md` or `docs/`.
- Every local link in `README.md` and `docs/` points at a file that exists.

When a check fails, write the missing row. Do not add the name to an allow-list
unless the setting is truly internal, and say why next to the name.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
DOCS = sorted((ROOT / "docs").glob("*.md"))
CONFIG_DOC = ROOT / "docs" / "CONFIGURATION.md"
ENV_HOMES = [ROOT / ".env.example", ROOT / "README.md", *DOCS]

# The same call shapes as tests/test_config_keys_seeded.py. A key that the code
# reads through a helper, not as a literal, is invisible to this pattern.
_KEY_READ = re.compile(
    r'(?:cfg|config)\.get_(?:setting|int|bool)\(\s*["\']([a-z0-9_]+)["\']')
# os.environ.get("X"), os.getenv("X"), os.environ["X"], need("X") in config.py and
# _env_int("X") in db.py. The OIDC variables are built from a provider id at run
# time (OIDC_<ID>_ISSUER), so their names are not literals. docs/AUTH.md covers them.
_ENV_READ = re.compile(
    r'(?:os\.environ\.get|os\.getenv|\bneed|\b_env_int)\(\s*["\']([A-Z][A-Z0-9_]+)["\']'
    r'|os\.environ\[\s*["\']([A-Z][A-Z0-9_]+)["\']\s*\]')
_LINK = re.compile(r'(?<!\!)\[[^\]]*\]\(([^)\s]+)\)')

# name -> why the docs may skip it. Empty on purpose.
INTERNAL_KEYS: dict[str, str] = {}
INTERNAL_ENV: dict[str, str] = {}


def _source_files():
    return sorted(SRC.rglob("*.py"))


def keys_read_by_code() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in _source_files():
        for m in _KEY_READ.finditer(path.read_text(encoding="utf-8")):
            found.setdefault(m.group(1), set()).add(str(path.relative_to(SRC)))
    return found


def env_read_by_code() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in _source_files():
        for m in _ENV_READ.finditer(path.read_text(encoding="utf-8")):
            name = m.group(1) or m.group(2)
            found.setdefault(name, set()).add(str(path.relative_to(SRC)))
    return found


def mentioned(name: str, text: str) -> bool:
    """True when `name` appears as a whole word, so `web_cookie` does not count
    as a mention of `web_cookie_secure`."""
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", text) is not None


def undocumented(names, texts, allowed=()) -> list[str]:
    return sorted(n for n in names if n not in allowed
                  and not any(mentioned(n, t) for t in texts))


def broken_links(files) -> list[tuple[str, str]]:
    bad = []
    for f in files:
        text = re.sub(r"```.*?```", "", f.read_text(encoding="utf-8"), flags=re.S)
        for m in _LINK.finditer(text):
            target = m.group(1)
            if re.match(r"^(https?:|mailto:|#)", target):
                continue
            path = target.split("#", 1)[0]
            if path and not (f.parent / path).exists():
                bad.append((str(f.relative_to(ROOT)), target))
    return bad


def test_the_scans_find_something():
    """If a scan silently matched nothing, the checks below would pass for the
    wrong reason."""
    assert len(keys_read_by_code()) > 60
    assert len(env_read_by_code()) > 15
    assert CONFIG_DOC.is_file() and len(DOCS) > 5


def test_the_check_can_fail():
    """A name that no document mentions must be reported, and a whole-word match
    must not accept a longer name."""
    assert undocumented({"no_such_setting"}, ["| `web_cookie_secure` |"]) == ["no_such_setting"]
    assert undocumented({"web_cookie"}, ["| `web_cookie_secure` |"]) == ["web_cookie"]
    assert undocumented({"web_cookie_secure"}, ["| `web_cookie_secure` |"]) == []
    assert undocumented({"no_such_setting"}, [""], allowed={"no_such_setting"}) == []


def test_every_bot_config_key_read_by_code_is_in_the_configuration_reference():
    read = keys_read_by_code()
    reference = CONFIG_DOC.read_text(encoding="utf-8")
    missing = undocumented(read, [reference], INTERNAL_KEYS)
    detail = "\n".join(f"  {k}  (read in {', '.join(sorted(read[k]))})" for k in missing)
    assert not missing, (
        f"{len(missing)} bot_config key(s) are read by the code but missing from "
        f"docs/CONFIGURATION.md:\n{detail}\n"
        "Add a row with the default and what the key does. The seed migration "
        "has the default and a description to start from.")


def test_every_environment_variable_read_by_code_is_documented():
    read = env_read_by_code()
    texts = [p.read_text(encoding="utf-8") for p in ENV_HOMES if p.is_file()]
    missing = undocumented(read, texts, INTERNAL_ENV)
    detail = "\n".join(f"  {n}  (read in {', '.join(sorted(read[n]))})" for n in missing)
    assert not missing, (
        f"{len(missing)} environment variable(s) are read by the code but appear in "
        f"no document:\n{detail}\n"
        "Add them to the environment table in docs/CONFIGURATION.md, or to "
        ".env.example.")


def test_every_local_link_in_the_readme_and_docs_resolves():
    bad = broken_links([ROOT / "README.md", *DOCS])
    detail = "\n".join(f"  {src}: {target}" for src, target in bad)
    assert not bad, f"{len(bad)} local link(s) point at a file that does not exist:\n{detail}"
