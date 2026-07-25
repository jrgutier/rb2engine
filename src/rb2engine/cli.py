"""click app: convert | inspect | verify | doctor; exit codes 0/1/2.

Only ``inspect`` is implemented in this pass. Other commands exit 2 with an
honest "not implemented yet" message rather than pretending success.

``inspect`` is strictly read-only: it never writes under the drive, and exits
0 even when the source library carries warnings/skips (inspection ≠ conversion).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import click

from rb2engine import __version__
from rb2engine.errors import FatalError, UnsupportedFormatError
from rb2engine.logging import configure_logging, log_event


def load_source_library(drive: Path) -> Any:
    """Parse a rekordbox USB export into a SourceLibrary.

    Reader modules are imported **lazily** so ``--help`` and the report layer
    work while pdb/anlz are still landing. Discovers a library-producing
    entrypoint on ``reader.pdb`` (or a future ``reader`` package-level
    helper). Does **not** call ``scan.scan_drive`` — that returns a layout
    descriptor, not a SourceLibrary.
    """
    drive = Path(drive)

    # Future package-level orchestration (if/when added).
    try:
        from rb2engine import reader as reader_pkg
    except ImportError:
        reader_pkg = None  # type: ignore[assignment]

    if reader_pkg is not None:
        for name in ("read_library", "load_library", "open_drive"):
            fn = getattr(reader_pkg, name, None)
            if callable(fn):
                return fn(drive)

    try:
        from rb2engine.reader import pdb as pdb_mod
    except ImportError as exc:
        raise FatalError(
            "reader.pdb is unavailable; cannot inspect this drive yet."
        ) from exc

    for name in ("read_library", "load_library", "parse_library", "open_pdb"):
        fn = getattr(pdb_mod, name, None)
        if callable(fn):
            return fn(drive)

    raise FatalError(
        "Source reader is not available yet "
        "(reader/pdb.py exposes no SourceLibrary load entrypoint). "
        "inspect requires a working reader, or a test double via "
        "rb2engine.cli.load_source_library."
    )


def _filter_library_track(library: Any, track_id: int) -> Any:
    """Return a SourceLibrary containing only *track_id* (same drive_root)."""
    from rb2engine.ir import SourceLibrary

    if track_id not in library.tracks:
        raise click.ClickException(f"track id {track_id} not found in source library")
    return SourceLibrary(
        drive_root=library.drive_root,
        tracks={track_id: library.tracks[track_id]},
        playlists=list(library.playlists),
        warnings=list(library.warnings),
    )


@click.group()
@click.version_option(version=__version__, prog_name="rb2engine")
@click.option(
    "-v",
    "--verbose",
    count=True,
    help="Increase log verbosity (-v info, -vv debug). Logs go to stderr.",
)
@click.option(
    "--log-json",
    is_flag=True,
    help="Emit one JSON log object per line on stderr (stdout stays for report/inspect).",
)
@click.pass_context
def main(ctx: click.Context, verbose: int, log_json: bool) -> None:
    """Convert rekordbox USB exports to Engine DJ libraries."""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    ctx.obj["log_json"] = log_json
    configure_logging(verbose=verbose, log_json=log_json)


@main.command("inspect")
@click.argument(
    "drive",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Dump canonical IR JSON (SourceLibrary.to_json_obj) to stdout.",
)
@click.option(
    "--track",
    "track_id",
    type=int,
    default=None,
    help="Restrict dump to a single rekordbox track id.",
)
@click.pass_context
def inspect_cmd(
    ctx: click.Context,
    drive: Path,
    as_json: bool,
    track_id: int | None,
) -> None:
    """Parse the source and dump the IR without writing anything.

    Primary debugging tool and the source of golden_ir.json. Always exits 0
    on a successful parse, even when the library has warnings.
    """
    log_event("inspect", "start", detail=str(drive), level="info")
    try:
        library = load_source_library(drive)
    except (FatalError, UnsupportedFormatError) as exc:
        click.echo(str(exc), err=True)
        ctx.exit(2)
    except Exception as exc:  # noqa: BLE001 - top-level guard: any failure becomes exit 2, never a traceback
        click.echo(f"inspect failed: {exc}", err=True)
        ctx.exit(2)

    if track_id is not None:
        library = _filter_library_track(library, track_id)

    if as_json:
        # CRITICAL: use IR canonicalization — do not re-serialize paths here.
        obj = library.to_json_obj()
        sys.stdout.write(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")
    else:
        n_tracks = len(library.tracks)
        n_playlists = len(library.playlists)
        n_warnings = len(library.warnings)
        click.echo(f"drive:      {library.drive_root}")
        click.echo(f"tracks:     {n_tracks}")
        click.echo(f"playlists:  {n_playlists}")
        click.echo(f"warnings:   {n_warnings}")
        if library.warnings:
            for w in library.warnings:
                click.echo(f"  - {w}")
        for rb_id in sorted(library.tracks.keys()):
            t = library.tracks[rb_id]
            click.echo(f"  [{rb_id}] {t.artist} — {t.title}")

    log_event("inspect", "done", detail={"tracks": len(library.tracks)}, level="info")
    # Inspection is not conversion: always 0 on successful parse.
    ctx.exit(0)


def _not_implemented(ctx: click.Context, name: str) -> None:
    click.echo(f"{name} is not implemented yet", err=True)
    ctx.exit(2)


@main.command("convert")
@click.argument(
    "drive",
    type=click.Path(exists=False, path_type=Path),
    required=False,
)
@click.option("--dry-run", is_flag=True, help="Parse and map without writing (future).")
@click.option("--database-uuid", default=None, help="Override Information.uuid (future).")
@click.option(
    "--path-base",
    type=click.Choice(["engine-lib", "drive-root", "absolute"], case_sensitive=False),
    default=None,
    help="Track.path base strategy (future; absolute is diagnostic-only).",
)
@click.option("--target-schema", default=None, help="Engine schema triple, e.g. 3.0.1.")
@click.option(
    "--report",
    "report_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Override JSON report path (default: Engine Library/rb2engine-report.json).",
)
@click.option("--no-artwork", is_flag=True, help="Skip album art extraction/writes.")
@click.pass_context
def convert_cmd(
    ctx: click.Context,
    drive: Path | None,
    dry_run: bool,
    database_uuid: str | None,
    path_base: str | None,
    target_schema: str | None,
    report_path: Path | None,
    no_artwork: bool,
) -> None:
    """Convert a rekordbox USB export into an Engine Library (not yet implemented)."""
    _ = (drive, dry_run, database_uuid, path_base, target_schema, report_path, no_artwork)
    _not_implemented(ctx, "convert")


@main.command("verify")
@click.argument(
    "drive",
    type=click.Path(exists=False, path_type=Path),
    required=False,
)
@click.pass_context
def verify_cmd(ctx: click.Context, drive: Path | None) -> None:
    """Decode written m.db and diff against source IR (not yet implemented)."""
    _ = drive
    _not_implemented(ctx, "verify")


@main.command("doctor")
@click.option(
    "--engine-db",
    type=click.Path(path_type=Path),
    default=None,
    help="Existing Engine m.db to check schema support for.",
)
@click.pass_context
def doctor_cmd(ctx: click.Context, engine_db: Path | None) -> None:
    """Report tool version, bundled DDL versions, schema support (not yet implemented)."""
    _ = engine_db
    _not_implemented(ctx, "doctor")


if __name__ == "__main__":  # pragma: no cover
    main()
