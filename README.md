<div align="center">

<img width="200" height="200" alt="Untitled" src="https://github.com/user-attachments/assets/555b8331-af04-47d1-85c8-cf464ec6b69d" title="Pixel art beholder generated with Nano Banana"/>


# *datalab* beholder


</div>

A daemon that watches instrument directories and syncs file metadata to a [datalab](https://github.com/datalab-org/datalab-api) instance. Researchers see available files in the datalab UI in near-real-time and can request specific files for transfer — without exposing instrument PCs to the internet.

## How it works

1. **Startup**: a full scan of each watched directory seeds a local SQLite state database, then pushes the initial snapshot to the datalab server.
2. **Watching**: a `watchdog`-based filesystem watcher detects creates, modifications, and deletions in real-time and updates the local state. Events are debounced (5 s window) to avoid thrashing during bulk writes.
3. **Push loop**: on a configurable interval (default 20 min), accumulated changes are read from local state and pushed to the server. No re-scanning happens here — the watcher keeps state up to date.
4. **File request loop**: on a separate interval (default 60 s), the daemon polls the server for files that users have requested through the datalab UI, then uploads them.

All network traffic is outbound only — no inbound firewall rules are needed on the instrument PC.

## Installation

```
pip install datalab-beholder
```

Or with [uv](https://docs.astral.sh/uv/):

```
uv pip install datalab-beholder
```

## Quick start

Generate a config template:

```
datalab-beholder init
```

This writes `~/.datalab-beholder/config.yaml`. Edit it to point at your datalab instance and the directories you want to watch:

```yaml
datalab:
  url: "https://datalab.example.org"
  api_key: "your-api-key-here"

watched_paths:
  - path: "/mnt/instrument/data"
    name: "XRD-Lab-A"
    include_patterns: ["*.raw", "*.csv"]
    exclude_patterns: ["*.tmp"]
    max_depth: null

sync:
  metadata_interval: 1200   # push changes every 20 minutes
  file_request_poll: 60     # check for file requests every minute

log_level: info
```

The API key can also be set via the `DATALAB_API_KEY` environment variable.

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

## Configuration

| Section | Field | Default | Description |
|---------|-------|---------|-------------|
| `datalab` | `url` | *(required)* | URL of the datalab instance |
| `datalab` | `api_key` | *(required)* | API key (or set `DATALAB_API_KEY`) |
| `watched_paths[]` | `path` | *(required)* | Directory to watch |
| `watched_paths[]` | `name` | *(required)* | Label shown in the datalab UI |
| `watched_paths[]` | `include_patterns` | `["*"]` | Glob patterns for files to include |
| `watched_paths[]` | `exclude_patterns` | `[]` | Glob patterns for files to exclude |
| `watched_paths[]` | `max_depth` | unlimited | Max directory traversal depth |
| `sync` | `metadata_interval` | `1200` | Seconds between metadata pushes |
| `sync` | `file_request_poll` | `60` | Seconds between file request polls |
| | `log_level` | `info` | Logging verbosity |
| | `state_db` | `~/.datalab-beholder/state.db` | Path to the local SQLite database |

## Development

```
git clone https://github.com/datalab-org/datalab-beholder.git
cd datalab-beholder
uv sync
uv run pytest -v
```

See [DESIGN.md](DESIGN.md) for the original design notes and motivation.
