"""Tests for config validation."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from datalab_beholder.config import ALLOWED_ID_GROUPS, WatchedPath


def _make(id_patterns: list[str], tmp_path: Path) -> WatchedPath:
    return WatchedPath(
        path=tmp_path,
        name="t",
        id_patterns=id_patterns,
    )


class TestIdPatternValidation:
    def test_allowed_vocab_is_what_we_expect(self) -> None:
        assert ALLOWED_ID_GROUPS == {"group_id", "item_id", "collection_id"}

    def test_no_patterns_is_fine(self, tmp_path: Path) -> None:
        wp = _make([], tmp_path)
        assert wp.id_patterns == []

    def test_single_known_group(self, tmp_path: Path) -> None:
        wp = _make([r"^(?P<item_id>[0-9]+)\.mpr$"], tmp_path)
        assert len(wp.id_patterns) == 1

    def test_multiple_known_groups(self, tmp_path: Path) -> None:
        wp = _make(
            [r"^(?P<group_id>P[0-9]{3,4})/(?P<item_id>[0-9]+)[-_].*\.mpr$"],
            tmp_path,
        )
        assert len(wp.id_patterns) == 1

    def test_all_three_known_groups(self, tmp_path: Path) -> None:
        wp = _make(
            [r"(?P<collection_id>C[0-9]+)/(?P<group_id>P[0-9]+)/(?P<item_id>[0-9]+)"],
            tmp_path,
        )
        assert len(wp.id_patterns) == 1

    def test_multiple_patterns_each_validated(self, tmp_path: Path) -> None:
        wp = _make(
            [
                r"^(?P<item_id>[0-9]+)\.mpr$",
                r"^(?P<group_id>P[0-9]+)/(?P<item_id>\d+)\.dat$",
            ],
            tmp_path,
        )
        assert len(wp.id_patterns) == 2

    def test_unknown_group_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError) as exc:
            _make([r"^(?P<sample_id>[0-9]+)\.mpr$"], tmp_path)
        msg = str(exc.value)
        assert "sample_id" in msg
        assert "unsupported capture" in msg

    def test_typo_in_known_group_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError) as exc:
            _make([r"(?P<itemid>[0-9]+)"], tmp_path)
        assert "itemid" in str(exc.value)

    def test_mixed_known_and_unknown_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError) as exc:
            _make(
                [r"(?P<group_id>P\d+)/(?P<weird>\d+)"],
                tmp_path,
            )
        msg = str(exc.value)
        # The complaint should name the offending group, not the legal one.
        assert "['weird']" in msg

    def test_no_named_groups_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError) as exc:
            _make([r"^[0-9]+\.mpr$"], tmp_path)
        assert "no named capture groups" in str(exc.value)

    def test_unnamed_group_alone_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError) as exc:
            _make([r"^([0-9]+)\.mpr$"], tmp_path)
        assert "no named capture groups" in str(exc.value)

    def test_unnamed_group_alongside_named_is_ok(self, tmp_path: Path) -> None:
        wp = _make([r"^(P\d+)/(?P<item_id>\d+)"], tmp_path)
        assert len(wp.id_patterns) == 1

    def test_invalid_regex_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError) as exc:
            _make([r"(?P<item_id>[unclosed"], tmp_path)
        assert "Invalid regex" in str(exc.value)

    def test_first_pattern_valid_second_invalid(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError):
            _make(
                [
                    r"^(?P<item_id>\d+)$",
                    r"(?P<bogus>\d+)",
                ],
                tmp_path,
            )


class TestComplexPatternsAtScanTime:
    """End-to-end sanity: complex valid patterns actually capture correctly."""

    @pytest.fixture
    def tree(self, tmp_path: Path) -> Path:
        from datalab_beholder.scanner import scan_directory  # noqa: F401

        root = tmp_path / "lab"
        (root / "C001" / "P011").mkdir(parents=True)
        (root / "C001" / "P011" / "42_run.mpr").write_text("x")
        (root / "C001" / "P011" / "100-run.mpr").write_text("x")
        (root / "C002" / "P9999").mkdir(parents=True)
        (root / "C002" / "P9999" / "7_x.mpr").write_text("x")
        return root

    def test_three_groups_captured(self, tree: Path) -> None:
        from datalab_beholder.scanner import scan_directory

        result = scan_directory(
            tree,
            include_patterns=["*.mpr"],
            id_patterns=[
                r"^(?P<collection_id>C\d+)/(?P<group_id>P\d+)/(?P<item_id>\d+)[-_]"
            ],
        )
        by_path = {e.path: e.ids for e in result.entries}
        assert by_path["C001/P011/42_run.mpr"] == {
            "collection_id": "C001",
            "group_id": "P011",
            "item_id": "42",
        }
        assert by_path["C002/P9999/7_x.mpr"] == {
            "collection_id": "C002",
            "group_id": "P9999",
            "item_id": "7",
        }

    def test_first_matching_pattern_wins(self, tree: Path) -> None:
        from datalab_beholder.scanner import scan_directory

        result = scan_directory(
            tree,
            include_patterns=["*.mpr"],
            id_patterns=[
                r"^(?P<group_id>P\d+)/(?P<item_id>\d+)",  # never matches (no leading P)
                r"^(?P<collection_id>C\d+)/(?P<group_id>P\d+)/(?P<item_id>\d+)",
            ],
        )
        ids = next(e.ids for e in result.entries if e.path.endswith("42_run.mpr"))
        assert "collection_id" in ids

    def test_alternation_with_optional_group(self, tmp_path: Path) -> None:
        """A group that doesn't participate in the match is omitted from ids."""
        from datalab_beholder.scanner import scan_directory

        root = tmp_path / "alt"
        root.mkdir()
        (root / "100-run.mpr").write_text("x")

        result = scan_directory(
            root,
            include_patterns=["*.mpr"],
            id_patterns=[r"^(?:(?P<group_id>P\d+)/)?(?P<item_id>\d+)[-_]"],
        )
        ids = result.entries[0].ids
        assert ids == {"item_id": "100"}
