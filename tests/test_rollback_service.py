# pylint: disable=protected-access,redefined-outer-name,unused-argument,unnecessary-lambda
"""Data-safe rollback: safety backup first, optional snapshot restore, relaunch.

Uses real on-disk SQLite so the backup/restore paths run end to end. The
installer launch is stubbed so nothing is actually executed.
"""
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import update_downloader, signature_service

from app import app_settings, paths
from app.config import settings
from app.services import backup_service, rollback_service, update_installer, update_service
from app.services.update_service import UpdateStatus


def _seed_db(path, tickers):
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE holdings (id INTEGER PRIMARY KEY, ticker TEXT)")
    conn.executemany("INSERT INTO holdings (ticker) VALUES (?)", [(t,) for t in tickers])
    conn.commit()
    conn.close()


def _holdings(path):
    conn = sqlite3.connect(str(path))
    try:
        return [r[0] for r in conn.execute("SELECT ticker FROM holdings ORDER BY id")]
    finally:
        conn.close()


@pytest.fixture
def rollback_env(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    live = tmp_path / "portfolio.db"
    monkeypatch.setattr(settings, "DATABASE_URL", f"sqlite:///{live.as_posix()}")
    _seed_db(live, ["NEW"])  # current (post-update) data

    # A pre-update snapshot the user could restore, plus an archived installer.
    snapshot = tmp_path / "backups" / "pre-update.db"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    _seed_db(snapshot, ["OLD"])
    archived = tmp_path / "updates" / "archive" / "Setup.exe"
    archived.parent.mkdir(parents=True, exist_ok=True)
    archived.write_text("installer")

    app_settings.save_settings({
        "rollback_point": {
            "version": "4.3.0",
            "db_backup": str(snapshot),
            "env_backup": None,
            "installer": str(archived),
            "created_at": "2026-07-08T00:00:00Z",
        }
    })
    info = {"asset_name": archived.name, "sha256_url": "fixture-checksums",
            "sha256_sig_url": None, "download_url": "fixture-installer"}
    release = SimpleNamespace(**info, to_dict=lambda: info)
    digest = update_downloader.compute_sha256(archived)
    monkeypatch.setattr(update_service, "fetch_release_info", lambda version: release)
    monkeypatch.setattr(update_downloader, "fetch_text", lambda url:
                        f"{digest}  {archived.name}\n")
    monkeypatch.setattr(signature_service, "is_configured", lambda: False)
    update_service._reset_for_tests()
    launched = []
    monkeypatch.setattr(update_installer, "launch_installer", lambda p: launched.append(p))
    yield {"live": live, "archived": archived, "launched": launched, "tmp": tmp_path}
    handle = backup_service._profile_lock_handles.pop(tmp_path / ".runtime.lock", None)
    if handle is not None:
        handle.close()
    update_service._reset_for_tests()


def test_can_rollback(rollback_env):
    assert rollback_service.can_rollback() is True
    app_settings.save_settings({"rollback_point": None})
    assert rollback_service.can_rollback() is False


def test_rollback_keeps_current_data_by_default(rollback_env):
    result = rollback_service.rollback(restore_data=False)

    assert result["status"] == "installing"
    assert rollback_env["launched"] == [rollback_env["archived"]]
    # Current data is untouched, and a pre-rollback safety backup was taken.
    assert _holdings(rollback_env["live"]) == ["NEW"]
    safety = list((rollback_env["tmp"] / "backups").glob("pre-rollback-*.db"))
    assert len(safety) == 1
    assert backup_service.verify_backup(safety[0], expected_min_holdings=1)


def test_rollback_can_restore_pre_update_snapshot(rollback_env):
    result = rollback_service.rollback(restore_data=True)

    assert result["status"] == "rollback_pending"
    assert _holdings(rollback_env["live"]) == ["NEW"]
    assert rollback_env["launched"] == []
    assert backup_service.apply_pending_restore()["installer_status"] == "installing"
    assert _holdings(rollback_env["live"]) == ["OLD"]
    # ...and the current ("NEW") data is preserved in the pre-rollback safety copy.
    safety = list((rollback_env["tmp"] / "backups").glob("pre-rollback-*.db"))
    assert _holdings(safety[0]) == ["NEW"]


def test_rollback_aborts_if_safety_backup_fails(rollback_env, monkeypatch):
    monkeypatch.setattr(
        backup_service, "create_backup",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
    )
    result = rollback_service.rollback(restore_data=True)

    assert result["status"] == "error"
    assert rollback_env["launched"] == []          # never handed off
    assert _holdings(rollback_env["live"]) == ["NEW"]  # data untouched


def test_rollback_refuses_while_update_in_progress(rollback_env):
    """Regression: rollback must not run concurrently with an active download/install.

    Without this guard, the always-visible Restore button could be clicked
    mid-download and interleave writes with the download thread's state
    updates. The rollback point remains untouched and nothing is launched.
    """
    for busy_status in (
        UpdateStatus.DOWNLOADING, UpdateStatus.VERIFYING,
        UpdateStatus.BACKING_UP, UpdateStatus.INSTALLING,
    ):
        update_service.mark(busy_status)
        result = rollback_service.rollback(restore_data=False)
        assert result["status"] == "error"
        assert "already in progress" in result["error"].lower()
    assert rollback_env["launched"] == []


def test_rollback_safety_backup_rejects_lost_holdings(rollback_env, monkeypatch):
    """Regression: the pre-rollback safety backup must use the DB's real count.

    A hardcoded expected_min_holdings=0 would accept a "successful" backup that
    silently lost the holdings table — this proves the fix catches it and
    refuses to proceed with the rollback.
    """
    def _empty_backup(
        source_db, label, dest_dir=None, ts=None, expected_min_holdings=None
    ):
        dest_dir = dest_dir or backup_service.backups_dir()
        dest_dir.mkdir(parents=True, exist_ok=True)
        empty = dest_dir / f"{label}-corrupt.db"
        conn = sqlite3.connect(str(empty))
        conn.execute("CREATE TABLE unrelated (id INTEGER)")
        conn.commit()
        conn.close()
        return empty

    monkeypatch.setattr(backup_service, "create_backup", _empty_backup)

    result = rollback_service.rollback(restore_data=False)

    assert result["status"] == "error"
    assert rollback_env["launched"] == []
    assert _holdings(rollback_env["live"]) == ["NEW"]  # untouched


def test_rollback_without_installer_keeps_data_safe(rollback_env, monkeypatch):
    # Archived installer missing and no network to fetch the previous release.
    app_settings.save_settings({
        "rollback_point": {
            "version": "4.3.0", "db_backup": str(rollback_env["tmp"] / "backups" / "pre-update.db"),
            "env_backup": None, "installer": None, "created_at": "2026-07-08T00:00:00Z",
        }
    })
    monkeypatch.setattr(update_service, "fetch_release_info", lambda v: None)

    result = rollback_service.rollback(restore_data=True)

    assert result["status"] == "error"
    assert "data is safe" in result["error"].lower()
    assert _holdings(rollback_env["live"]) == ["NEW"]
    assert app_settings.load_settings()["pending_db_restore"] is None


def test_queued_rollback_preserves_open_readers_and_later_writes(rollback_env):
    live = rollback_env["live"]
    reader = sqlite3.connect(str(live))
    reader.execute("PRAGMA journal_mode=WAL")
    writer = sqlite3.connect(str(live))
    try:
        assert rollback_service.rollback(restore_data=True)["status"] == "rollback_pending"
        assert reader.execute("SELECT ticker FROM holdings").fetchone()[0] == "NEW"
        writer.execute("INSERT INTO holdings (ticker) VALUES ('LATER')")
        writer.commit()
        assert reader.execute("SELECT COUNT(*) FROM holdings").fetchone()[0] == 2
    finally:
        writer.close()
        reader.close()
    result = backup_service.apply_pending_restore()
    assert result["installer_status"] == "installing"
    safety = backup_service.resolve_backup_name(result["safety_backup"])
    assert _holdings(safety) == ["NEW", "LATER"]
    assert _holdings(live) == ["OLD"]
    assert app_settings.load_settings()["pending_db_restore"] is None


def test_queued_rollback_missing_installer_retains_current_profile(rollback_env):
    assert rollback_service.rollback(restore_data=True)["status"] == "rollback_pending"
    rollback_env["archived"].unlink()
    result = backup_service.apply_pending_restore()
    assert result["status"] == "failed"
    assert _holdings(rollback_env["live"]) == ["NEW"]
    assert rollback_env["launched"] == []
    assert app_settings.load_settings()["pending_db_restore"] is None


def test_queued_rollback_launch_failure_records_restored_data(rollback_env, monkeypatch):
    assert rollback_service.rollback(restore_data=True)["status"] == "rollback_pending"
    monkeypatch.setattr(update_installer, "launch_installer", lambda path:
                        (_ for _ in ()).throw(OSError("launch refused")))
    result = backup_service.apply_pending_restore()
    assert result["status"] == "restored"
    assert result["installer_status"] == "failed"
    assert _holdings(rollback_env["live"]) == ["OLD"]
    assert _holdings(backup_service.resolve_backup_name(result["safety_backup"])) == ["NEW"]
    assert app_settings.load_settings()["last_db_restore"] == result
    assert backup_service.apply_pending_restore() is None


@pytest.mark.parametrize("archived", [False, True])
@pytest.mark.parametrize("checksum", ["missing", "wrong", "valid"])
def test_previous_installer_requires_matching_manifest(
    rollback_env, monkeypatch, archived, checksum
):
    point = dict(app_settings.load_settings()["rollback_point"])
    installer = rollback_env["archived"]
    if not archived:
        point["installer"] = None
    info = {"asset_name": installer.name, "sha256_url": None if checksum == "missing" else "sums",
            "sha256_sig_url": None, "download_url": "fixture"}
    monkeypatch.setattr(update_service, "fetch_release_info", lambda v:
                        SimpleNamespace(**info, to_dict=lambda: info))
    digest = update_downloader.compute_sha256(installer) if checksum == "valid" else "0" * 64
    monkeypatch.setattr(update_downloader, "fetch_text", lambda url: f"{digest}  {installer.name}")
    monkeypatch.setattr(update_downloader, "download_update", lambda url, dest:
                        Path(dest).write_bytes(b"installer"))
    resolved = rollback_service._resolve_previous_installer(point)
    assert bool(resolved) == (checksum == "valid")


@pytest.mark.parametrize("signature", [None, False, True])
def test_previous_installer_respects_required_signature(rollback_env, monkeypatch, signature):
    point = dict(app_settings.load_settings()["rollback_point"])
    installer = rollback_env["archived"]
    info = {"asset_name": installer.name, "sha256_url": "sums",
            "sha256_sig_url": "sig" if signature is not None else None, "download_url": "fixture"}
    monkeypatch.setattr(update_service, "fetch_release_info", lambda v:
                        SimpleNamespace(**info, to_dict=lambda: info))
    monkeypatch.setattr(signature_service, "is_configured", lambda: True)
    monkeypatch.setattr(signature_service, "verify_manifest", lambda *a: signature)
    assert bool(rollback_service._resolve_previous_installer(point)) == (signature is True)


def test_changed_archive_is_refused_before_queue(rollback_env):
    rollback_env["archived"].write_bytes(b"changed")
    assert rollback_service.rollback(restore_data=True)["status"] == "error"
    assert rollback_env["launched"] == []
    assert app_settings.load_settings()["pending_db_restore"] is None
    assert _holdings(rollback_env["live"]) == ["NEW"]


def test_changed_queued_installer_is_refused_before_restore(rollback_env):
    assert rollback_service.rollback(restore_data=True)["status"] == "rollback_pending"
    rollback_env["archived"].write_bytes(b"changed after queue")
    assert backup_service.apply_pending_restore()["status"] == "failed"
    assert rollback_env["launched"] == []
    assert _holdings(rollback_env["live"]) == ["NEW"]


@pytest.mark.parametrize("launcher", ["desktop", "source"])
def test_real_launcher_handoff_exits_before_database_import(rollback_env, monkeypatch, launcher):
    import builtins
    import runpy
    import sys
    import desktop.main as desktop_main

    assert rollback_service.rollback(restore_data=True)["status"] == "rollback_pending"
    monkeypatch.setattr(paths, "prepare_runtime_profile", lambda: None)
    monkeypatch.setattr(desktop_main, "_find_free_port", lambda port: port)
    monkeypatch.setattr(sys, "argv", ["desktop/main.py"])
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name in ("app.database", "app.main", "app.schema_meta"):
            raise AssertionError("launcher opened application database after installer handoff")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    if launcher == "desktop":
        assert desktop_main.main() == 0
    else:
        with pytest.raises(SystemExit) as exited:
            runpy.run_path("run.py", run_name="__main__")
        assert exited.value.code == 0
    assert rollback_env["launched"] == [rollback_env["archived"]]
    assert _holdings(rollback_env["live"]) == ["OLD"]
