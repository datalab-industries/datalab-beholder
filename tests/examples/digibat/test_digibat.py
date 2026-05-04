"""Scan the digibat fixture tree and verify the regex + template combo
resolves the expected ``item_id``s.

This locks in the contract that the example config in this directory
documents: P-prefixed project dirs at any depth, with the file's last
``[/_-]<digits>[_-]`` sandwich captured as the item id.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from datalab_beholder.scanner import scan_directory

HERE = Path(__file__).parent


def test_digibat_scan_matches_expected_ids() -> None:
    config = yaml.safe_load((HERE / "config.yaml").read_text())
    wp = config["watched_paths"][0]

    result = scan_directory(
        root=HERE,
        name=wp["name"],
        include_patterns=wp["include_patterns"],
        id_patterns=wp["id_patterns"],
        item_id_template=wp.get("item_id_template"),
        collection_id_template=wp.get("collection_id_template"),
    )

    by_path = {e.path: e.ids for e in result.entries}

    assert by_path == {
        "P011/1111_test.mpr": {
            "group_id": "P011",
            "item_id": "P011/1111",
        },
        "P012/subdir/P012-CEL-1112-test.mpr": {
            "group_id": "P012",
            "item_id": "P012/1112",
        },
    }

    # `P011/test.mpr` matches the include glob but has no digits to capture;
    # `xyz/1111-test.mpr` lacks the `P` prefix. Both must be skipped.
    assert "P011/test.mpr" not in by_path
    assert "xyz/1111-test.mpr" not in by_path
