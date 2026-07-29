#!/usr/bin/env python3
"""Reproduction harness for non-deterministic playlist output.

WHY THIS EXISTS
---------------
A real conversion produced two spurious ``PlaylistEntity`` rows (one each in
two playlists) that corresponded to no source playlist entry. Re-running
``convert`` over the *same* ``export.pdb`` produced a clean database. So the
writer's determinism guarantee did not hold, but a single re-run destroys the
evidence. One observation cannot tell you the reproduction rate, and it cannot
tell you which half of the pipeline is at fault.

This harness answers both questions by bisecting the pipeline:

* **Reader stage** — parse ``export.pdb`` N times and fingerprint the result.
  Any variation here is a reader bug. The raw bytes are hashed too, so an
  unreliable read is distinguishable from a non-deterministic parse.
* **Writer stage** — read the library *once*, then run ``build_library`` N
  times from that single immutable input. Any variation here is a writer bug,
  because the input is provably identical across runs.

If both stages are stable the non-determinism is neither reader nor writer, and
the next suspect is the commit/copy/replace path or the environment.

WHY NOT A SYMLINKED SHADOW ROOT
-------------------------------
An earlier version built each run against a temp directory holding symlinks to
the real ``PIONEER/`` and ``Contents/``. That was wrong and quietly so:
``engine_track_path`` calls ``.resolve()`` on both the track path and the drive
root (``writer/paths.py``), so the symlink collapsed to the real stick and
``relative_to`` raised. ``mapper/track.py`` swallows that error and degrades to
the raw path, so every track still "converted" and the harness reported a
confident verdict about a configuration nobody ships. The stage now runs
against the real drive root and redirects only the *output* directory, and
``_assert_paths_are_faithful`` fails loudly if the written paths ever stop
looking like a real conversion's.

SAFETY
------
``PIONEER/`` and ``Contents/`` are only ever read. Builds write to a scratch
``Engine Library.repro/`` beside the real library, which is removed after each
run; the real ``Engine Library/`` is never opened for writing.

USAGE
-----
    python tools/repro_playlist_determinism.py "/Volumes/USB DISK" --runs 5
    python tools/repro_playlist_determinism.py "/Volumes/USB DISK" --stage reader
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter
from pathlib import Path

# Engine sentinel: tail of a nextEntityId chain.
_NO_NEXT = 0

# Scratch library name used for builds. Distinct from "Engine Library" so a
# crash can never leave the user's real library half-written.
_SCRATCH_LIBRARY = "Engine Library.repro"


# --------------------------------------------------------------------------
# fingerprints
# --------------------------------------------------------------------------
def _hash(payload: object) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def source_fingerprint(lib) -> dict[str, object]:
    """Everything about the source that can move a playlist chain.

    Membership alone is not enough: chains are built from ``track_id_map``,
    whose ids come from the sorted set of tracks that resolved to a path. A
    reader that non-deterministically drops one track shifts every Engine
    Track.id and therefore every chain, while playlist membership looks
    untouched.
    """
    return {
        "playlists": {str(pl.rb_id): list(pl.track_rb_ids) for pl in lib.playlists},
        "tracks": sorted(lib.tracks),
        "resolved": {
            str(rb): (str(t.resolved_path) if t.resolved_path else None)
            for rb, t in lib.tracks.items()
        },
    }


def entity_chains(m_db: Path) -> dict[str, list[int]]:
    """Written-side playlist order, keyed by playlist title.

    Order is reconstructed through ``nextEntityId`` exactly as Engine reads it,
    so a chain that is corrupt in a way row order would hide still shows up.
    """
    conn = sqlite3.connect(f"file:{m_db}?mode=ro", uri=True)
    try:
        titles = {int(i): str(t) for i, t in conn.execute("SELECT id, title FROM Playlist")}
        out: dict[str, list[int]] = {}
        for list_id, title in titles.items():
            rows = conn.execute(
                "SELECT id, trackId, nextEntityId FROM PlaylistEntity WHERE listId = ?",
                (list_id,),
            ).fetchall()
            by_next = {int(n): (int(e), int(t)) for e, t, n in rows}
            order: list[int] = []
            curr, seen = _NO_NEXT, set()
            while curr in by_next:
                eid, track_id = by_next[curr]
                if eid in seen:  # cycle guard — a corrupt chain must not hang
                    break
                seen.add(eid)
                order.insert(0, track_id)
                curr = eid
            # Rows unreachable from the chain are themselves a defect; surface
            # them rather than reporting a shorter, tidier list.
            if len(order) != len(rows):
                order.append(-len(rows))  # sentinel makes the mismatch visible
            out[f"{title}#{list_id}"] = order
        return out
    finally:
        conn.close()


def _assert_paths_are_faithful(m_db: Path, expected_tracks: int) -> None:
    """Fail unless the written Track paths look like a real conversion's.

    ``map_track`` degrades to the raw absolute path instead of raising when
    relative-path arithmetic fails, so a misconfigured run still reports every
    track converted. Without this check that degradation is invisible and the
    harness would draw a confident conclusion from the wrong configuration.
    """
    conn = sqlite3.connect(f"file:{m_db}?mode=ro", uri=True)
    try:
        total, relative = conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN path LIKE '../%' THEN 1 ELSE 0 END) "
            "FROM Track"
        ).fetchone()
        total, relative = int(total), int(relative or 0)
    finally:
        conn.close()
    if total != expected_tracks or relative != total:
        sample = sqlite3.connect(f"file:{m_db}?mode=ro", uri=True)
        try:
            bad = sample.execute(
                "SELECT path FROM Track WHERE path NOT LIKE '../%' LIMIT 3"
            ).fetchall()
        finally:
            sample.close()
        raise SystemExit(
            f"harness broken: {total} Track rows (expected {expected_tracks}), "
            f"{total - relative} with non-relative paths e.g. {[b[0] for b in bad]} "
            "— the run did not reproduce a real conversion's path arithmetic"
        )


# --------------------------------------------------------------------------
# stages
# --------------------------------------------------------------------------
def run_reader_stage(root: Path, runs: int) -> None:
    """Parse the pdb `runs` times; report distinct fingerprints."""
    from rb2engine.reader.pdb import parse_export_pdb
    from rb2engine.reader.scan import scan_drive

    print(f"\n=== reader stage: parsing export.pdb x{runs} ===")
    seen: Counter[str] = Counter()
    byte_hashes: Counter[str] = Counter()
    first: dict[str, object] | None = None
    for i in range(1, runs + 1):
        pdb_path = scan_drive(root).export_pdb
        # Hash the bytes as read. If the parse varies while these agree, the
        # parser is at fault; if these vary, the read itself is unreliable.
        byte_hashes[hashlib.sha256(pdb_path.read_bytes()).hexdigest()[:16]] += 1
        lib = parse_export_pdb(pdb_path, root)
        fingerprint = source_fingerprint(lib)
        fp = _hash(fingerprint)
        seen[fp] += 1
        if first is None:
            first = fingerprint
            entries = sum(len(v) for v in fingerprint["playlists"].values())  # type: ignore[union-attr]
            print(f"  run {i}: fp={fp}  playlists={len(fingerprint['playlists'])}  "  # type: ignore[arg-type]
                  f"tracks={len(fingerprint['tracks'])}  entries={entries}")  # type: ignore[arg-type]
        else:
            same = fp == _hash(first)
            print(f"  run {i}: fp={fp}  {'same' if same else '*** DIFFERENT ***'}")
            if not same:
                report_membership_diff(
                    first["playlists"], fingerprint["playlists"]  # type: ignore[index,arg-type]
                )
    print(f"  distinct fingerprints: {len(seen)}  ->", dict(seen))
    print(f"  distinct export.pdb byte hashes: {len(byte_hashes)}  ->", dict(byte_hashes))
    if len(byte_hashes) > 1:
        print("  VERDICT: *** export.pdb read returned different bytes — I/O layer ***")
    elif len(seen) > 1:
        print("  VERDICT: *** parser is non-deterministic on identical bytes ***")
    else:
        print("  VERDICT: reader is deterministic across these runs")


def run_writer_stage(
    root: Path, runs: int, *, artwork: bool, keep_dir: Path
) -> None:
    """Read once, build `runs` times from that identical input.

    *artwork* mirrors the real ``convert`` default. It is far slower (every
    audio file is opened) but it is the configuration the observed failure
    actually ran under, so a repro attempt that skips it is not faithful.
    """
    from rb2engine.reader.library import read_library
    from rb2engine.report import ConversionReport
    from rb2engine.writer import build as build_mod

    print(f"\n=== writer stage: build_library x{runs} "
          f"(artwork={'on' if artwork else 'off'}) from one immutable read ===")
    # ANLZ never affects playlist chains, so it stays off regardless.
    lib = read_library(root, with_anlz=False, with_artwork=artwork)
    src_entries = sum(len(pl.track_rb_ids) for pl in lib.playlists)
    print(f"  source: {len(lib.tracks)} tracks, {len(lib.playlists)} playlists, "
          f"{src_entries} playlist entries")

    scratch = root / _SCRATCH_LIBRARY
    seen: Counter[str] = Counter()
    baseline: dict[str, list[int]] | None = None
    original_dirname = build_mod.ENGINE_LIBRARY_DIRNAME
    try:
        # Redirect only the output. drive_root stays the real stick so that
        # .resolve()-based path arithmetic matches a real conversion exactly.
        build_mod.ENGINE_LIBRARY_DIRNAME = _SCRATCH_LIBRARY
        for i in range(1, runs + 1):
            shutil.rmtree(scratch, ignore_errors=True)
            report = ConversionReport()
            m_db = build_mod.build_library(
                lib, drive_root=root, report=report, with_artwork=artwork
            )
            _assert_paths_are_faithful(m_db, len(lib.tracks))

            chains = entity_chains(m_db)
            fp = _hash(chains)
            seen[fp] += 1
            written = sum(len(v) for v in chains.values())
            if baseline is None:
                baseline = chains
                # Keep the first database. Without a reference copy a later
                # divergence can only be described, not diffed — which is
                # exactly how the original evidence was lost.
                keep_dir.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(m_db, keep_dir / "baseline.m.db")
                print(f"  run {i}: fp={fp}  entries_written={written}  "
                      f"(source {src_entries}, delta {written - src_entries:+d})")
                print(f"         baseline preserved -> {keep_dir / 'baseline.m.db'}")
            else:
                same = fp == _hash(baseline)
                print(f"  run {i}: fp={fp}  entries_written={written}  "
                      f"{'same' if same else '*** DIFFERENT ***'}")
                if not same:
                    report_chain_diff(baseline, chains)
                    kept = keep_dir / f"diverged-run{i}.m.db"
                    keep_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(m_db, kept)
                    print(f"         *** DIVERGING DB PRESERVED -> {kept} ***")
    finally:
        build_mod.ENGINE_LIBRARY_DIRNAME = original_dirname
        shutil.rmtree(scratch, ignore_errors=True)

    print(f"  distinct fingerprints: {len(seen)}  ->", dict(seen))
    if len(seen) == 1:
        print("  VERDICT: writer is deterministic across these runs")
    else:
        print("  VERDICT: *** writer is NON-deterministic — reproduced ***")


# --------------------------------------------------------------------------
# diffs
# --------------------------------------------------------------------------
def report_membership_diff(a: dict[str, list[int]], b: dict[str, list[int]]) -> None:
    for key in sorted(set(a) | set(b)):
        va, vb = a.get(key, []), b.get(key, [])
        if va != vb:
            print(f"    playlist rb_id={key}: {len(va)} -> {len(vb)} entries")
            print(f"      only in A: {sorted((Counter(va) - Counter(vb)).elements())}")
            print(f"      only in B: {sorted((Counter(vb) - Counter(va)).elements())}")


def report_chain_diff(a: dict[str, list[int]], b: dict[str, list[int]]) -> None:
    for key in sorted(set(a) | set(b)):
        va, vb = a.get(key, []), b.get(key, [])
        if va != vb:
            print(f"    {key}: {len(va)} -> {len(vb)} entries")
            print(f"      only in baseline: {sorted((Counter(va) - Counter(vb)).elements())}")
            print(f"      only in this run: {sorted((Counter(vb) - Counter(va)).elements())}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("drive_root", type=Path)
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--stage", choices=("reader", "writer", "both"), default="both")
    ap.add_argument(
        "--no-artwork",
        action="store_true",
        help="skip artwork extraction: much faster, but no longer the "
             "configuration the observed failure ran under",
    )
    ap.add_argument(
        "--keep-dir",
        type=Path,
        default=None,
        help="where a baseline and any diverging m.db are preserved "
             "(default: a fresh temp directory, reported on stdout)",
    )
    args = ap.parse_args()

    if args.runs < 1:
        print("--runs must be >= 1", file=sys.stderr)
        return 2

    root = args.drive_root
    if not (root / "PIONEER").is_dir():
        print(f"no PIONEER/ under {root}", file=sys.stderr)
        return 2

    # Databases are hundreds of MB and contain the user's library; never
    # default to dropping them into the working tree.
    keep_dir = args.keep_dir or Path(tempfile.mkdtemp(prefix="rb2engine-repro-"))

    if args.stage in ("reader", "both"):
        run_reader_stage(root, args.runs)
    if args.stage in ("writer", "both"):
        run_writer_stage(
            root, args.runs, artwork=not args.no_artwork, keep_dir=keep_dir
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
