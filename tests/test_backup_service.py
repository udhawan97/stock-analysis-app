# pylint: disable=protected-access,redefined-outer-name,unused-argument,unnecessary-lambda
"""Backup, verification, and restore of the SQLite portfolio database.

These tests use real on-disk SQLite files in a temp directory to exercise the
online backup API, integrity checking, the non-destructive restore path (current
file preserved as ``*.failed-*``), and retention pruning.
"""
import os
import sqlite3
import stat
from pathlib import Path

import pytest

from app.services import backup_service


@pytest.fixture(autouse=True)
def close_test_profile_locks():
    before = set(backup_service._profile_lock_handles)
    yield
    for path in set(backup_service._profile_lock_handles) - before:
        backup_service._profile_lock_handles.pop(path).close()


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _assert_owner_only(path: Path) -> None:
    """Check the POSIX privacy contract where permission bits are meaningful."""
    if os.name != "nt":
        assert _mode(path) == 0o600


def _make_db(path, rows):
    """Create a minimal holdings DB with ``rows`` holding records."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE holdings (id INTEGER PRIMARY KEY, ticker TEXT)")
        conn.executemany(
            "INSERT INTO holdings (ticker) VALUES (?)", [(t,) for t in rows]
        )
        conn.commit()
    finally:
        conn.close()


def _holdings(path):
    conn = sqlite3.connect(str(path))
    try:
        return [r[0] for r in conn.execute("SELECT ticker FROM holdings ORDER BY id")]
    finally:
        conn.close()


def test_create_and_verify_preserves_rows(tmp_path):
    src = tmp_path / "portfolio.db"
    _make_db(src, ["VOO", "AAPL", "MSFT"])

    backup = backup_service.create_backup(src, label="test", dest_dir=tmp_path / "backups")

    assert backup.exists()
    assert backup_service.verify_backup(backup, expected_min_holdings=3)
    assert _holdings(backup) == ["VOO", "AAPL", "MSFT"]
    assert sorted(path.name for path in backup.parent.iterdir()) == [backup.name]


def test_staging_durability_flush_uses_windows_safe_descriptor(tmp_path, monkeypatch):
    src = tmp_path / "portfolio.db"
    _make_db(src, ["VOO"])
    observed_modes = []
    real_open = open

    def tracking_open(path, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if str(path).endswith(".staging"):
            observed_modes.append(mode)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(backup_service, "open", tracking_open, raising=False)

    backup_service.create_backup(src, label="test", dest_dir=tmp_path / "backups")

    assert "r+b" in observed_modes


def test_expected_holding_count_is_verified_before_publication(tmp_path, monkeypatch):
    src = tmp_path / "portfolio.db"
    _make_db(src, ["VOO", "AAPL"])
    vault = tmp_path / "backups"
    observed = []

    def reject_staging(_path, expected_min_holdings=None):
        observed.append(expected_min_holdings)
        return False

    monkeypatch.setattr(backup_service, "verify_backup", reject_staging)

    with pytest.raises(ValueError, match="integrity verification"):
        backup_service.create_backup(
            src,
            label="manual",
            dest_dir=vault,
            expected_min_holdings=2,
        )

    assert observed == [2]
    assert not list(vault.glob("*.db"))
    assert not list(vault.glob("*.staging"))


def test_no_replace_collision_preserves_unowned_destination(tmp_path):
    src = tmp_path / "portfolio.db"
    _make_db(src, ["VOO"])
    vault = tmp_path / "backups"
    vault.mkdir()
    existing = vault / "manual-fixed.db"
    existing.write_bytes(b"belongs-to-another-process")

    with pytest.raises(FileExistsError):
        backup_service.create_backup(src, "manual", vault, ts="fixed")

    assert existing.read_bytes() == b"belongs-to-another-process"
    assert not list(vault.glob("*.staging"))


def test_listing_absent_vault_does_not_create_it(tmp_path, monkeypatch):
    from app import paths

    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    vault = tmp_path / "backups"

    assert backup_service.list_backups() == []
    assert not vault.exists()


def test_listing_and_verification_do_not_create_sqlite_sidecars(tmp_path, monkeypatch):
    from app import paths

    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    vault = tmp_path / "backups"
    vault.mkdir()
    backup = vault / "manual-closed.db"
    _make_db(backup, ["VOO", "AAPL"])
    before = {path.name: path.stat().st_size for path in vault.iterdir()}

    assert backup_service.verify_vault_backup(backup) is True
    assert backup_service.list_backups()[0]["holding_count"] == 2

    after = {path.name: path.stat().st_size for path in vault.iterdir()}
    assert after == before
    assert not Path(f"{backup}-wal").exists()
    assert not Path(f"{backup}-shm").exists()


def test_vault_verification_refuses_nonempty_wal_sidecar(tmp_path):
    backup = tmp_path / "manual-incomplete.db"
    _make_db(backup, ["VOO"])
    Path(f"{backup}-wal").write_bytes(b"uncommitted pages")

    assert backup_service.verify_backup(backup) is False
    assert backup_service.verify_vault_backup(backup) is False


def test_verify_rejects_missing_and_empty(tmp_path):
    assert backup_service.verify_backup(tmp_path / "nope.db") is False
    empty = tmp_path / "empty.db"
    empty.touch()
    assert backup_service.verify_backup(empty) is False


def test_verify_rejects_corrupt_file(tmp_path):
    junk = tmp_path / "corrupt.db"
    junk.write_bytes(b"this is definitely not a sqlite database")
    assert backup_service.verify_backup(junk) is False


def test_vault_verification_rejects_an_unrelated_healthy_database(tmp_path):
    unrelated = tmp_path / "unrelated.db"
    conn = sqlite3.connect(str(unrelated))
    conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
    conn.commit()
    conn.close()

    assert backup_service.verify_backup(unrelated) is True
    assert backup_service.verify_vault_backup(unrelated) is False


def test_verify_min_holdings_when_table_absent(tmp_path):
    empty_db = tmp_path / "fresh.db"
    conn = sqlite3.connect(str(empty_db))
    conn.execute("CREATE TABLE unrelated (id INTEGER)")
    conn.commit()
    conn.close()
    # No holdings table: valid only when zero holdings are expected.
    assert backup_service.verify_backup(empty_db, expected_min_holdings=0) is True
    assert backup_service.verify_backup(empty_db, expected_min_holdings=1) is False


def test_restore_preserves_current_and_restores_rows(tmp_path):
    live = tmp_path / "portfolio.db"
    _make_db(live, ["OLD1", "OLD2"])
    backup = backup_service.create_backup(live, label="snap", dest_dir=tmp_path / "backups")

    # Simulate the live DB drifting/corrupting after the backup.
    conn = sqlite3.connect(str(live))
    conn.execute("DELETE FROM holdings")
    conn.execute("INSERT INTO holdings (ticker) VALUES ('BROKEN')")
    conn.commit()
    conn.close()

    assert backup_service.restore_backup(backup, live, ts="20260101-000000") is True

    # Rows come back from the backup, and the pre-restore file is preserved.
    assert _holdings(live) == ["OLD1", "OLD2"]
    assert (tmp_path / "portfolio.db.failed-20260101-000000").exists()


def test_restore_refuses_unverified_backup(tmp_path):
    live = tmp_path / "portfolio.db"
    _make_db(live, ["KEEP"])
    junk = tmp_path / "corrupt.db"
    junk.write_bytes(b"not sqlite")
    with pytest.raises(ValueError):
        backup_service.restore_backup(junk, live)
    # The live DB is untouched when a restore is refused.
    assert _holdings(live) == ["KEEP"]


def test_restore_failure_mid_copy_leaves_live_db_intact(tmp_path, monkeypatch):
    """A copy failure during restore must never leave the live DB missing.

    The backup is staged and re-verified before the live file is moved aside, so
    a mid-restore failure (disk full, interrupted process) leaves the current
    database exactly where it was rather than deleted with no replacement ready.
    """
    live = tmp_path / "portfolio.db"
    _make_db(live, ["CURRENT1", "CURRENT2"])
    backup = backup_service.create_backup(live, label="snap", dest_dir=tmp_path / "backups")

    # Simulate the staging copy blowing up (e.g. disk fills) partway through.
    def _boom(src, dst):
        raise OSError("No space left on device")

    monkeypatch.setattr(backup_service.shutil, "copyfile", _boom)

    with pytest.raises(OSError):
        backup_service.restore_backup(backup, live, ts="20260101-000000")

    # Live DB is fully intact; nothing was moved aside because the copy failed
    # before the swap step ever ran.
    assert _holdings(live) == ["CURRENT1", "CURRENT2"]
    assert not (tmp_path / "portfolio.db.failed-20260101-000000").exists()


def _restore_fault_fixture(tmp_path):
    live = tmp_path / "portfolio.db"
    _make_db(live, ["BACKUP"])
    backup = backup_service.create_backup(
        live, label="snap", dest_dir=tmp_path / "backups"
    )
    connection = sqlite3.connect(str(live))
    connection.execute("DELETE FROM holdings")
    connection.execute("INSERT INTO holdings (ticker) VALUES ('CURRENT')")
    connection.commit()
    connection.close()
    Path(f"{live}-wal").write_bytes(b"current-wal")
    Path(f"{live}-shm").write_bytes(b"current-shm")
    originals = {
        suffix: Path(f"{live}{suffix}").read_bytes()
        for suffix in ("", "-wal", "-shm")
    }
    return live, backup, originals


@pytest.mark.parametrize("fault_suffix", ("", "-wal", "-shm", "publish"))
def test_every_forward_restore_rename_fault_restores_canonical_originals(
    tmp_path, monkeypatch, fault_suffix
):
    live, backup, originals = _restore_fault_fixture(tmp_path)
    stamp = "20260101-000000"
    staging = tmp_path / f"portfolio.db.staging-{stamp}"
    real_replace = Path.replace

    def faulted_replace(source, destination):
        destination = Path(destination)
        if fault_suffix == "publish":
            should_fault = source == staging and destination == live
        else:
            current = Path(f"{live}{fault_suffix}")
            should_fault = source == current and destination == Path(
                f"{current}.failed-{stamp}"
            )
        if should_fault:
            raise OSError("simulated forward rename failure")
        return real_replace(source, destination)

    monkeypatch.setattr(Path, "replace", faulted_replace)

    with pytest.raises(OSError, match="forward rename failure"):
        backup_service.restore_backup(backup, live, ts=stamp)

    for suffix, expected in originals.items():
        assert Path(f"{live}{suffix}").read_bytes() == expected
    assert _holdings(live) == ["CURRENT"]
    assert staging.read_bytes() == backup.read_bytes()


@pytest.mark.parametrize("rollback_suffix", ("", "-wal", "-shm"))
def test_every_rollback_rename_fault_falls_back_to_preserving_copy(
    tmp_path, monkeypatch, rollback_suffix
):
    live, backup, originals = _restore_fault_fixture(tmp_path)
    stamp = "20260101-000000"
    staging = tmp_path / f"portfolio.db.staging-{stamp}"
    real_replace = Path.replace

    def faulted_replace(source, destination):
        destination = Path(destination)
        failed = Path(f"{live}{rollback_suffix}.failed-{stamp}")
        current = Path(f"{live}{rollback_suffix}")
        if (source == staging and destination == live) or (
            source == failed and destination == current
        ):
            raise OSError("simulated rename failure")
        return real_replace(source, destination)

    monkeypatch.setattr(Path, "replace", faulted_replace)

    with pytest.raises(OSError, match="rename failure"):
        backup_service.restore_backup(backup, live, ts=stamp)

    for suffix, expected in originals.items():
        assert Path(f"{live}{suffix}").read_bytes() == expected
    assert _holdings(live) == ["CURRENT"]
    assert staging.read_bytes() == backup.read_bytes()
    failed = Path(f"{live}{rollback_suffix}.failed-{stamp}")
    assert failed.read_bytes() == originals[rollback_suffix]


def test_count_holdings(tmp_path):
    db = tmp_path / "portfolio.db"
    _make_db(db, ["VOO", "AAPL"])
    assert backup_service.count_holdings(db) == 2


def test_count_holdings_missing_table_or_file_is_zero(tmp_path):
    assert backup_service.count_holdings(tmp_path / "nope.db") == 0
    empty = tmp_path / "notable.db"
    conn = sqlite3.connect(str(empty))
    conn.execute("CREATE TABLE unrelated (id INTEGER)")
    conn.commit()
    conn.close()
    assert backup_service.count_holdings(empty) == 0


def test_env_snapshot_and_restore(tmp_path, monkeypatch):
    from app import paths

    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=sk-original\n", encoding="utf-8")

    snap = backup_service.snapshot_env(tmp_path / "backup.env")
    assert snap is not None
    assert snap.read_text(encoding="utf-8").startswith("ANTHROPIC_API_KEY=sk-original")

    # Current .env drifts, then is restored from the snapshot.
    env.write_text("ANTHROPIC_API_KEY=sk-changed\n", encoding="utf-8")
    assert backup_service.restore_env(snap, ts="20260101-000000") is True
    assert "sk-original" in env.read_text(encoding="utf-8")
    assert (tmp_path / ".env.failed-20260101-000000").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode contract")
def test_env_snapshot_and_restore_remain_owner_only_under_common_umask(
    tmp_path, monkeypatch
):
    from app import paths

    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=sk-original\n", encoding="utf-8")
    env.chmod(0o600)
    previous_umask = os.umask(0o022)
    try:
        snap = backup_service.snapshot_env(tmp_path / "backup.env")
        _assert_owner_only(snap)
        env.write_text("ANTHROPIC_API_KEY=sk-changed\n", encoding="utf-8")
        backup_service.restore_env(snap, ts="20260101-000000")
    finally:
        os.umask(previous_umask)

    _assert_owner_only(env)
    _assert_owner_only(tmp_path / ".env.failed-20260101-000000")


@pytest.mark.parametrize("source_kind", ["symlink", "directory"])
def test_env_snapshot_refuses_nonregular_source(
    tmp_path, monkeypatch, source_kind
):
    from app import paths

    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    source = tmp_path / ".env"
    if source_kind == "symlink":
        referent = tmp_path / "referent.env"
        referent.write_text("ANTHROPIC_API_KEY=keep\n", encoding="utf-8")
        source.symlink_to(referent)
    else:
        source.mkdir()

    with pytest.raises(backup_service.env_file.EnvFileSecurityError):
        backup_service.snapshot_env(tmp_path / "backup.env")


@pytest.mark.parametrize("source_kind", ["symlink", "directory"])
def test_env_restore_refuses_nonregular_backup_without_changing_canonical(
    tmp_path, monkeypatch, source_kind
):
    from app import paths

    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    current = tmp_path / ".env"
    current.write_text("ANTHROPIC_API_KEY=keep\n", encoding="utf-8")
    current.chmod(0o600)
    backup = tmp_path / "backup.env"
    if source_kind == "symlink":
        referent = tmp_path / "referent.env"
        referent.write_text("ANTHROPIC_API_KEY=replace\n", encoding="utf-8")
        backup.symlink_to(referent)
    else:
        backup.mkdir()

    with pytest.raises(backup_service.env_file.EnvFileSecurityError):
        backup_service.restore_env(backup, ts="20260101-000000")

    assert current.read_text(encoding="utf-8") == "ANTHROPIC_API_KEY=keep\n"
    _assert_owner_only(current)


@pytest.mark.parametrize("target_kind", ["symlink", "directory"])
def test_env_restore_refuses_nonregular_canonical_target(
    tmp_path, monkeypatch, target_kind
):
    from app import paths

    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    current = tmp_path / ".env"
    if target_kind == "symlink":
        referent = tmp_path / "referent.env"
        referent.write_text("ANTHROPIC_API_KEY=keep\n", encoding="utf-8")
        current.symlink_to(referent)
    else:
        current.mkdir()
    backup = tmp_path / "backup.env"
    backup.write_text("ANTHROPIC_API_KEY=replace\n", encoding="utf-8")
    backup.chmod(0o600)

    with pytest.raises(backup_service.env_file.EnvFileSecurityError):
        backup_service.restore_env(backup, ts="20260101-000000")

    if target_kind == "symlink":
        assert current.is_symlink()
        assert referent.read_text(encoding="utf-8") == "ANTHROPIC_API_KEY=keep\n"
    else:
        assert current.is_dir()
    assert not list(tmp_path.glob("..env.restore-20260101-000000-*"))


def test_env_restore_copy_failure_preserves_canonical(tmp_path, monkeypatch):
    from app import paths

    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    current = tmp_path / ".env"
    current.write_text("ANTHROPIC_API_KEY=keep\n", encoding="utf-8")
    current.chmod(0o600)
    backup = tmp_path / "backup.env"
    backup.write_text("ANTHROPIC_API_KEY=replace\n", encoding="utf-8")
    backup.chmod(0o600)
    real_read = backup_service.env_file._read_bytes

    def fail_backup_read(path):
        if Path(path) == backup:
            raise OSError("simulated copy failure")
        return real_read(path)

    monkeypatch.setattr(backup_service.env_file, "_read_bytes", fail_backup_read)

    with pytest.raises(OSError, match="copy failure"):
        backup_service.restore_env(backup, ts="20260101-000000")

    assert current.read_text(encoding="utf-8") == "ANTHROPIC_API_KEY=keep\n"
    _assert_owner_only(current)


def test_env_restore_replace_failure_preserves_canonical(tmp_path, monkeypatch):
    from app import paths

    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    current = tmp_path / ".env"
    current.write_text("ANTHROPIC_API_KEY=keep\n", encoding="utf-8")
    current.chmod(0o600)
    backup = tmp_path / "backup.env"
    backup.write_text("ANTHROPIC_API_KEY=replace\n", encoding="utf-8")
    backup.chmod(0o600)
    real_replace = backup_service.env_file.os.replace

    def fail_canonical_replace(source, target):
        if Path(target) == current:
            raise OSError("simulated publication failure")
        return real_replace(source, target)

    monkeypatch.setattr(
        backup_service.env_file.os,
        "replace",
        fail_canonical_replace,
    )

    with pytest.raises(OSError, match="publication failure"):
        backup_service.restore_env(backup, ts="20260101-000000")

    assert current.read_text(encoding="utf-8") == "ANTHROPIC_API_KEY=keep\n"
    _assert_owner_only(current)
    assert not list(tmp_path.glob("..env-*.tmp"))


def test_snapshot_env_returns_none_when_absent(tmp_path, monkeypatch):
    from app import paths

    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    assert backup_service.snapshot_env(tmp_path / "backup.env") is None


def test_prune_keeps_newest_n(tmp_path):
    backups = tmp_path / "backups"
    backups.mkdir()
    import time

    created = []
    for i in range(7):
        src = tmp_path / f"src{i}.db"
        _make_db(src, [f"T{i}"])
        path = backup_service.create_backup(src, label=f"b{i}", dest_dir=backups, ts=f"t{i}")
        # Space out mtimes so ordering is deterministic.
        os.utime(path, (time.time() + i, time.time() + i))
        created.append(path)

    removed = backup_service.prune_backups(backups, keep=3)
    remaining = sorted(backups.glob("*.db"))
    assert len(remaining) == 3
    assert len(removed) == 4
    # The three newest (highest index) survive.
    assert {p.name for p in remaining} == {created[i].name for i in (4, 5, 6)}


def test_manual_pruning_does_not_remove_other_rollback_points(tmp_path):
    backups = tmp_path / "backups"
    backups.mkdir()
    protected = backups / "pre-update-5.9.0.db"
    protected.touch()
    for i in range(3):
        (backups / f"manual-{i}.db").touch()

    removed = backup_service.prune_backups(
        backups, keep=1, pattern="manual-*.db"
    )

    assert protected.exists()
    assert len(removed) == 2
    assert len(list(backups.glob("manual-*.db"))) == 1


def test_resolve_backup_name_rejects_traversal(tmp_path, monkeypatch):
    from app import paths

    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    with pytest.raises(ValueError):
        backup_service.resolve_backup_name("../portfolio.db")
    with pytest.raises(ValueError):
        backup_service.resolve_backup_name("notes.txt")


def test_queued_restore_applies_before_start_and_keeps_safety_copy(tmp_path, monkeypatch):
    from app import paths

    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    live = tmp_path / "portfolio.db"
    _make_db(live, ["CURRENT"])
    wanted = backup_service.create_backup(
        live, label="manual", dest_dir=tmp_path / "backups", ts="wanted"
    )

    conn = sqlite3.connect(str(live))
    conn.execute("DELETE FROM holdings")
    conn.execute("INSERT INTO holdings (ticker) VALUES ('CHANGED')")
    conn.commit()
    conn.close()

    monkeypatch.setattr(backup_service, "live_db_path", lambda: live)
    backup_service.queue_restore(wanted.name)
    result = backup_service.apply_pending_restore()

    assert result["status"] == "restored"
    assert _holdings(live) == ["CURRENT"]
    assert result["safety_backup"].startswith("pre-manual-restore-")
    assert backup_service.resolve_backup_name(result["safety_backup"]).exists()


def test_create_verified_backup_owns_count_create_and_verification(tmp_path):
    source = tmp_path / "portfolio.db"
    _make_db(source, ["AAPL", "MSFT"])

    point = backup_service.create_verified_backup(
        "pre-change", source_db=source, dest_dir=tmp_path / "backups"
    )

    assert point.database.exists()
    assert point.environment is None
    assert backup_service.verify_backup(point.database, expected_min_holdings=2)


def test_vault_verified_backup_rechecks_the_published_holdings_count(
    tmp_path, monkeypatch
):
    source = tmp_path / "portfolio.db"
    _make_db(source, ["AAPL", "MSFT"])
    checks = []
    verify_backup = backup_service.verify_backup

    def record_check(path, expected_min_holdings=None):
        checks.append((Path(path), expected_min_holdings))
        return verify_backup(path, expected_min_holdings)

    monkeypatch.setattr(backup_service, "verify_backup", record_check)

    point = backup_service.create_verified_backup(
        "manual",
        source_db=source,
        dest_dir=tmp_path / "backups",
        require_vault_schema=True,
    )

    assert (point.database, 2) in checks


def test_create_verified_backup_rejects_and_removes_a_corrupt_result(
    tmp_path, monkeypatch
):
    source = tmp_path / "portfolio.db"
    _make_db(source, ["AAPL"])
    corrupt = tmp_path / "backups" / "pre-change-corrupt.db"

    def publish_corrupt(*_args, **_kwargs):
        corrupt.parent.mkdir(parents=True, exist_ok=True)
        corrupt.write_text("not sqlite", encoding="utf-8")
        return corrupt

    monkeypatch.setattr(backup_service, "create_backup", publish_corrupt)

    with pytest.raises(ValueError, match="verification"):
        backup_service.create_verified_backup(
            "pre-change", source_db=source, dest_dir=corrupt.parent
        )

    assert not corrupt.exists()


def test_environment_snapshot_failure_keeps_the_verified_database_artifact(
    tmp_path, monkeypatch
):
    source = tmp_path / "portfolio.db"
    _make_db(source, ["AAPL"])

    def fail_environment(_destination):
        raise OSError("disk full")

    monkeypatch.setattr(backup_service, "snapshot_env", fail_environment)

    with pytest.raises(backup_service.EnvironmentSnapshotError) as caught:
        backup_service.create_verified_backup(
            "pre-update",
            source_db=source,
            dest_dir=tmp_path / "backups",
            include_environment=True,
        )

    assert caught.value.backup.database.exists()
    assert caught.value.backup.environment is None
    assert backup_service.verify_backup(
        caught.value.backup.database, expected_min_holdings=1
    )


@pytest.mark.parametrize("partial_copy", [False, True])
def test_incomplete_restore_blocks_second_start_even_with_settings_write_failure(
    tmp_path, monkeypatch, partial_copy
):
    from app import app_settings, paths

    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    live, backup, originals = _restore_fault_fixture(tmp_path)
    monkeypatch.setattr(backup_service, "live_db_path", lambda: live)
    monkeypatch.setattr(backup_service, "_timestamp", lambda: "blocked")
    # The synthetic sidecars are deliberately byte sentinels, not valid WAL.
    monkeypatch.setattr(backup_service, "create_verified_backup", lambda **k:
                        backup_service.VerifiedBackup(backup))
    backup_service.queue_restore(backup.name)
    real_replace = Path.replace

    def fault_replace(source, destination):
        if source.name.endswith(("staging-blocked", "failed-blocked")):
            raise OSError("rename blocked")
        return real_replace(source, destination)

    def fault_copy(source, destination):
        if partial_copy:
            Path(destination).write_bytes(b"partial")
        raise OSError("recovery copy failed")

    monkeypatch.setattr(Path, "replace", fault_replace)
    monkeypatch.setattr(backup_service.shutil, "copy2", fault_copy)
    monkeypatch.setattr(app_settings, "save_settings", lambda values:
                        (_ for _ in ()).throw(OSError("settings unavailable")))
    with pytest.raises(backup_service.RestoreRecoveryError):
        backup_service.apply_pending_restore()
    assert Path(f"{live}.restore-in-progress").exists()
    assert Path(f"{live}.staging-blocked").read_bytes() == backup.read_bytes()
    for suffix, expected in originals.items():
        assert Path(f"{live}{suffix}.failed-blocked").read_bytes() == expected
    # A new launch must stop before reading settings or opening any DB.
    monkeypatch.setattr(app_settings, "load_settings", lambda:
                        (_ for _ in ()).throw(AssertionError("settings read")))
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k:
                        (_ for _ in ()).throw(AssertionError("database opened")))
    with pytest.raises(backup_service.RestoreRecoveryError, match="recovery is incomplete"):
        backup_service.apply_pending_restore()


def test_profile_lock_contends_across_processes_and_releases_on_exit(tmp_path, monkeypatch):
    import subprocess
    import sys
    from app import paths

    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    script = (
        "import sys; from pathlib import Path; from app import paths; "
        "from app.services import backup_service; "
        "paths.data_dir=lambda: Path(sys.argv[1]); "
        "backup_service.acquire_profile_lock()"
    )

    def child():
        return subprocess.run(
            [sys.executable, "-c", script, str(tmp_path)], check=False,
            capture_output=True, text=True,
        )

    assert child().returncode == 0
    # The first child exited: the next process can acquire its released lock.
    backup_service.acquire_profile_lock()
    locked = child()
    assert locked.returncode != 0
    assert "ProfileInUseError" in locked.stderr
    backup_service._profile_lock_handles.pop(tmp_path / ".runtime.lock").close()
    assert child().returncode == 0


@pytest.mark.parametrize("launcher", ["desktop", "source"])
def test_launchers_stop_on_persistent_barrier_before_database_start(
    tmp_path, monkeypatch, launcher
):
    import builtins
    import runpy
    import sys
    import desktop.main as desktop_main
    from app import paths

    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(paths, "prepare_runtime_profile", lambda: None)
    live = tmp_path / "portfolio.db"
    monkeypatch.setattr(backup_service, "live_db_path", lambda: live)
    Path(f"{live}.restore-in-progress").write_text("recovery evidence", encoding="utf-8")
    monkeypatch.setattr(desktop_main, "_find_free_port", lambda port: port)
    monkeypatch.setattr(sys, "argv", ["desktop/main.py"])
    surfaced = []
    monkeypatch.setattr(desktop_main, "_surface_startup_error", lambda *args: surfaced.append(args))
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name in ("app.database", "app.main", "app.schema_meta"):
            raise AssertionError("database startup crossed recovery barrier")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    for _attempt in range(2):
        if launcher == "desktop":
            assert desktop_main.main() == 1
            assert "recovery requires attention" in surfaced[-1][0]
        else:
            with pytest.raises(backup_service.RestoreRecoveryError):
                runpy.run_path("run.py", run_name="__main__")
        marker_text = Path(f"{live}.restore-in-progress").read_text(encoding="utf-8")
        assert marker_text == "recovery evidence"
        assert not live.exists()


def test_existing_recovery_marker_is_never_removed_by_failed_claim(tmp_path, monkeypatch):
    live, backup, originals = _restore_fault_fixture(tmp_path)
    barrier = Path(f"{live}.restore-in-progress")
    original_open = Path.open

    def raced_open(path, *args, **kwargs):
        if path == barrier and args and args[0] == "x":
            with original_open(barrier, "w", encoding="utf-8") as handle:
                handle.write("other recovery owns this marker")
            raise FileExistsError("another recovery")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", raced_open)
    with pytest.raises(backup_service.RestoreRecoveryError):
        backup_service.restore_backup(backup, live)
    assert barrier.read_text(encoding="utf-8") == "other recovery owns this marker"
    for suffix, expected in originals.items():
        assert Path(f"{live}{suffix}").read_bytes() == expected


@pytest.mark.parametrize("launcher", ["desktop", "source"])
def test_second_launcher_refuses_before_pending_restore(tmp_path, monkeypatch, launcher):
    import runpy
    import subprocess
    import sys
    import desktop.main as desktop_main
    from app import paths

    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(paths, "prepare_runtime_profile", lambda: None)
    monkeypatch.setattr(desktop_main, "_find_free_port", lambda port: port)
    monkeypatch.setattr(sys, "argv", ["desktop/main.py"])
    monkeypatch.setattr(backup_service, "apply_pending_restore", lambda:
                        (_ for _ in ()).throw(AssertionError("restore reached")))
    surfaced = []
    monkeypatch.setattr(desktop_main, "_surface_startup_error", lambda *args: surfaced.append(args))
    script = (
        "import sys; from pathlib import Path; from app import paths; "
        "from app.services import backup_service; "
        "paths.data_dir=lambda: Path(sys.argv[1]); "
        "backup_service.acquire_profile_lock(); print('locked', flush=True); sys.stdin.read()"
    )
    with subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path)], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ) as owner:
        try:
            assert owner.stdout.readline().strip() == "locked"
            if launcher == "desktop":
                assert desktop_main.main() == 1
                assert "already open" in surfaced[0][0]
            else:
                with pytest.raises(backup_service.ProfileInUseError):
                    runpy.run_path("run.py", run_name="__main__")
        finally:
            owner.communicate(timeout=5)


@pytest.mark.parametrize("fault", ["final_sync", "unlink_before", "unlink_after"])
def test_post_publication_fault_never_reports_pre_swap_failure(tmp_path, monkeypatch, fault):
    from app import app_settings, paths

    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    live = tmp_path / "portfolio.db"
    monkeypatch.setattr(backup_service, "live_db_path", lambda: live)
    _make_db(live, ["CURRENT"])
    wanted = backup_service.backups_dir() / "manual-wanted.db"
    _make_db(wanted, ["EARLIER"])
    backup_service.queue_restore(wanted.name)
    barrier = Path(f"{live}.restore-in-progress")
    real_sync = backup_service._fsync_directory
    real_unlink = Path.unlink

    def faulted_sync(directory):
        if fault == "final_sync" and directory == live.parent and not barrier.exists():
            raise OSError("final directory sync failed after publication")
        return real_sync(directory)

    def faulted_unlink(path, *args, **kwargs):
        if path == barrier and fault.startswith("unlink_"):
            if fault == "unlink_after":
                real_unlink(path, *args, **kwargs)
            raise OSError("recovery barrier removal failed")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(backup_service, "_fsync_directory", faulted_sync)
    monkeypatch.setattr(Path, "unlink", faulted_unlink)
    if fault == "unlink_before":
        with pytest.raises(backup_service.RestoreRecoveryError):
            backup_service.apply_pending_restore()
        assert barrier.exists()
        assert app_settings.load_settings()["pending_db_restore"] is not None
        with pytest.raises(backup_service.RestoreRecoveryError):
            backup_service.apply_pending_restore()
    else:
        result = backup_service.apply_pending_restore()
        assert result["status"] == "restored"
        assert app_settings.load_settings()["last_db_restore"] == result
        assert app_settings.load_settings()["pending_db_restore"] is None
        assert not barrier.exists()
        assert backup_service.apply_pending_restore() is None
    assert _holdings(live) == ["EARLIER"]
    safety = list(backup_service.backups_dir().glob("pre-manual-restore-*.db"))
    assert _holdings(safety[0]) == ["CURRENT"]
