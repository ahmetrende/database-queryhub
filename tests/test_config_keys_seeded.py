"""Every bot_config key the code reads must be seeded by a migration.

A missing row does not break the feature — every read passes a default — it
makes the key invisible: GET /admin/config builds its groups from the rows in
bot_config, so an unseeded key cannot be seen or changed from the admin UI. That
included the target-TLS and trusted-proxy hardening switches, which is the worst
possible place for "you have to INSERT it by hand and nothing says so".

29 keys were in that state. This test is the gate that keeps it at zero: it
reads the call sites out of the source and the seeded keys out of the migration
files, so it needs no database and runs in the fast suite.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
MIGRATIONS = ROOT / "migrations"

# cfg.get_setting("key", default) / get_int / get_bool, however it's imported.
_READ = re.compile(
    r'(?:cfg|config)\.get_(?:setting|int|bool)\(\s*["\']([a-z0-9_]+)["\']')
# INSERT INTO bot_config ... ('key', ...) — the seeds are literal tuples, so the
# first quoted token of each row is the key.
_SEED_ROW = re.compile(r"^\s*\(\s*'([a-z0-9_]+)'\s*,", re.MULTILINE)

# Read through a helper rather than as a literal, so the pattern above can't see
# them. Each is checked by its own test elsewhere; listing them here keeps this
# gate honest instead of loosening the regex.
DYNAMIC_KEYS: set[str] = set()


def _keys_read_by_code():
    found = {}
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for m in _READ.finditer(text):
            found.setdefault(m.group(1), set()).add(
                str(path.relative_to(SRC)))
    return found


def _keys_seeded_by_migrations():
    seeded = set()
    for path in sorted(MIGRATIONS.glob("*.sql")):
        text = path.read_text(encoding="utf-8")
        # Only look at statements that write bot_config.
        for stmt in re.split(r";\s*\n", text):
            if "bot_config" not in stmt.lower():
                continue
            if not re.search(r"insert\s+into\s+bot_config", stmt, re.I):
                continue
            seeded |= set(_SEED_ROW.findall(stmt))
    return seeded


def test_the_scan_finds_something():
    """If either side silently matched nothing, the comparison below would pass
    for the wrong reason."""
    assert len(_keys_read_by_code()) > 40
    assert len(_keys_seeded_by_migrations()) > 40


def test_every_config_key_read_by_code_is_seeded():
    read = _keys_read_by_code()
    seeded = _keys_seeded_by_migrations() | DYNAMIC_KEYS
    missing = sorted(set(read) - seeded)
    detail = "\n".join(f"  {k}  (read in {', '.join(sorted(read[k]))})"
                       for k in missing)
    assert not missing, (
        f"{len(missing)} bot_config key(s) are read by the code but seeded by "
        f"no migration, so the admin UI cannot show or set them:\n{detail}\n"
        "Add them to a migration (see 079_seed_unseeded_config_keys.sql) with "
        "the same default the code passes.")


def test_every_config_insert_declares_a_description():
    """The admin UI renders the description under each field, so a key seeded
    without one is a mystery switch in a security tool.

    This checks the statement's column list rather than each row's literals: the
    descriptions contain parentheses and escaped quotes ('user''s'), which no
    regex over row tuples survives — an earlier version of this test "found"
    two undescribed keys that both had descriptions.
    """
    offenders = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"insert\s+into\s+bot_config\s*\(([^)]*)\)",
                             text, re.I):
            columns = {c.strip().lower() for c in m.group(1).split(",")}
            if "description" not in columns:
                offenders.append(f"{path.name}: INSERT without a description column")
    assert not offenders, "\n".join(offenders)
