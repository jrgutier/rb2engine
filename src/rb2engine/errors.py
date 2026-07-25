"""Exception types: UnsupportedFormatError (fatal, exit 2), TrackSkipped (soft, exit 1), FatalError (exit 2)."""


class UnsupportedFormatError(Exception):
    """Fatal: unsupported or unreadable source/target format. Maps to process exit code 2."""


class TrackSkipped(Exception):
    """Soft: a single track could not be converted. Any occurrence maps to process exit code 1."""


class FatalError(Exception):
    """Fatal: unrecoverable conversion failure. Maps to process exit code 2."""
