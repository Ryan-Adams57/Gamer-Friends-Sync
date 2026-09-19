"""
Unit and smoke tests for gamer_friends_sync.

These tests do not touch the network or require psnawp / xbox-webapi: the
platform libraries are imported lazily and are never reached by --self-test or
the pure helper functions exercised here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Make the script importable when pytest is run from anywhere in the repo.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gamer_friends_sync as gfs  # noqa: E402


def test_compute_delta_first_run_reports_nothing():
    # An empty "previous" map is a first run; it must not report every current
    # friend as newly added.
    added, removed = gfs.compute_delta({}, {"1": "Alice", "2": "Bob"})
    assert added == []
    assert removed == []


def test_compute_delta_detects_add_and_remove():
    previous = {"1": "Alice", "2": "Bob"}
    current = {"1": "Alice", "3": "Carol"}
    added, removed = gfs.compute_delta(previous, current)
    assert added == [{"id": "3", "name": "Carol"}]
    assert removed == [{"id": "2", "name": "Bob"}]


def test_clip_to_field_stays_under_discord_limit():
    lines = [f"+ `Friend{i:03d}`" for i in range(500)]
    value = gfs._clip_to_field(lines)
    assert len(value) <= gfs.DISCORD_FIELD_LIMIT
    assert value.splitlines()[-1].startswith("+")  # truthful "+N more" tail


def test_build_discord_payload_none_when_no_changes():
    assert gfs.build_discord_payload([], self_test=False) is None


def test_build_discord_payload_groups_by_platform():
    changes = [
        {"platform": "playstation", "event": "added", "id": "1", "name": "Alice"},
        {"platform": "xbox", "event": "removed", "id": "2", "name": "Bob"},
    ]
    payload = gfs.build_discord_payload(changes, self_test=False)
    assert payload is not None
    titles = [e["title"] for e in payload["embeds"]]
    assert any("PlayStation" in t for t in titles)
    assert any("Xbox" in t for t in titles)


def test_self_test_end_to_end(tmp_path: Path):
    # The whole pipeline should run offline and produce CSVs, a snapshot, and
    # the HTML viewer, exiting 0.
    out = tmp_path / "out"
    rc = gfs.main(["--self-test", "--export-path", str(out)])
    assert rc == 0

    psn_csv = list(out.glob("psn_friends_*.csv"))
    xbox_csv = list(out.glob("xbox_friends_*.csv"))
    assert psn_csv, "expected a PlayStation CSV"
    assert xbox_csv, "expected an Xbox CSV"
    assert (out / "gamer_friends.html").exists()

    snapshot = json.loads((out / "snapshot.json").read_text(encoding="utf-8"))
    assert "playstation" in snapshot and "xbox" in snapshot
