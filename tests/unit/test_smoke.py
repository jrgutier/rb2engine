"""Smoke test: package import and version pin the toolchain end-to-end."""

import rb2engine


def test_version_is_nonempty_string() -> None:
    """rb2engine must expose a non-empty __version__ so packaging/import work."""
    assert isinstance(rb2engine.__version__, str)
    assert rb2engine.__version__
