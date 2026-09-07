"""Restore the previous version safely, without ever losing current data.

A rollback point is recorded before each in-app update (see
:mod:`app.services.update_installer`): the outgoing version plus a verified
snapshot of the database and ``.env`` at that moment. Rolling back:

1. Takes a *fresh* verified backup of the CURRENT data first, so whichever data
   the user ends up with, the other copy is always recoverable — a rollback can
   never corrupt or silently discard newer data.
2. Optionally queues the pre-update snapshot for a clean restart of the current
   binary (the user's explicit choice); the
   default keeps current data, which an older binary can still read because
   migrations are additive-only.
3. Reinstalls the previous version's binary — from the archived installer if
   present, otherwise re-downloaded from that version's release — then quits so
   the installer can run.

The data steps are fully offline (backups are local). Only reinstalling the
binary may need the network; if verification is unavailable, current data is
kept and the user is pointed at the releases page.
"""
from __future__ import annotations

import logging

from app.services import (
    backup_service,
    update_downloader,
    update_installer,
    update_log,
    update_service,
)
from app.services.update_service import UpdateStatus

logger = logging.getLogger(__name__)

# Statuses that mean an update download/verify/backup/install is actively in
# flight. Rollback refuses to start while one of these is active — running
# both at once would interleave writes to update_service's shared state and
# could overlap two OS-level install handoffs.
_BUSY_STATUSES = {
    UpdateStatus.DOWNLOADING.value,
    UpdateStatus.VERIFYING.value,
    UpdateStatus.BACKING_UP.value,
    UpdateStatus.INSTALLING.value,
    UpdateStatus.ROLLBACK_PENDING.value,
}


def _rollback_point() -> dict | None:
    from app import app_settings

    return app_settings.load_settings().get("rollback_point")


def can_rollback() -> bool:
    """True when a rollback point with an existing, verified DB backup is present."""
    from pathlib import Path

    point = _rollback_point()
    if not point or not point.get("db_backup"):
        return False
    return Path(point["db_backup"]).exists()


def rollback(restore_data: bool = False) -> dict:  # pylint: disable=too-many-return-statements
    """Roll back to the previous version. See module docstring for the contract."""
    current = update_service.get_state()
    if current.get("status") in _BUSY_STATUSES:
        return update_service.mark(
            UpdateStatus.ERROR,
            error="An update is already in progress. Wait for it to finish before restoring "
            "a previous version.",
        )

    rollback_point = _rollback_point()
    if not rollback_point or not rollback_point.get("db_backup"):
        return update_service.mark(
            UpdateStatus.ERROR, error="There's no previous version to restore."
        )

    update_service.mark(UpdateStatus.BACKING_UP)

    # 1. Always snapshot current data first — nothing newer is ever lost.
    try:
        backup_service.create_verified_backup(
            label="pre-rollback",
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Pre-rollback safety backup failed: %s", type(exc).__name__)
        return update_service.mark(
            UpdateStatus.ERROR,
            error="Couldn't safeguard your current data, so the rollback was paused.",
        )

    # Verify the exact installer before requesting any data transition.
    verified_installer = _resolve_previous_installer(rollback_point)
    if verified_installer is None:
        update_log.event("rollback: previous installer unavailable or unverified")
        return update_service.mark(
            UpdateStatus.ERROR,
            error="Your data is safe. The previous installer couldn't be verified. "
            "Reinstall it from the releases page to finish rolling back.",
        )
    installer, installer_digest = verified_installer

    if restore_data:
        return _queue_data_rollback(rollback_point, installer, installer_digest)

    try:
        if update_downloader.compute_sha256(installer) != installer_digest:
            raise ValueError("Verified installer changed before handoff")
        update_installer.launch_installer(installer)
    except Exception:  # pylint: disable=broad-except
        logger.exception("Failed to launch rollback installer")
        return update_service.mark(
            UpdateStatus.ERROR, error="Couldn't start the previous version's installer."
        )

    update_log.event(f"rollback handoff to version={rollback_point.get('version')}")
    state = update_service.mark(UpdateStatus.INSTALLING)
    update_installer.schedule_exit()
    return state



def _queue_data_rollback(rollback_point: dict, installer, installer_digest: str) -> dict:
    """Persist explicit consent for the current binary's next clean startup."""
    from app import app_settings
    from pathlib import Path

    try:
        if app_settings.load_settings().get("pending_db_restore"):
            raise ValueError("A database restore is already queued")
        snapshot = backup_service.resolve_backup_name(Path(rollback_point["db_backup"]).name)
        if snapshot.resolve() != Path(rollback_point["db_backup"]).resolve():
            raise ValueError("Rollback snapshot is outside the backup vault")
        if not backup_service.verify_backup(snapshot):
            raise ValueError("Rollback snapshot failed verification")
        pending = {
            "name": snapshot.name,
            "kind": "rollback",
            "installer": str(installer),
            "installer_sha256": installer_digest,
        }
        if rollback_point.get("env_backup"):
            environment = Path(rollback_point["env_backup"])
            if environment.resolve().parent != backup_service.backups_dir().resolve():
                raise ValueError("Rollback environment is outside the backup vault")
            pending["environment"] = environment.name
        app_settings.save_settings({"pending_db_restore": pending})
    except Exception:
        logger.exception("Could not queue rollback for a clean restart")
        return update_service.mark(
            UpdateStatus.ERROR, error="Couldn't queue the rollback. Your data is unchanged."
        )
    # Only the current binary can guarantee the new startup safety boundary.
    return update_service.mark(UpdateStatus.ROLLBACK_PENDING)


def _resolve_previous_installer(rollback_point: dict):
    """Return the installer path and its trusted release digest, or refuse."""
    from pathlib import Path

    version = rollback_point.get("version")
    if not version:
        return None
    try:
        info = update_service.fetch_release_info(version)
        if not info or not info.asset_name or not info.sha256_url:
            return None
        archived = rollback_point.get("installer")
        if archived and Path(archived).is_file():
            dest = Path(archived)
        else:
            if not info.download_url:
                return None
            dest = update_downloader.archive_dir() / info.asset_name
            update_downloader.download_update(info.download_url, dest)
        digest = update_installer.verify_release_installer(dest, info.to_dict())
        if not digest:
            logger.error("Rollback installer failed verification")
            return None
        return dest, digest
    except Exception:  # pylint: disable=broad-except
        logger.exception("Failed to verify previous installer")
        return None
