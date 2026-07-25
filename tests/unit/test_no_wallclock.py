"""CI grep: no wall-clock APIs in writer/ or mapper/ Python.

WHY: two conversions of the same source must produce byte-identical m.db
(criterion 8 / test_determinism). Any wall-clock write makes that unpassable
or flaky (two runs inside the same second pass; two spanning a second boundary
fail). Time-derived Track columns are wired through mapper/track.py as well as
writer/, so grepping only writer/ leaves half the surface unchecked.

.sql under writer/ddl/ is deliberately excluded: it is captured Engine DDL we
are forbidden to edit. Engine triggers that stamp strftime('%s') are handled by
a post-write pin in build finalize, not by editing the DDL.
"""

from __future__ import annotations

import re
from pathlib import Path

# Patterns from the plan §C / Layer-1 gate (C-MAJOR-4, residual note 2).
_FORBIDDEN = (
    re.compile(r"\btime\.time\b"),
    re.compile(r"\bdatetime\.now\b"),
    re.compile(r"\bdatetime\.utcnow\b"),
    re.compile(r"""datetime\s*\(\s*['"]now['"]\s*\)"""),
    re.compile(r"\bCURRENT_TIMESTAMP\b"),
)

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "rb2engine"

# Both packages that can inject time-derived values into m.db columns.
_SCAN_DIRS = ("writer", "mapper")


def _python_files() -> list[Path]:
    files: list[Path] = []
    for name in _SCAN_DIRS:
        root = _SRC_ROOT / name
        if not root.is_dir():
            continue
        files.extend(sorted(root.rglob("*.py")))
    return files


def test_no_wallclock_calls_in_writer_or_mapper() -> None:
    """Fail if writer/**/*.py or mapper/**/*.py call wall-clock APIs.

    A stray datetime.now() in mapper/track.py would evade a writer-only grep
    and make determinism flaky across second boundaries without a clear CI
    signal. .sql files are not scanned (captured DDL, not our code).
    """
    hits: list[str] = []
    scanned = 0
    for path in _python_files():
        scanned += 1
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(_SRC_ROOT)
        for lineno, line in enumerate(text.splitlines(), start=1):
            for pat in _FORBIDDEN:
                if pat.search(line):
                    hits.append(f"{rel}:{lineno}: {line.strip()}")

    assert scanned > 0, "expected to scan writer/ and mapper/ Python sources"
    # Ensure we actually cover mapper — the residual note that motivated this.
    mapper_files = [p for p in _python_files() if "mapper" in p.parts]
    assert mapper_files, "mapper/**/*.py must be on the wall-clock audit path"
    assert hits == [], (
        "wall-clock APIs are forbidden in writer/ and mapper/ (determinism):\n"
        + "\n".join(hits)
    )


def test_wallclock_scan_excludes_sql_ddl() -> None:
    """Captured schema_*.sql must not be grepped — we cannot edit them.

    If this scan ever walked .sql, Engine's strftime('%s') triggers would
    permanently fail CI for a file the plan forbids modifying.
    """
    ddl = _SRC_ROOT / "writer" / "ddl"
    sql_files = list(ddl.glob("schema_*.sql"))
    assert sql_files, "expected bundled DDL files under writer/ddl/"
    # Our scanner only yields .py — no sql path may appear.
    scanned = {p.resolve() for p in _python_files()}
    for sql in sql_files:
        assert sql.resolve() not in scanned
        # Sanity: the DDL really does contain time constructs we deliberately ignore.
        body = sql.read_text(encoding="utf-8")
        assert "strftime" in body
