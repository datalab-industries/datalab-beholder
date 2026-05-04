"""Three-stage end-to-end smoke test against a real datalab instance.

Drives `BeholderDaemon` against ``config.yaml`` in this directory (which
points at ``http://localhost:8080`` by default) and walks the file tree
through three mutations, checking after each one that the items /
attached files on the server reflect the local state.

A mock-transport version of the same flow lives next door as
``test_three_stage.py`` and runs in CI; this script is the live
counterpart, intended for manual smoke testing against a real datalab.

Run it:
    DATALAB_API_KEY=... uv run python tests/examples/three-stage/run_live.py

Item ids are prefixed with ``beholder-test-`` so they're easy to spot
(and easy to delete) on the target deployment. Re-running is idempotent
— ``ensure_item`` finds the items if they already exist and
``find_existing_file_id`` makes attachments replace-in-place rather than
duplicate.
"""

from __future__ import annotations

import logging
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from datalab_beholder.config import load_config
from datalab_beholder.daemon import BeholderDaemon

EXAMPLE_DIR = Path(__file__).resolve().parent
TREE = EXAMPLE_DIR / "instrument-pc"
CONFIG_PATH = EXAMPLE_DIR / "config.yaml"
STATE_DB = EXAMPLE_DIR / "state.db"


@dataclass(frozen=True)
class Expectation:
    """One row of the post-tick assertion table.

    ``files`` is the set of attachment basenames we expect on the item
    after the stage completes. None means "don't care" (item must
    exist, files unchecked).
    """

    item_id: str
    files: set[str] | None = None


# --------------------------------------------------------------------------
# Tree mutations
# --------------------------------------------------------------------------


def reset_tree() -> None:
    """Wipe the watched tree and the daemon's state DB so each run
    starts from a known baseline."""
    if TREE.exists():
        shutil.rmtree(TREE)
    TREE.mkdir(parents=True)
    STATE_DB.unlink(missing_ok=True)


def write_file(rel_path: str, content: bytes) -> None:
    full = TREE / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(content)


def stage1_initial() -> None:
    """Two cells in PROJ-A; one unmatched note file."""
    write_file("PROJ-A/beholder-test-001-cycle1.mpr", b"\x00" * 64)
    write_file("PROJ-A/beholder-test-002-formation.mpr", b"\x00" * 128)
    write_file("notes.txt", b"ignore me")


def stage2_modify_and_add() -> None:
    """Modify cell 001's existing file (size changes → counts as
    'modified'), and add a brand-new cell 003."""
    write_file("PROJ-A/beholder-test-001-cycle1.mpr", b"\xff" * 96)
    write_file("PROJ-A/beholder-test-003-rate.mpr", b"\x00" * 32)


def stage3_second_file_for_existing_item() -> None:
    """Add a *second* attachment to cell 001."""
    write_file("PROJ-A/beholder-test-001-cycle2.mpr", b"\x11" * 48)


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------


def verify(daemon: BeholderDaemon, expectations: list[Expectation]) -> list[str]:
    """Query the server (via the daemon's already-built client) and
    return a list of human-readable failure messages, empty on success.
    """
    # Single-datalab config in this example, so just grab the first.
    client = next(iter(daemon.clients.values()))
    failures: list[str] = []

    for exp in expectations:
        item = client.fetch_item(exp.item_id)
        if item is None:
            failures.append(f"item {exp.item_id!r} not found on server")
            continue
        if exp.files is None:
            continue
        attached = {f.get("name") for f in item.get("files") or []}
        missing = exp.files - attached
        if missing:
            failures.append(
                f"item {exp.item_id!r} missing attachments: {sorted(missing)} "
                f"(server has: {sorted(attached)})"
            )
    return failures


def run_stage(
    name: str,
    mutate: Callable[[], None],
    daemon: BeholderDaemon,
    expectations: list[Expectation],
) -> bool:
    print(f"\n=== {name} ===")
    mutate()
    print(
        f"  files now on disk: {sorted(p.name for p in TREE.rglob('*') if p.is_file())}"
    )

    # Drive the daemon: one tick to scan, one tick to attach (intervals
    # are 0 in the example config, so both fit in a single tick window).
    daemon.tick()

    failures = verify(daemon, expectations)
    if failures:
        print("  FAIL")
        for f in failures:
            print(f"    - {f}")
        return False
    print("  OK")
    for exp in expectations:
        files_repr = (
            "any" if exp.files is None else ", ".join(sorted(exp.files)) or "(none)"
        )
        print(f"    - {exp.item_id}: files = {files_repr}")
    return True


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    if not CONFIG_PATH.exists():
        print(f"missing config: {CONFIG_PATH}", file=sys.stderr)
        return 2

    reset_tree()
    config = load_config(CONFIG_PATH)
    daemon = BeholderDaemon(config)
    daemon.setup()

    stages = [
        (
            "Stage 1: initial sync",
            stage1_initial,
            [
                Expectation(
                    "PROJ-A-beholder-test-001",
                    {"beholder-test-001-cycle1.mpr"},
                ),
                Expectation(
                    "PROJ-A-beholder-test-002",
                    {"beholder-test-002-formation.mpr"},
                ),
            ],
        ),
        (
            "Stage 2: modify cell 001 + add cell 003",
            stage2_modify_and_add,
            [
                Expectation(
                    "PROJ-A-beholder-test-001",
                    {"beholder-test-001-cycle1.mpr"},
                ),
                Expectation(
                    "PROJ-A-beholder-test-002",
                    {"beholder-test-002-formation.mpr"},
                ),
                Expectation(
                    "PROJ-A-beholder-test-003",
                    {"beholder-test-003-rate.mpr"},
                ),
            ],
        ),
        (
            "Stage 3: add a second attachment to cell 001",
            stage3_second_file_for_existing_item,
            [
                Expectation(
                    "PROJ-A-beholder-test-001",
                    {
                        "beholder-test-001-cycle1.mpr",
                        "beholder-test-001-cycle2.mpr",
                    },
                ),
            ],
        ),
    ]

    all_ok = True
    for name, mutate, expectations in stages:
        if not run_stage(name, mutate, daemon, expectations):
            all_ok = False
            break

    daemon.shutdown()

    print()
    if all_ok:
        print("All stages passed.")
        return 0
    print("Stages failed — see output above.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
