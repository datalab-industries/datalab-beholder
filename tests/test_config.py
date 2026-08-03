"""Tests for config validation."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from datalab_beholder.config import (
    ALLOWED_ID_GROUPS,
    LATEST_CONFIG_VERSION,
    BeholderConfig,
    CloudWatchedPath,
    LocalWatchedPath,
    SSHWatchedPath,
    _migrate_raw_config,
    load_config,
)


def _make(id_patterns: list[str], tmp_path: Path) -> LocalWatchedPath:
    return LocalWatchedPath(
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


def _wp(name: str, path: Path, datalab: str | None = None) -> dict:
    out = {"path": str(path), "name": name}
    if datalab is not None:
        out["datalab"] = datalab
    return out


def _dl(name: str, key: str = "k") -> dict:
    return {"name": name, "url": f"https://{name}.example.org", "api_key": key}


class TestItemIdRequirement:
    def test_pattern_without_item_id_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError) as exc:
            _make([r"(?P<group_id>P\d+)"], tmp_path)
        assert "item_id" in str(exc.value)

    def test_item_id_with_optional_others_accepted(self, tmp_path: Path) -> None:
        wp = _make(
            [
                r"(?P<group_id>P\d+)/(?P<item_id>\d+)",
                r"(?P<collection_id>C\d+)/(?P<item_id>\d+)",
            ],
            tmp_path,
        )
        assert len(wp.id_patterns) == 2

    def test_one_pattern_missing_item_id_rejects_whole_list(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(ValidationError):
            _make(
                [
                    r"(?P<item_id>\d+)\.mpr",
                    r"(?P<group_id>P\d+)\.dat",
                ],
                tmp_path,
            )


class TestDatalabRefValidation:
    def test_unknown_datalab_ref_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError) as exc:
            BeholderConfig(
                datalabs=[_dl("real")],
                watched_paths=[_wp("wp1", tmp_path, datalab="ghost")],
                state_db=tmp_path / "s.db",
            )
        msg = str(exc.value)
        assert "wp1" in msg
        assert "ghost" in msg
        assert "real" in msg

    def test_single_datalab_assigned_when_omitted(self, tmp_path: Path) -> None:
        cfg = BeholderConfig(
            datalabs=[_dl("only")],
            watched_paths=[_wp("wp1", tmp_path)],
            state_db=tmp_path / "s.db",
        )
        assert cfg.watched_paths[0].datalab == "only"

    def test_multi_datalab_requires_explicit_choice(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError) as exc:
            BeholderConfig(
                datalabs=[_dl("a"), _dl("b")],
                watched_paths=[_wp("wp1", tmp_path)],
                state_db=tmp_path / "s.db",
            )
        assert "wp1" in str(exc.value)

    def test_duplicate_datalab_names_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError) as exc:
            BeholderConfig(
                datalabs=[_dl("a"), _dl("a")],
                watched_paths=[_wp("wp1", tmp_path, datalab="a")],
                state_db=tmp_path / "s.db",
            )
        assert (
            "unique" in str(exc.value).lower() or "duplicate" in str(exc.value).lower()
        )

    def test_empty_datalabs_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError):
            BeholderConfig(
                datalabs=[],
                watched_paths=[_wp("wp1", tmp_path)],
                state_db=tmp_path / "s.db",
            )

    def test_each_wp_routes_to_its_named_datalab(self, tmp_path: Path) -> None:
        cfg = BeholderConfig(
            datalabs=[_dl("a"), _dl("b")],
            watched_paths=[
                _wp("wp1", tmp_path, datalab="a"),
                _wp("wp2", tmp_path, datalab="b"),
            ],
            state_db=tmp_path / "s.db",
        )
        assert cfg.watched_paths[0].datalab == "a"
        assert cfg.watched_paths[1].datalab == "b"


class TestDiscriminatedUnion:
    """`WatchedPath` is a discriminated union over Local/SSH/Cloud subclasses."""

    def test_missing_kind_defaults_to_local(self, tmp_path: Path) -> None:
        cfg = BeholderConfig(
            datalabs=[_dl("d")],
            watched_paths=[{"path": str(tmp_path), "name": "wp1", "datalab": "d"}],
            state_db=tmp_path / "s.db",
        )
        assert isinstance(cfg.watched_paths[0], LocalWatchedPath)
        assert cfg.watched_paths[0].kind == "local"

    def test_explicit_local_kind(self, tmp_path: Path) -> None:
        cfg = BeholderConfig(
            datalabs=[_dl("d")],
            watched_paths=[
                {
                    "kind": "local",
                    "path": str(tmp_path),
                    "name": "wp1",
                    "datalab": "d",
                }
            ],
            state_db=tmp_path / "s.db",
        )
        assert isinstance(cfg.watched_paths[0], LocalWatchedPath)

    def test_ssh_kind_round_trips(self, tmp_path: Path) -> None:
        cfg = BeholderConfig(
            datalabs=[_dl("d")],
            watched_paths=[
                {
                    "kind": "ssh",
                    "host": "bob@archive.example.org",
                    "path": "/data/runs",
                    "name": "wp_ssh",
                    "datalab": "d",
                }
            ],
            state_db=tmp_path / "s.db",
        )
        assert isinstance(cfg.watched_paths[0], SSHWatchedPath)
        assert cfg.watched_paths[0].host == "bob@archive.example.org"
        assert str(cfg.watched_paths[0].path) == "/data/runs"

    def test_cloud_kind_round_trips(self, tmp_path: Path) -> None:
        cfg = BeholderConfig(
            datalabs=[_dl("d")],
            watched_paths=[
                {
                    "kind": "cloud",
                    "path": str(tmp_path),
                    "name": "wp_cloud",
                    "datalab": "d",
                    "provider": "onedrive",
                }
            ],
            state_db=tmp_path / "s.db",
        )
        assert isinstance(cfg.watched_paths[0], CloudWatchedPath)
        assert cfg.watched_paths[0].provider == "onedrive"

    def test_ssh_scan_methods_raise_not_implemented(self, tmp_path: Path) -> None:
        wp = SSHWatchedPath(host="x@y", path="/data", name="wp", datalab="d")
        for method in (wp.hot_scan, wp.warm_scan, wp.cold_scan):
            with pytest.raises(NotImplementedError):
                method(state=None)  # type: ignore[arg-type]

    def test_cloud_scan_methods_raise_not_implemented(self, tmp_path: Path) -> None:
        wp = CloudWatchedPath(path=tmp_path, name="wp", datalab="d")
        for method in (wp.hot_scan, wp.warm_scan, wp.cold_scan):
            with pytest.raises(NotImplementedError):
                method(state=None)  # type: ignore[arg-type]


class TestBlockPatterns:
    def test_defaults_to_empty(self, tmp_path: Path) -> None:
        wp = LocalWatchedPath(path=tmp_path, name="wp")
        assert wp.block_patterns == {}

    def test_accepts_pattern_to_block_type_map(self, tmp_path: Path) -> None:
        wp = LocalWatchedPath(
            path=tmp_path,
            name="wp",
            block_patterns={"*.mpr": "cycle", "*.nda": "cycle"},
        )
        assert wp.block_patterns == {"*.mpr": "cycle", "*.nda": "cycle"}

    def test_empty_pattern_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError, match="non-empty glob patterns"):
            LocalWatchedPath(
                path=tmp_path,
                name="wp",
                block_patterns={"": "cycle"},
            )

    def test_empty_block_type_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError, match="non-empty block type"):
            LocalWatchedPath(
                path=tmp_path,
                name="wp",
                block_patterns={"*.mpr": ""},
            )


class TestScanCadence:
    def test_defaults_present(self, tmp_path: Path) -> None:
        wp = LocalWatchedPath(path=tmp_path, name="wp")
        assert wp.scan.hot_interval == 60
        assert wp.scan.warm_interval == 3600
        assert wp.scan.cold_interval == 86400
        assert wp.scan.hot_window == 86400

    def test_cold_interval_can_be_disabled(self, tmp_path: Path) -> None:
        wp = LocalWatchedPath(
            path=tmp_path,
            name="wp",
            scan={"cold_interval": None},  # type: ignore[arg-type]
        )
        assert wp.scan.cold_interval is None

    def test_per_path_overrides(self, tmp_path: Path) -> None:
        wp = LocalWatchedPath(
            path=tmp_path,
            name="wp",
            scan={"hot_interval": 5, "warm_interval": 30, "hot_window": 600},  # type: ignore[arg-type]
        )
        assert wp.scan.hot_interval == 5
        assert wp.scan.warm_interval == 30
        assert wp.scan.hot_window == 600


class TestConfigVersioning:
    def test_default_version_is_latest(self, tmp_path: Path) -> None:
        cfg = BeholderConfig(
            datalabs=[_dl("a")],
            watched_paths=[_wp("wp1", tmp_path, datalab="a")],
            state_db=tmp_path / "s.db",
        )
        assert cfg.version == LATEST_CONFIG_VERSION

    def test_missing_version_treated_as_v1(self) -> None:
        raw: dict = {"datalabs": [], "watched_paths": []}
        out = _migrate_raw_config(raw, source="<test>")
        # No migrators registered yet (v1 == latest), so dict passes through.
        assert out is raw

    def test_explicit_current_version_passes_through(self) -> None:
        raw = {"version": 1, "datalabs": [], "watched_paths": []}
        out = _migrate_raw_config(raw, source="<test>")
        assert out["version"] == 1

    def test_future_version_rejected(self) -> None:
        raw = {"version": LATEST_CONFIG_VERSION + 1}
        with pytest.raises(ValueError, match="Upgrade the daemon"):
            _migrate_raw_config(raw, source="<test>")

    def test_non_integer_version_rejected(self) -> None:
        raw = {"version": "1"}
        with pytest.raises(ValueError, match="must be an integer"):
            _migrate_raw_config(raw, source="<test>")

    def test_load_config_round_trips_version_field(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(
            f"version: {LATEST_CONFIG_VERSION}\n"
            "datalabs:\n"
            "  - name: a\n"
            "    url: https://a.example.org\n"
            "    api_key: k\n"
            "watched_paths:\n"
            f"  - path: {tmp_path}\n"
            "    name: wp1\n"
            "    datalab: a\n"
            f"state_db: {tmp_path / 's.db'}\n"
        )
        cfg = load_config(cfg_path)
        assert cfg.version == LATEST_CONFIG_VERSION

    def test_load_config_without_version_field(self, tmp_path: Path) -> None:
        """Existing v1 configs in the wild have no `version` field."""
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(
            "datalabs:\n"
            "  - name: a\n"
            "    url: https://a.example.org\n"
            "    api_key: k\n"
            "watched_paths:\n"
            f"  - path: {tmp_path}\n"
            "    name: wp1\n"
            "    datalab: a\n"
            f"state_db: {tmp_path / 's.db'}\n"
        )
        cfg = load_config(cfg_path)
        assert cfg.version == LATEST_CONFIG_VERSION
