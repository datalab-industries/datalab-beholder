# Block modes example

Shows how `block_mode` changes the blocks created for files matched by
`block_patterns`. The `data/` folder holds four small CSVs:

```
data/
├── bmdemo1-cycle1.csv   ┐
├── bmdemo1-cycle2.csv   ├─ three files for item bmdemo1
├── bmdemo1-cycle3.csv   ┘
└── bmdemo2-cycle1.csv   ── one file for item bmdemo2
```

[`config.yaml`](config.yaml) watches that folder three times, once per
mode, and gives each mode its own item ids so the results can be compared
side by side:

| watched path | `block_mode`         | item `bmdemo1-…` ends up with              |
|--------------|----------------------|--------------------------------------------|
| `per-file`   | `per_file` (default) | 3 `tabular` blocks, one per file           |
| `per-item`   | `per_item`           | 1 `tabular` block, wired to one file       |
| `all-files`  | `per_item_all_files` | 1 `tabular` block, wired to all 3 files    |

`bmdemo2-…` only has one file, so every mode gives it a single block.

Under `per_item` the block goes to whichever file is attached first, which
follows scan order rather than filename order.

[`test_block_modes.py`](test_block_modes.py) runs this config against an
in-memory datalab as part of the test suite, including the "things to
try" below:

```sh
uv run pytest tests/examples/block-modes
```

## Preview without touching a server

Edit the `url` in `config.yaml` (and set `DATALAB_API_KEY`), then from the
repository root:

```sh
uv run datalab-beholder dry-run --config tests/examples/block-modes/config.yaml
```

This scans `data/` and asks the server (read-only) what already exists;
the summary lists, per watched path, the items, uploads, and
`create_block` / `update_block` actions a real run would perform. If the
server can't be reached you still get the scan, with every file reported
as "server state unknown".

## Run it for real

```sh
uv run datalab-beholder start --config tests/examples/block-modes/config.yaml
```

This creates six sample items (`bmdemo1-per-file`, `bmdemo1-per-item`,
`bmdemo1-all-files`, and the same three for `bmdemo2`) on the configured
instance. Stop it with Ctrl+C once the log shows the blocks being created.

Things to try:

- Drop a `bmdemo1-cycle4.csv` into `data/`: `per-file` adds a fourth block,
  `per-item` just attaches the file, and `all-files` adds it to the
  existing block.
- Delete `state.db` to make the daemon treat every file as new again.
  Existing blocks are recognised, so this doesn't create duplicates.

Sync state lives in `state.db` next to the config (ignored by git).
Whether a block actually *displays* several files at once is up to that
block type on the server — swap `tabular` for a type that handles multiple
files (e.g. `cycle` with real cycler files) if your instance's `tabular`
block only shows the first one.
