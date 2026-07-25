"""AlbumArt INSERT — content_key dedup and first-seen AUTOINCREMENT order.

Determinism of every Track.albumArtId in a canonical dump depends on insert
order: id is AUTOINCREMENT, so first-seen content_key order is load-bearing.
A writer that reorders by hash or path would still "dedup" but fail golden
comparisons and any test that pins id values.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from rb2engine.ir import SourceArtwork
from rb2engine.ir_engine import artwork_content_hash
from rb2engine.writer import schema as schema_mod
from rb2engine.writer.artwork import insert_artwork

DDL_DIR = Path(__file__).resolve().parents[2] / "src" / "rb2engine" / "writer" / "ddl"
DB_UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
GOLDEN_PNG = (
    Path(__file__).resolve().parents[1] / "fixtures" / "golden" / "albumart_engine.png"
)

# Minimal distinct 1×1 PNGs (different bytes → different content_key).
# Hand-authored fixtures — not produced by insert_artwork.
_PNG_A = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c49444154789c6360f8cfc0000000030001f9c4e2c50000000049454e44ae426082"
)
_PNG_B = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c49444154789c6368f8ffc0000000030001fbcdc28d0000000049454e44ae426082"
)


def _create_db(path: Path, schema: tuple[int, int, int]) -> sqlite3.Connection:
    if schema == (3, 0, 1):
        conn = schema_mod.create_database(path, schema, database_uuid=DB_UUID)
    elif schema == (3, 0, 2):
        conn = sqlite3.connect(str(path))
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.executescript((DDL_DIR / "schema_3_0_2.sql").read_text(encoding="utf-8"))
        conn.execute(
            """
            INSERT INTO Information (
                uuid, schemaVersionMajor, schemaVersionMinor, schemaVersionPatch,
                currentPlayedIndiciator, lastRekordBoxLibraryImportReadCounter
            ) VALUES (?, 3, 0, 2, 0, NULL)
            """,
            (DB_UUID,),
        )
        conn.commit()
    else:
        raise AssertionError(f"unexpected schema {schema}")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@pytest.fixture(params=[(3, 0, 1), (3, 0, 2)], ids=["schema_3_0_1", "schema_3_0_2"])
def conn(tmp_path: Path, request: pytest.FixtureRequest) -> sqlite3.Connection:
    schema = request.param
    connection = _create_db(tmp_path / f"art_{schema[2]}.db", schema)
    yield connection
    connection.close()


def _art_file(tmp_path: Path, name: str, data: bytes) -> SourceArtwork:
    path = tmp_path / name
    path.write_bytes(data)
    return SourceArtwork(
        content_key=artwork_content_hash(data),
        path=path,
        source="pdb",
    )


def test_dedup_two_identical_images_yield_one_row(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Two tracks sharing cover art must produce one AlbumArt row (one BLOB copy).

    Without dedup, a 300 MB library becomes multi-GB and albumArtId values
    diverge for the same image — both wrong for Engine and for determinism.
    """
    a1 = _art_file(tmp_path, "cover_a.png", _PNG_A)
    a2 = SourceArtwork(
        content_key=a1.content_key,
        path=a1.path,
        source="pdb",
    )
    assert a1.content_key == a2.content_key

    id_map = insert_artwork(conn, [a1, a2])
    conn.commit()

    assert id_map == {a1.content_key: 1}
    rows = conn.execute(
        "SELECT id, hash, length(albumArt) FROM AlbumArt ORDER BY id"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 1
    assert rows[0][1] == a1.content_key
    assert rows[0][2] == len(_PNG_A)
    blob = conn.execute("SELECT albumArt FROM AlbumArt WHERE id = 1").fetchone()[0]
    assert blob == _PNG_A


def test_first_seen_order_fixes_autoincrement_ids(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Insertion order is first-seen, not sorted-by-hash — ids drive albumArtId.

    If the writer sorted by content_key, canonical dumps and every Track.albumArtId
    would shuffle whenever a new image sorts earlier, breaking determinism.
    """
    first = _art_file(tmp_path, "b.png", _PNG_B)  # content_key may sort after/before A
    second = _art_file(tmp_path, "a.png", _PNG_A)
    # Confirm the keys are distinct so order is meaningful.
    assert first.content_key != second.content_key

    id_map = insert_artwork(conn, [first, second, first])
    conn.commit()

    assert id_map[first.content_key] == 1
    assert id_map[second.content_key] == 2
    ordered = conn.execute("SELECT id, hash FROM AlbumArt ORDER BY id").fetchall()
    assert ordered == [
        (1, first.content_key),
        (2, second.content_key),
    ]


def test_blob_bytes_round_trip_golden_png(
    conn: sqlite3.Connection,
) -> None:
    """Stored BLOB must be byte-identical to the source image (magic intact).

    A writer that stores empty blobs, paths-as-text, or re-encoded thumbnails
    would still produce rows and pass length>0 checks while Engine shows blank
    or corrupt covers.
    """
    data = GOLDEN_PNG.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    art = SourceArtwork(
        content_key=artwork_content_hash(data),
        path=GOLDEN_PNG,
        source="pdb",
    )
    id_map = insert_artwork(conn, [art])
    conn.commit()
    blob = conn.execute(
        "SELECT albumArt FROM AlbumArt WHERE id = ?",
        (id_map[art.content_key],),
    ).fetchone()[0]
    assert blob == data
    assert blob[:8] == b"\x89PNG\r\n\x1a\n"


def test_hash_column_is_content_key(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """AlbumArt.hash is our internal content_key (stripped sha1), not Engine's opaque hash.

    Engine's golden hash value is unresolved (D1); internal consistency is what
    links tracks via art_ids[content_key].
    """
    art = _art_file(tmp_path, "x.png", _PNG_A)
    insert_artwork(conn, [art])
    conn.commit()
    stored = conn.execute("SELECT hash FROM AlbumArt").fetchone()[0]
    assert stored == art.content_key
    assert stored == artwork_content_hash(_PNG_A)


def test_empty_sequence_inserts_nothing(conn: sqlite3.Connection) -> None:
    assert insert_artwork(conn, []) == {}
    assert conn.execute("SELECT COUNT(*) FROM AlbumArt").fetchone()[0] == 0


def test_album_art_id_fk_resolves_from_returned_map(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Returned content_key→id must be usable as Track.albumArtId under FK RESTRICT.

    If the map lies about ids, track insert fails only when foreign_keys=ON —
    the same condition Engine enforces for ON DELETE RESTRICT integrity.
    """
    art = _art_file(tmp_path, "cover.png", _PNG_A)
    id_map = insert_artwork(conn, [art])
    conn.commit()
    art_id = id_map[art.content_key]

    conn.execute(
        "INSERT INTO Track (path, title, albumArtId, albumArt) VALUES (?, ?, ?, ?)",
        ("../Contents/a.mp3", "t", art_id, "image://planck/0"),
    )
    conn.commit()
    got = conn.execute("SELECT albumArtId FROM Track").fetchone()[0]
    assert got == art_id
    assert (
        conn.execute("SELECT id FROM AlbumArt WHERE id = ?", (got,)).fetchone()[0]
        == art_id
    )
