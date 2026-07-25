# rb2engine

Convert a Pioneer rekordbox USB export into an Engine DJ library (`m.db`).

> **Status:** Milestone M0 (scaffold). Parsing, mapping, and writing are not
> implemented yet. See the implementation plan under `.omc/plans/` (local
> only; not shipped in the published tree).

## Requirements

- Python 3.11+
- [uv](https://github.com/astral-sh/uv)

## Setup

```bash
uv sync
uv run pytest
```

## CLI (planned)

```bash
rb2engine convert <drive>
rb2engine inspect <drive> [--json] [--mapped]
rb2engine verify <drive>
rb2engine doctor
```

## License

MIT. See `LICENSE` and `NOTICE` for third-party attribution
(crate-digger / EPL-2.0 vendored parser; libdjinterop as documentation only).
