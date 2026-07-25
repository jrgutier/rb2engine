"""Source artwork: mutagen embedded tags (primary); PIONEER/Artwork/ (secondary).

Image bytes never live in the IR long-term: SourceArtwork carries a Path +
content_key; call read_artwork_bytes when the writer needs the BLOB payload.

Read-only with respect to audio files — never mutagen save() / re-tag.
"""

from __future__ import annotations

import warnings
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from mutagen import File as MutagenFile
from mutagen.mp4 import MP4

from rb2engine.ir import SourceArtwork
from rb2engine.ir_engine import artwork_content_hash

SOURCE_EMBEDDED = "embedded"
SOURCE_PDB = "pdb"


def _warn(message: str) -> None:
    warnings.warn(message, UserWarning, stacklevel=3)


def _looks_like_image(data: bytes) -> bool:
    """Reject garbage that would land as a useless AlbumArt BLOB."""
    if len(data) < 8:
        return False
    if data.startswith(b"\xff\xd8\xff"):  # JPEG
        return True
    if data.startswith(b"\x89PNG\r\n\x1a\n"):  # PNG
        return True
    if data.startswith((b"GIF87a", b"GIF89a")):
        return True
    if data.startswith(b"BM"):  # BMP
        return True
    return bool(data.startswith(b"RIFF") and data[8:12] == b"WEBP")


def _picture_bytes_from_mutagen(audio: object) -> bytes | None:
    """Return the first usable embedded picture payload, or None."""
    if audio is None:
        return None

    pictures = getattr(audio, "pictures", None)
    if pictures:
        data = getattr(pictures[0], "data", None)
        if data:
            return bytes(data)

    tags = getattr(audio, "tags", None)
    if tags is None:
        return None

    # MP4 / M4A `covr`
    if isinstance(audio, MP4) or (hasattr(tags, "__contains__") and "covr" in tags):
        try:
            covers = tags["covr"]  # type: ignore[index]
        except (KeyError, TypeError, AttributeError, ValueError):
            covers = None
        if covers:
            return bytes(covers[0])

    # ID3 APIC / PIC — MP3, AIFF, WAV (and other ID3FileType containers)
    try:
        keys = list(tags.keys())  # type: ignore[attr-defined]
    except (TypeError, AttributeError, ValueError):
        return None

    frames: list[object] = []
    for key in keys:
        sk = str(key)
        if sk.startswith(("APIC", "PIC")):
            frames.append(tags[key])  # type: ignore[index]
    if not frames:
        return None

    # Prefer front cover (PictureType 3) when present.
    for frame in frames:
        ptype = getattr(frame, "type", None)
        data = getattr(frame, "data", None)
        if data and ptype is not None and int(ptype) == 3:
            return bytes(data)
    data = getattr(frames[0], "data", None)
    return bytes(data) if data else None


def _read_embedded_image_bytes(track_path: Path) -> bytes | None:
    """Open audio read-only and return embedded image bytes, or None.

    Corrupt/undecodable payloads warn and return None — never raise.
    """
    path = Path(track_path)
    try:
        audio = MutagenFile(path)
    except Exception as exc:  # noqa: BLE001 - unreadable file is skipped+reported, never fatal
        _warn(f"artwork: cannot open {path}: {exc}")
        return None
    if audio is None:
        return None
    try:
        image = _picture_bytes_from_mutagen(audio)
    except Exception as exc:  # noqa: BLE001 - corrupt art is skipped+reported, never fatal
        _warn(f"artwork: failed reading embedded picture from {path}: {exc}")
        return None
    if not image:
        return None
    if not _looks_like_image(image):
        _warn(f"artwork: undecodable/corrupt image data in {path}")
        return None
    return image


def extract_artwork(track_path: Path) -> SourceArtwork | None:
    """Extract embedded album art from an audio file (primary source).

    Returns None when the track has no usable embedded art. Never writes to
    or re-saves the audio file.
    """
    path = Path(track_path)
    image = _read_embedded_image_bytes(path)
    if image is None:
        return None
    return SourceArtwork(
        content_key=artwork_content_hash(image),
        path=path,
        source=SOURCE_EMBEDDED,
    )


