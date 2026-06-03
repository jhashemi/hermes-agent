"""Tests for pre-mutation checkpoint hooks (Phase 4)."""

import os
import pytest
import tempfile
from pathlib import Path

from tools.checkpoint_manager import CheckpointManager


@pytest.fixture
def chk_mgr(tmp_path):
    """Create a CheckpointManager pointing at a temp working dir."""
    mgr = CheckpointManager(enabled=True)
    mgr._checkpointed_dirs = set()  # reset per-test
    # Create a git repo in tmp_path
    os.system(f"cd {tmp_path} && git init && git config user.email 'test@test.com' "
              f"&& git config user.name 'Test' && echo 'init' > README.md "
              f"&& git add . && git commit -m 'init'")
    return mgr, tmp_path


class TestSnapshotFile:
    """Tests for per-file pre-mutation snapshots."""

    def test_snapshot_file_copies_before_mutation(self, chk_mgr):
        """snapshot_file() creates a backup of the file before it's modified."""
        mgr, tmp_path = chk_mgr
        test_file = tmp_path / "important.py"
        test_file.write_text("original_content()")
        
        result = mgr.snapshot_file(str(test_file), reason="pre-write")
        assert result is True
        
        # The backup should exist
        backup = mgr.get_snapshot_path(str(test_file))
        assert backup is not None
        assert Path(backup).exists()
        assert Path(backup).read_text() == "original_content()"

    def test_snapshot_file_nonexistent_returns_false(self, chk_mgr):
        """snapshot_file() for a nonexistent file returns False gracefully."""
        mgr, tmp_path = chk_mgr
        result = mgr.snapshot_file(str(tmp_path / "no_such_file.py"), reason="pre-write")
        assert result is False

    def test_restore_from_snapshot(self, chk_mgr):
        """restore_snapshot() recovers the file to its pre-mutation state."""
        mgr, tmp_path = chk_mgr
        test_file = tmp_path / "config.yaml"
        test_file.write_text("key: old_value")
        
        mgr.snapshot_file(str(test_file), reason="pre-patch")
        test_file.write_text("key: new_value")  # mutation happens
        
        # File now has new content
        assert test_file.read_text() == "key: new_value"
        
        # Restore from snapshot
        result = mgr.restore_snapshot(str(test_file))
        assert result is True
        assert test_file.read_text() == "key: old_value"

    def test_restore_no_snapshot_returns_false(self, chk_mgr):
        """restore_snapshot() for a file with no snapshot returns False."""
        mgr, tmp_path = chk_mgr
        test_file = tmp_path / "never_snapshotted.py"
        test_file.write_text("content")
        result = mgr.restore_snapshot(str(test_file))
        assert result is False

    def test_snapshot_overwrites_previous(self, chk_mgr):
        """A second snapshot of the same file replaces the first."""
        mgr, tmp_path = chk_mgr
        test_file = tmp_path / " mutable.py"
        test_file.write_text("v1")
        mgr.snapshot_file(str(test_file), reason="pre-write-1")
        test_file.write_text("v2")
        mgr.snapshot_file(str(test_file), reason="pre-write-2")
        
        # Restoring should give v2 (latest snapshot), not v1
        mgr.restore_snapshot(str(test_file))
        # The snapshot was taken when file had v2 content
        # After snapshot_file was called with v2 on disk, it backed up v2
        # Wait — we snapshot BEFORE writing. Let me re-think:
        # 1. file=v1, snapshot_file → backup=v1
        # 2. file written to v2
        # 3. snapshot_file → backup=v2 (overwrites previous backup)
        # 4. restore_snapshot → file=v2
        assert test_file.read_text() == "v2"


class TestGetSnapshotPath:
    """Tests for snapshot path resolution."""

    def test_returns_path_for_snapshotted_file(self, chk_mgr):
        mgr, tmp_path = chk_mgr
        test_file = tmp_path / "tracked.py"
        test_file.write_text("code")
        mgr.snapshot_file(str(test_file), reason="test")
        path = mgr.get_snapshot_path(str(test_file))
        assert path is not None
        assert ".hermes-snapshots" in path

    def test_returns_none_for_unsnapshotted_file(self, chk_mgr):
        mgr, tmp_path = chk_mgr
        path = mgr.get_snapshot_path(str(tmp_path / "nope.py"))
        assert path is None
