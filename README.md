<div align="center">

<img width="200" height="200" alt="Untitled" src="https://github.com/user-attachments/assets/555b8331-af04-47d1-85c8-cf464ec6b69d" title="Pixel art beholder generated with Nano Banana"/>


# *datalab* beholder


</div>

> ⚠️ **Under construction.** APIs, config shape, and server endpoints are still in flux. Not yet recommended for production deployments.

A daemon that watches instrument directories and syncs file metadata to one or more [*datalab*](https://github.com/datalab-org/datalab) instances. Researchers see available files in the datalab UI in near-real-time and can request specific files for transfer — without exposing instrument PCs to the internet.

Two modes of operation, which can be combined per watched path:

- **Metadata exposure** — surface every matching file in the *datalab* UI as a browsable listing; users request files on demand and the daemon uploads them.
- **Direct attach** — extract a datalab `item_id` from each file path with a regex (`id_patterns`) and have the daemon push the file straight onto that item as it appears, no user action required.

## How it works

1. **Startup**: a full scan of each watched directory seeds a local SQLite state database, then pushes the initial snapshot to the datalab server.
2. **Watching**: a `watchdog`-based filesystem watcher detects creates, modifications, and deletions in real-time and updates the local state. Events are debounced (5 s window) to avoid thrashing during bulk writes.
3. **Push loop**: on a configurable interval (default 20 min), accumulated changes are read from local state and pushed to the server. No re-scanning happens here — the watcher keeps state up to date.
4. **File request loop**: on a separate interval (default 60 s), the daemon polls the server for files that users have requested through the datalab UI, then uploads them.

All network traffic is outbound only — no inbound firewall rules are needed on the instrument PC.

## Installation

Either clone this repository and `uv pip install .` (or add to your
`pyproject.toml`), or use the standalone PyInstaller executable published for
each release on GitHub for your platform.

## Quick start

Generate a config template:

```
datalab-beholder init
```

This writes `~/.datalab-beholder/config.yaml`. Edit it to point at your datalab instance and the directories you want to watch:

```yaml
datalabs:
  - name: "main"
    url: "https://datalab.example.org"
    api_key: "your-api-key-here"   # or omit and use a <PREFIX>_DATALAB_API_KEY env var

watched_paths:
  # Plain metadata-exposure path: files appear in the UI for users to request.
  - path: "/mnt/instrument/data"
    name: "XRD-Lab-A"
    datalab: "main"
    include_patterns: ["*.raw", "*.csv"]
    exclude_patterns: ["*.tmp"]
    max_depth: null

  # Direct-attach path: files are auto-attached to existing items via item_id
  # extracted from the file path.
  - path: "/mnt/instrument/echem"
    name: "digibat"
    datalab: "main"
    item_type: "cells"
    include_patterns: ["*.mpr"]
    id_patterns:
      - "^(?P<group_id>P[0-9]{3,4})/(?P<item_id>[0-9]+)[-_].*\\.mpr$"

sync:
  metadata_interval: 1200   # push changes every 20 minutes
  file_request_poll: 60     # check for file requests every minute

log_level: info
```

Multiple `datalabs` entries are supported; each `watched_paths[].datalab` field references one by name. API keys are resolved by the underlying datalab client, which transparently checks `<PREFIX>_DATALAB_API_KEY` env vars (where `<PREFIX>` matches the deployment's identifier prefix) when no literal key is provided.

### Direct attach via `id_patterns`

`id_patterns` is a list of Python regexes with named capture groups. Each file path (relative to the watched root) is tested against the patterns; the first match wins, and its captured groups are attached to the file entry. Allowed group names are:

- `item_id` — **required** in every pattern; identifies the existing datalab item to attach to.
- `group_id` — optional; the parent group/project on the datalab side.
- `collection_id` — optional; a collection identifier.

Files that don't match any pattern are silently skipped from the direct-attach path. Combine with `item_type` to scope what kind of item the daemon attaches files to.

Start the daemon:

```
datalab-beholder start
```

To use a config file in a different location:

```
datalab-beholder start --config /path/to/config.yaml
```

## CLI reference

### `datalab-beholder init`

Creates a config template at `~/.datalab-beholder/config.yaml` (or at `--path`).

```
datalab-beholder init --path ./my-config.yaml
```

### `datalab-beholder start`

Starts the daemon. Blocks until interrupted (Ctrl+C / SIGTERM).

```
datalab-beholder start [--config PATH] [--log-level debug|info|warning|error]
```

### `datalab-beholder scan`

One-off directory scan — useful for testing your patterns before running the daemon.

```
datalab-beholder scan /mnt/instrument/data \
  --name "XRD-Lab-A" \
  --include "*.raw" --include "*.csv" \
  --exclude "*.tmp" \
  --max-depth 3 \
  --pretty
```

Outputs structured JSON describing the directory contents.

### `datalab-beholder status`

Shows the current state database and recent sync history.

```
datalab-beholder status [--config PATH]
```

### `datalab-beholder gui`

Launches a small Tkinter status GUI showing connection state per configured datalab, recent activity, and a settings editor for the YAML config. Driven by the same `tick()` loop as the CLI daemon.

```
datalab-beholder gui [--config PATH]
```

## Standalone executable (PyInstaller)

For deployment onto instrument PCs without a Python install, a single-file executable can be built with [PyInstaller](https://pyinstaller.org/) using the bundled `beholder.spec`:

```
uv run pyinstaller beholder.spec
```

The resulting binary in `dist/` launches the GUI and reads `config.yaml` from the directory next to the executable. Pre-built Windows binaries are produced by the project's CI workflow.

## Configuration

| Section | Field | Default | Description |
|---------|-------|---------|-------------|
| `datalabs[]` | `name` | *(required)* | Unique label referenced by `watched_paths[].datalab` |
| `datalabs[]` | `url` | *(required)* | URL of the datalab instance |
| `datalabs[]` | `api_key` | *(optional)* | API key; if omitted, resolved from env by the datalab client |
| `watched_paths[]` | `path` | *(required)* | Directory to watch |
| `watched_paths[]` | `name` | *(required)* | Label shown in the datalab UI |
| `watched_paths[]` | `datalab` | *(required if >1 datalab)* | Name of the datalab instance to push to |
| `watched_paths[]` | `include_patterns` | `["*"]` | Glob patterns for files to include |
| `watched_paths[]` | `exclude_patterns` | `[]` | Glob patterns for files to exclude |
| `watched_paths[]` | `id_patterns` | `[]` | Regexes with named `item_id`/`group_id`/`collection_id` groups for direct attach |
| `watched_paths[]` | `item_type` | `null` | Datalab item type for direct attach (e.g. `cells`, `samples`) |
| `watched_paths[]` | `max_depth` | `10` | Max directory traversal depth |
| `sync` | `metadata_interval` | `1200` | Seconds between metadata pushes |
| `sync` | `file_request_poll` | `60` | Seconds between file request polls |
| | `log_level` | `info` | Logging verbosity |
| | `state_db` | next to package/exe | Path to the local SQLite database |

## Development

```
git clone https://github.com/datalab-org/datalab-beholder.git
cd datalab-beholder
uv sync
uv run pytest -v
```

See [DESIGN.md](DESIGN.md) for the original design notes and motivation.