def extract_pdb_artwork(image_path: Path) -> SourceArtwork | None:
    """Secondary source: a file under PIONEER/Artwork/ (or any image path).

    Empty on the measured stick; retained for exports that populate it.
    """
    path = Path(image_path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        _warn(f"artwork: cannot read pdb artwork {path}: {exc}")
        return None
    if not data or not _looks_like_image(data):
        if data:
            _warn(f"artwork: undecodable/corrupt pdb artwork {path}")
        return None
    return SourceArtwork(
        content_key=artwork_content_hash(data),
        path=path,
        source=SOURCE_PDB,
    )


def read_artwork_bytes(art: SourceArtwork) -> bytes | None:
    """Load image bytes for a SourceArtwork (on demand; not stored in the IR).

    embedded → re-extract picture frames from the audio at art.path.
    pdb → read the image file at art.path.
    """
    if art.path is None:
        return None
    if art.source == SOURCE_PDB:
        try:
            data = art.path.read_bytes()
        except OSError:
            return None
        return data or None
    return _read_embedded_image_bytes(art.path)


@dataclass
class ArtworkSourceReport:
    """Probe of which source supplied art — feeds M3 timing + conversion report."""

    tracks_seen: int = 0
    tracks_with_embedded: int = 0
    tracks_with_pdb: int = 0
    tracks_with_neither: int = 0
    unique_images: int = 0
    total_unique_bytes: int = 0
    assigned_from_embedded: int = 0
    assigned_from_pdb: int = 0
    skipped_corrupt: int = 0


@dataclass
class ArtworkIndex:
    """Dedup-aware batch result.

    ``unique`` is first-seen order over track iteration — that order becomes
    AlbumArt AUTOINCREMENT ids and therefore every Track.albumArtId.
    """

    unique: list[SourceArtwork] = field(default_factory=list)
    by_track: dict[Path, SourceArtwork | None] = field(default_factory=dict)
    report: ArtworkSourceReport = field(default_factory=ArtworkSourceReport)


def collect_artwork(
    track_paths: Iterable[Path],
    *,
    pdb_artwork_by_track: Mapping[Path, Path] | None = None,
) -> ArtworkIndex:
    """Batch extract with content_key dedup and a source availability report.

    Primary: embedded tags via mutagen.
    Secondary: ``pdb_artwork_by_track`` paths when embedded is absent.
    First-seen track order pins which SourceArtwork is retained per key.
    """
    pdb_map: Mapping[Path, Path] = pdb_artwork_by_track or {}
    unique: list[SourceArtwork] = []
    by_key: dict[str, SourceArtwork] = {}
    by_track: dict[Path, SourceArtwork | None] = {}
    report = ArtworkSourceReport()
    unique_sizes: dict[str, int] = {}

    for raw in track_paths:
        track_path = Path(raw)
        report.tracks_seen += 1

        image = _read_embedded_image_bytes(track_path)
        source = SOURCE_EMBEDDED
        had_embedded = image is not None
        if had_embedded:
            report.tracks_with_embedded += 1
        else:
            pdb_path = pdb_map.get(track_path)
            if pdb_path is not None:
                pdb_art = extract_pdb_artwork(Path(pdb_path))
                if pdb_art is not None:
                    # Load bytes once for size/hash consistency with first-seen.
                    image = read_artwork_bytes(pdb_art)
                    if image is not None:
                        source = SOURCE_PDB
                        report.tracks_with_pdb += 1

        if image is None:
            report.tracks_with_neither += 1
            by_track[track_path] = None
            continue

        key = artwork_content_hash(image)
        existing = by_key.get(key)
        if existing is None:
            art = SourceArtwork(
                content_key=key,
                path=track_path if source == SOURCE_EMBEDDED else Path(pdb_map[track_path]),
                source=source,
            )
            by_key[key] = art
            unique.append(art)
            unique_sizes[key] = len(image)
            by_track[track_path] = art
            if source == SOURCE_EMBEDDED:
                report.assigned_from_embedded += 1
            else:
                report.assigned_from_pdb += 1
        else:
            by_track[track_path] = existing
            if existing.source == SOURCE_EMBEDDED:
                report.assigned_from_embedded += 1
            else:
                report.assigned_from_pdb += 1

        # Drop image reference promptly; next iteration overwrites.
        del image

    report.unique_images = len(unique)
    report.total_unique_bytes = sum(unique_sizes.values())
    return ArtworkIndex(unique=unique, by_track=by_track, report=report)
