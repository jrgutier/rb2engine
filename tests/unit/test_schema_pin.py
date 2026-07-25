"""Pin bundled Engine DDL by content hash and declared version triple.

WHY: if someone re-captures a schema and the bytes differ, we must fail loudly
rather than silently changing what we write into users' libraries. The DDL is
Engine-authored ground truth; SUPPORTED_SCHEMAS must stay in lockstep with the
files actually present under writer/ddl/.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from rb2engine.writer import schema as schema_mod

# Content hashes of the committed captures. Update deliberately when re-capturing
# (never by re-hashing an accidental edit). Values measured from the files on
# disk at pin time — not from a self-generated dump.
_PINNED_DDL: dict[tuple[int, int, int], dict[str, str]] = {
    (3, 0, 1): {
        "filename": "schema_3_0_1.sql",
        "sha256": (
            "e4cf0ec7861486bd4c10c6071eb91fcb8e9f82bffcbcf360e121301c7643f203"
        ),
    },
    (3, 0, 2): {
        "filename": "schema_3_0_2.sql",
        "sha256": (
            "eb6f8e06a1dfd15506ca6ba61f64b1954d58c7816d819a9a2371cf4370da5f60"
        ),
    },
}

# Header comments declare the Engine schema triple (not inventable from filename
# alone — the filename must match, and the header must agree).
_VERSION_IN_HEADER = re.compile(
    r"schema(?:\s+version)?\s+(\d+)\.(\d+)\.(\d+)",
    re.IGNORECASE,
)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _declared_version(text: str) -> tuple[int, int, int] | None:
    """First schema X.Y.Z mentioned in the provenance header / body."""
    m = _VERSION_IN_HEADER.search(text)
    if m is None:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def test_supported_schemas_keys_match_ddl_files_on_disk() -> None:
    """SUPPORTED_SCHEMAS must be exactly the schema_*.sql files we ship.

    A key without a file (or a file without a key) means resolve_schema would
    either raise mysteriously or leave an orphan capture nobody can select.
    """
    ddl_dir = schema_mod.DDL_DIR
    on_disk = sorted(p.name for p in ddl_dir.glob("schema_*.sql"))
    mapped = sorted(schema_mod.SUPPORTED_SCHEMAS.values())
    assert on_disk == mapped

    assert set(schema_mod.SUPPORTED_SCHEMAS) == set(_PINNED_DDL)
    for triple, filename in schema_mod.SUPPORTED_SCHEMAS.items():
        assert _PINNED_DDL[triple]["filename"] == filename
        assert (ddl_dir / filename).is_file()


def test_each_bundled_ddl_matches_pinned_content_hash() -> None:
    """Byte-level pin: re-capture that changes SQL must update this test.

    Silent drift in DDL would change table/trigger inventory written to every
    user's stick without a failing test.
    """
    for triple, meta in sorted(_PINNED_DDL.items()):
        path = schema_mod.DDL_DIR / meta["filename"]
        assert path.is_file(), f"missing DDL for {triple}: {path}"
        digest = _sha256_file(path)
        assert digest == meta["sha256"], (
            f"DDL content hash mismatch for schema {triple[0]}.{triple[1]}.{triple[2]} "
            f"({path.name}).\n"
            f"  pinned:  {meta['sha256']}\n"
            f"  actual:  {digest}\n"
            "If this is an intentional re-capture, update _PINNED_DDL and "
            "document provenance in writer/ddl/README.md."
        )


def test_each_bundled_ddl_declares_matching_version_triple() -> None:
    """Filename / SUPPORTED_SCHEMAS key / header comment must agree.

    A mislabeled capture (e.g. 3.0.2 SQL filed as schema_3_0_1.sql) would
    ship the wrong column set (albumArtSourceHash) under the wrong gate.
    """
    for triple, meta in sorted(_PINNED_DDL.items()):
        path = schema_mod.DDL_DIR / meta["filename"]
        text = path.read_text(encoding="utf-8")
        declared = _declared_version(text)
        assert declared == triple, (
            f"{path.name}: header declares {declared}, "
            f"SUPPORTED_SCHEMAS key is {triple}"
        )
        # Filename pattern schema_M_m_p.sql must encode the same triple.
        stem_match = re.fullmatch(
            r"schema_(\d+)_(\d+)_(\d+)\.sql", meta["filename"]
        )
        assert stem_match is not None
        from_name = (
            int(stem_match.group(1)),
            int(stem_match.group(2)),
            int(stem_match.group(3)),
        )
        assert from_name == triple
