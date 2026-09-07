"""Safe backup, verification, and restore of the SQLite portfolio database.

Holdings data is the app's most valuable state, so every backup goes through the
SQLite *online backup API* (``sqlite3.Connection.backup``) rather than a raw file
copy. The online API is the only WAL-safe way to snapshot a live database: it
copies a transactionally consistent set of pages even while the app is reading
and writing, and it checkpoints the WAL contents into the standalone backup file
so the result is a single, self-contained ``.db`` with no ``-wal``/``-shm``
sidecars to keep in sync.

Restores never delete the current files — the (possibly broken) live database is
moved aside as ``*.failed-<timestamp>`` for inspection before the verified backup
is copied into place.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import sqlite3
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from app import paths
from app.services import env_file

logger = logging.getLogger(__name__)

BACKUP_DIRNAME = "backups"
DEFAULT_KEEP = 5
MANUAL_KEEP = 12
AUTO_KEEP = 7
AUTO_CLAIM_KEEP_DAYS = 45
BACKUP_POLICY_FILENAME = "backup-policy.json"
_operation_lock = threading.RLock()
_operation_state = threading.local()
_profile_lock_handles: dict = {}

BACKUP_POLICY_DEFAULTS = {
    "auto_backup_enabled": False,
    "last_auto_backup": None,
}


@dataclass(frozen=True)
class VerifiedBackup:
    """A database artifact that passed FolioOrb's safety checks."""

    database: Path
    environment: Path | None = None


class EnvironmentSnapshotError(RuntimeError):
    """Environment capture failed after the database artifact was verified."""

    def __init__(self, backup: VerifiedBackup):
        super().__init__("Environment snapshot failed after database backup succeeded")
        self.backup = backup


class RestoreRecoveryError(RuntimeError):
    """A restore failed and the original canonical files could not be republished."""


class ProfileInUseError(RuntimeError):
    """Another launcher still owns this profile and its database connections."""


def acquire_profile_lock() -> None:
    """Hold an OS lock until process exit, before opening the canonical database."""
    lock_path = paths.data_dir() / ".runtime.lock"
    if lock_path in _profile_lock_handles:
        return
    handle = lock_path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise ProfileInUseError(
            "This FolioOrb profile is already open. Quit the other FolioOrb process "
            "before restarting or restoring its data."
        ) from exc
    _profile_lock_handles[lock_path] = handle


def _restore_barrier(target: Path) -> Path:
    return Path(f"{target}.restore-in-progress")


def require_completed_restore(target: Path) -> None:
    """Refuse startup after interrupted publication, independently of settings.json."""
    if _restore_barrier(target).exists():
        raise RestoreRecoveryError(
            "Database restore recovery is incomplete. Keep all database, staging and "
            "failed files intact; resolve recovery before restarting FolioOrb."
        )


def _fsync_directory(directory: Path) -> None:
    """Persist a completed POSIX directory-entry change before reporting success."""
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(str(directory), flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _lock_file(handle) -> None:
    """Acquire one blocking cross-process byte lock on macOS/Linux/Windows."""
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
        os.fsync(handle.fileno())
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        except OSError as exc:
            logger.error("Timed out acquiring the cross-process backup lock on Windows")
            raise TimeoutError("Backup operation lock timed out") from exc
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_file(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def backup_operation(dest_dir: Path | None = None):
    """Serialize backup create/verify/publish/prune across threads and processes."""
    with _operation_lock:
        depth = int(getattr(_operation_state, "depth", 0))
        if depth:
            _operation_state.depth = depth + 1
            try:
                yield
            finally:
                _operation_state.depth -= 1
            return

        destination = Path(dest_dir) if dest_dir else backups_dir()
        lock_root = destination.parent
        lock_root.mkdir(parents=True, exist_ok=True)
        lock_path = lock_root / ".folioorb-backup-operation.lock"
        with open(lock_path, "a+b") as handle:
            try:
                os.chmod(lock_path, 0o600)
            except OSError:
                pass
            _lock_file(handle)
            _operation_state.depth = 1
            try:
                yield
            finally:
                _operation_state.depth = 0
                _unlock_file(handle)


def backups_dir(*, create: bool = True) -> Path:
    """Return the backup-vault directory, optionally creating it.

    Inventory and download paths pass ``create=False`` so merely opening the
    Backup Vault never changes the filesystem. Snapshot-producing paths keep
    the default and create the directory on first write.
    """
    directory = paths.data_dir() / BACKUP_DIRNAME
    if create:
        directory.mkdir(parents=True, exist_ok=True)
    return directory


def env_path() -> Path:
    """Path to the per-user ``.env`` (Claude key etc.)."""
    return paths.data_dir() / ".env"


def snapshot_env(dest_path: Path) -> Path | None:
    """Securely snapshot the current ``.env`` if it exists; else return None."""
    src = env_path()
    try:
        src.lstat()
    except FileNotFoundError:
        return None
    dest_path = Path(dest_path)
    env_file.copy_env_file(src, dest_path)
    return dest_path


def restore_env(env_backup: Path, ts: str | None = None) -> bool:
    """Atomically restore a private ``.env`` while preserving the current one."""
    env_backup = Path(env_backup)
    try:
        env_backup.lstat()
    except FileNotFoundError:
        return False
    current = env_path()
    stamp = ts or _timestamp()
    staging = current.parent / (
        f".{current.name}.restore-{stamp}-{secrets.token_hex(6)}"
    )
    try:
        # Validate, copy, chmod, and fsync the replacement before touching any
        # current profile state.
        env_file.copy_env_file(env_backup, staging)
        try:
            current.lstat()
        except FileNotFoundError:
            pass
        else:
            # Publish a private recovery copy; never move the canonical file
            # away before the complete replacement is staged and ready.
            env_file.copy_env_file(current, Path(f"{current}.failed-{stamp}"))
        env_file.copy_env_file(staging, current)
        return True
    finally:
        _safe_remove(staging)


def live_db_path() -> Path:
    """Filesystem path of the live SQLite database from the configured URL.

    Raises ``ValueError`` for non-file databases (non-SQLite or ``:memory:``),
    which cannot be backed up and are only used in tests/dev.
    """
    from app.config import settings

    url = settings.DATABASE_URL
    if not url.startswith("sqlite") or ":memory:" in url:
        raise ValueError("Backups require a file-based SQLite database")
    return Path(url.replace("sqlite:///", "", 1))


def resolve_backup_name(name: str) -> Path:
    """Resolve one vault basename without allowing path traversal."""
    raw = str(name or "").strip()
    safe = Path(raw).name
    if not raw or safe != raw or not safe.endswith(".db"):
        raise ValueError("Invalid backup name")
    vault = backups_dir(create=False).resolve()
    path = (vault / safe).resolve()
    if path.parent != vault:
        raise ValueError("Invalid backup name")
    return path


def _vault_connection(path: Path) -> sqlite3.Connection:
    """Open one stable vault artifact without creating SQLite sidecars.

    ``immutable=1`` is intentionally confined to closed backup artifacts. It
    must never be used for the live database, whose committed state may still
    reside in WAL. A non-empty sibling WAL means the artifact is not standalone
    and is therefore refused rather than read incompletely.
    """
    path = Path(path)
    wal = Path(f"{path}-wal")
    if wal.exists() and wal.stat().st_size > 0:
        raise sqlite3.DatabaseError("Backup has an uncommitted WAL sidecar")
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)


def _count_vault_holdings(db_path: Path) -> int:
    """Count holdings in a closed vault artifact without mutating it."""
    db_path = Path(db_path)
    if not db_path.exists():
        return 0
    try:
        conn = _vault_connection(db_path)
    except (OSError, sqlite3.DatabaseError):
        return 0
    try:
        return conn.execute("SELECT COUNT(*) FROM holdings").fetchone()[0]
    except sqlite3.DatabaseError:
        return 0
    finally:
        conn.close()


def backup_info(path: Path) -> dict:
    """Public, secret-free metadata for one database backup."""
    path = Path(path)
    return {
        "name": path.name,
        "size_bytes": path.stat().st_size,
        "created_at": datetime.fromtimestamp(
            path.stat().st_mtime, tz=timezone.utc
        ).isoformat(),
        "verified": verify_vault_backup(path),
        "holding_count": _count_vault_holdings(path),
    }


def list_backups() -> list[dict]:
    """Newest-first inventory of the local database vault."""
    vault = backups_dir(create=False)
    if not vault.exists():
        return []
    with backup_operation(vault):
        backup_paths = sorted(
            vault.glob("*.db"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        return [backup_info(path) for path in backup_paths]


def _policy_path() -> Path:
    return paths.data_dir() / BACKUP_POLICY_FILENAME


def load_backup_policy() -> dict:
    """Read the backup-only policy sidecar; malformed state degrades to defaults."""
    policy = dict(BACKUP_POLICY_DEFAULTS)
    path = _policy_path()
    if not path.exists():
        return policy
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return policy
    except (OSError, json.JSONDecodeError):
        return policy
    if isinstance(raw.get("auto_backup_enabled"), bool):
        policy["auto_backup_enabled"] = raw["auto_backup_enabled"]
    if raw.get("last_auto_backup") is None or isinstance(raw.get("last_auto_backup"), dict):
        policy["last_auto_backup"] = raw.get("last_auto_backup")
    return policy


def _write_policy_unlocked(policy: dict) -> dict:
    path = _policy_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=".backup-policy-", suffix=".tmp", dir=str(path.parent)
    )
    try:
        os.chmod(temp_name, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(policy, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        _fsync_directory(path.parent)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return policy


def set_auto_backup_enabled(enabled: bool) -> dict:
    """Persist the opt-in preference without touching unrelated app settings."""
    vault = backups_dir()
    with backup_operation(vault):
        policy = load_backup_policy()
        policy["auto_backup_enabled"] = bool(enabled)
        return _write_policy_unlocked(policy)


def manual_backup_freshness(now: datetime | None = None) -> dict:
    """Age the newest currently verified manual snapshot, skipping corrupt files."""
    vault = backups_dir(create=False)
    skipped = 0
    newest_info = None
    newest_mtime = None
    if vault.exists():
        with backup_operation(vault):
            candidates = sorted(
                vault.glob("manual-*.db"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
            newest = None
            for candidate in candidates:
                if verify_vault_backup(candidate):
                    newest = candidate
                    break
                skipped += 1
            if newest is not None:
                newest_info = backup_info(newest)
                newest_mtime = newest.stat().st_mtime
    if newest_info is None or newest_mtime is None:
        return {
            "status": "none",
            "age_days": None,
            "latest": None,
            "skipped_unverified": skipped,
            "needs_attention": True,
            "same_device_only": True,
        }

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    modified = datetime.fromtimestamp(newest_mtime, tz=timezone.utc)
    age_days = max(0, int((current - modified).total_seconds() // 86_400))
    status = "current" if age_days <= 7 else "due" if age_days <= 30 else "stale"
    return {
        "status": status,
        "age_days": age_days,
        "latest": newest_info,
        "skipped_unverified": skipped,
        "needs_attention": status != "current",
        "same_device_only": True,
    }


def backup_protection_status() -> dict:
    """Public manual-freshness and automatic-backup policy state."""
    return {
        "manual_freshness": manual_backup_freshness(),
        "automatic": load_backup_policy(),
        "auto_keep": AUTO_KEEP,
        "limitations": [
            "Backups stay on this device unless you copy them elsewhere.",
            "Manual freshness ignores automatic, update, and restore snapshots.",
        ],
    }


def _claim_auto_day(vault: Path, local_day: date) -> Path | None:
    claim = vault / f"auto-{local_day.isoformat()}.claim"
    try:
        fd = os.open(claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return None
    try:
        payload = json.dumps({
            "local_date": local_day.isoformat(),
            "attempted_at_utc": datetime.now(timezone.utc).isoformat(),
            "pid": os.getpid(),
        }).encode("utf-8")
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_directory(vault)
    return claim


def _prune_old_claims(vault: Path, now: datetime) -> None:
    cutoff = now.timestamp() - AUTO_CLAIM_KEEP_DAYS * 86_400
    removed = False
    for claim in vault.glob("auto-*.claim"):
        try:
            if claim.stat().st_mtime < cutoff:
                claim.unlink()
                removed = True
        except OSError:
            logger.warning("Could not prune old automatic-backup claim %s", claim.name)
    if removed:
        _fsync_directory(vault)


def _prune_auto_backups(vault: Path) -> None:
    verified = []
    removed = False
    for path in sorted(
        vault.glob("auto-*.db"), key=lambda item: item.stat().st_mtime, reverse=True
    ):
        if verify_vault_backup(path):
            verified.append(path)
        else:
            _safe_remove(path)
            removed = True
    for old in verified[AUTO_KEEP:]:
        _safe_remove(old)
        removed = True
    if removed:
        _fsync_directory(vault)


def maybe_create_automatic_backup(local_day: date | None = None) -> dict:
    """Attempt at most one verified automatic backup per local calendar day."""
    day = local_day or date.today()
    if not load_backup_policy()["auto_backup_enabled"]:
        return {"status": "disabled", "local_date": day.isoformat()}

    vault = backups_dir()
    with backup_operation(vault):
        policy = load_backup_policy()
        if not policy["auto_backup_enabled"]:
            return {"status": "disabled", "local_date": day.isoformat()}
        claim = _claim_auto_day(vault, day)
        if claim is None:
            return {"status": "already_attempted", "local_date": day.isoformat()}

        attempted_at = datetime.now(timezone.utc)
        running = {
            "status": "running",
            "local_date": day.isoformat(),
            "attempted_at_utc": attempted_at.isoformat(),
        }
        policy["last_auto_backup"] = running
        _write_policy_unlocked(policy)
        backup = None
        try:
            point = create_verified_backup(
                label="auto",
                dest_dir=vault,
                require_vault_schema=True,
            )
            backup = point.database
            _prune_auto_backups(vault)
            _prune_old_claims(vault, attempted_at)
            result = {
                "status": "succeeded",
                "local_date": day.isoformat(),
                "attempted_at_utc": attempted_at.isoformat(),
                "backup": backup_info(backup),
            }
        except Exception as exc:  # pylint: disable=broad-except
            if backup is not None:
                _safe_remove(backup)
            logger.error("Automatic backup failed: %s", type(exc).__name__)
            result = {
                "status": "failed",
                "local_date": day.isoformat(),
                "attempted_at_utc": attempted_at.isoformat(),
                "error": type(exc).__name__,
            }
        policy = load_backup_policy()
        policy["last_auto_backup"] = result
        _write_policy_unlocked(policy)
        return result


def start_auto_backup_check() -> None:
    """Run the launch-triggered opt-in backup check away from startup latency."""
    threading.Thread(target=maybe_create_automatic_backup, daemon=True).start()


def create_manual_backup() -> dict:
    """Create and verify a user-requested database-only vault snapshot."""
    vault = backups_dir()
    with backup_operation(vault):
        point = create_verified_backup(
            label="manual",
            dest_dir=vault,
            require_vault_schema=True,
        )
        backup = point.database
        # A user-created snapshot must never evict an update or pre-restore
        # rollback point. Retention applies only to other manual snapshots.
        prune_backups(vault, keep=MANUAL_KEEP, pattern="manual-*.db")
        return backup_info(backup)


def queue_restore(name: str) -> dict:
    """Verify a vault item and queue it for the next clean process start."""
    from app import app_settings

    backup = resolve_backup_name(name)
    if not verify_vault_backup(backup):
        raise ValueError("Refusing to queue an unverified backup")
    requested_at = datetime.now(timezone.utc).isoformat()
    pending = {"name": backup.name, "requested_at": requested_at}
    app_settings.save_settings({"pending_db_restore": pending})
    return pending


def apply_pending_restore() -> dict | None:
    """Apply a queued restore before the database engine is imported.

    The current database gets its own verified safety backup first. A failed
    pre-publication request is cleared so one bad vault item cannot trap the
    app in a startup loop. Incomplete canonical recovery is a hard, persistent
    startup barrier; no database is opened until recovery is resolved.
    """
    from app import app_settings

    live = live_db_path()
    require_completed_restore(live)
    settings = app_settings.load_settings()
    pending = settings.get("pending_db_restore")
    if not isinstance(pending, dict) or not pending.get("name"):
        return None

    now = datetime.now(timezone.utc).isoformat()
    try:
        is_rollback = pending.get("kind") == "rollback"
        if is_rollback:
            from app.services import update_downloader

            if not pending.get("installer_sha256") or update_downloader.compute_sha256(
                Path(pending["installer"])
            ) != pending["installer_sha256"]:
                raise ValueError("Queued installer changed; current data retained")
        requested = resolve_backup_name(str(pending["name"]))
        verifier = verify_backup if is_rollback else verify_vault_backup
        if not verifier(requested):
            raise ValueError("Queued backup failed verification")
        safety_name = None
        if live.exists():
            safety = create_verified_backup(
                label="pre-rollback-restore" if is_rollback else "pre-manual-restore",
                include_environment=is_rollback,
            ).database
            safety_name = safety.name
        restore_backup(requested, live)
        result = {
            "status": "restored",
            "name": requested.name,
            "safety_backup": safety_name,
            "completed_at": now,
        }
        if is_rollback and pending.get("environment"):
            try:
                restore_env(backups_dir() / Path(pending["environment"]).name)
                result["environment_status"] = "restored"
            except Exception:  # pylint: disable=broad-except
                result["environment_status"] = "failed"
                logger.error("Database restored, but saved environment could not be restored")
    except RestoreRecoveryError:
        # The sidecar created before publication survives even if settings
        # cannot be written. Never clear the request or open canonical SQLite.
        raise
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Queued database restore failed: %s", type(exc).__name__)
        require_completed_restore(live)
        result = {
            "status": "failed",
            "name": str(pending.get("name") or ""),
            "error": type(exc).__name__,
            "completed_at": now,
        }
    app_settings.save_settings({
        "pending_db_restore": None,
        "last_db_restore": result,
    })
    if result["status"] == "restored" and pending.get("kind") == "rollback":
        from app.services import update_installer

        try:
            # Recheck the pinned release digest immediately before handoff.
            if update_downloader.compute_sha256(Path(pending["installer"])) != (
                pending["installer_sha256"]
            ):
                raise ValueError("Queued installer changed before handoff")
            update_installer.launch_installer(Path(pending["installer"]))
            result["installer_status"] = "installing"
        except Exception:
            logger.exception("Earlier data restored, but rollback installer could not start")
            result["installer_status"] = "failed"
        app_settings.save_settings({"last_db_restore": result})
    return result


def _timestamp() -> str:
    moment = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    return f"{moment}-{secrets.token_hex(3)}"


def count_holdings(db_path: Path) -> int:
    """Row count of the ``holdings`` table in ``db_path`` (0 if missing/absent).

    Callers use this *before* taking a safety-critical backup so
    ``verify_backup`` can be given the database's real current count instead of
    a hardcoded ``0`` — otherwise a backup that silently lost the holdings
    table would still pass verification (0 rows satisfies an expectation of 0).
    """
    db_path = Path(db_path)
    if not db_path.exists():
        return 0
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("SELECT COUNT(*) FROM holdings").fetchone()[0]
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


def create_backup(
    source_db: Path,
    label: str,
    dest_dir: Path | None = None,
    ts: str | None = None,
    expected_min_holdings: int | None = None,
) -> Path:
    """Snapshot ``source_db`` into ``dest_dir`` using the online backup API.

    The filename is ``<label>-<timestamp>.db``. ``ts`` may be supplied for
    deterministic tests. Expected holdings are checked while the file is still
    private staging state, before its no-replace publication. Returns the path
    to the created backup.
    """
    source_db = Path(source_db)
    if not source_db.exists():
        raise FileNotFoundError(f"Source database not found: {source_db}")
    dest_dir = Path(dest_dir) if dest_dir else backups_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)

    with backup_operation(dest_dir):
        published = None
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{label}-", suffix=".staging", dir=str(dest_dir)
        )
        temp_path = Path(temp_name)
        os.close(fd)
        try:
            os.chmod(temp_path, 0o600)
            source_conn = sqlite3.connect(str(source_db))
            try:
                dest_conn = sqlite3.connect(str(temp_path))
                try:
                    with dest_conn:
                        source_conn.backup(dest_conn)
                finally:
                    dest_conn.close()
            finally:
                source_conn.close()
            # Windows rejects fsync on a read-only CRT descriptor (EBADF), even
            # though POSIX accepts it. Open the already-complete staging file
            # read/write solely for the durability flush; no bytes are changed.
            with open(temp_path, "r+b") as handle:
                os.fsync(handle.fileno())
            if not verify_backup(
                temp_path, expected_min_holdings=expected_min_holdings
            ):
                raise ValueError("Backup failed integrity verification")

            stamp = ts or _timestamp()
            dest = dest_dir / f"{label}-{stamp}.db"
            # Hard-link publication is atomic and never replaces an existing
            # artifact. The staging link is then removed, leaving the verified
            # inode published under its final collision-proof name.
            os.link(temp_path, dest)
            published = dest
            temp_path.unlink()
            _fsync_directory(dest_dir)
        except Exception:
            _safe_remove(temp_path)
            if published is not None:
                _safe_remove(published)
            raise

    logger.info("Created database backup %s", dest.name)
    return dest


def verify_backup(backup_path: Path, expected_min_holdings: int | None = None) -> bool:
    """Return True only if ``backup_path`` is a healthy, non-empty SQLite file.

    Runs ``PRAGMA integrity_check`` and, when ``expected_min_holdings`` is given,
    confirms the ``holdings`` table has at least that many rows. A missing
    holdings table counts as valid only when zero rows are expected (a fresh DB).
    """
    backup_path = Path(backup_path)
    if not backup_path.exists() or backup_path.stat().st_size == 0:
        return False

    conn = None
    try:
        conn = _vault_connection(backup_path)
        result = conn.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            return False
        if expected_min_holdings is not None:
            try:
                count = conn.execute("SELECT COUNT(*) FROM holdings").fetchone()[0]
            except sqlite3.OperationalError:
                return expected_min_holdings == 0
            return count >= expected_min_holdings
        return True
    except (OSError, sqlite3.DatabaseError):
        return False
    finally:
        if conn is not None:
            conn.close()


def verify_vault_backup(backup_path: Path) -> bool:
    """Require both SQLite integrity and FolioOrb's holdings schema."""
    backup_path = Path(backup_path)
    if not verify_backup(backup_path):
        return False
    try:
        conn = _vault_connection(backup_path)
    except (OSError, sqlite3.DatabaseError):
        return False
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='holdings'"
        ).fetchone()
        return row is not None
    except sqlite3.DatabaseError:
        return False
    finally:
        conn.close()


def create_verified_backup(
    label: str,
    *,
    source_db: Path | None = None,
    dest_dir: Path | None = None,
    include_environment: bool = False,
    require_vault_schema: bool = False,
) -> VerifiedBackup:
    """Create one independently verified database backup and optional env copy.

    Callers cannot omit the live holdings count or independent verification.
    """
    source = Path(source_db) if source_db is not None else live_db_path()
    expected = count_holdings(source)
    database = create_backup(
        source,
        label=label,
        dest_dir=dest_dir,
        expected_min_holdings=expected,
    )
    verified = verify_backup(database, expected_min_holdings=expected)
    if verified and require_vault_schema:
        verified = verify_vault_backup(database)
    if not verified:
        _safe_remove(database)
        raise ValueError("Backup failed post-publication verification")

    point = VerifiedBackup(database=database)
    if not include_environment:
        return point
    try:
        environment = snapshot_env(Path(f"{database}.env"))
    except Exception as exc:
        # The verified DB remains intentionally recoverable and is carried by
        # the typed error; it must never be mistaken for a complete update
        # rollback point when environment capture was requested.
        raise EnvironmentSnapshotError(point) from exc
    return VerifiedBackup(database=database, environment=environment)


def restore_backup(backup_path: Path, target_db: Path, ts: str | None = None) -> bool:
    """Restore ``backup_path`` over ``target_db`` without destroying the current file.

    All profile connections must be closed. Stage and verify the replacement
    before moving the database and WAL sidecars to ``*.failed-<timestamp>``.
    Keep a persistent startup barrier if canonical recovery is incomplete.
    Returns True after publication, including nonfatal directory-sync faults.
    """
    backup_path, target_db = Path(backup_path), Path(target_db)
    require_completed_restore(target_db)
    if not verify_backup(backup_path):
        raise ValueError("Refusing to restore an unverified backup")

    stamp = ts or _timestamp()
    target_db.parent.mkdir(parents=True, exist_ok=True)
    staging = target_db.parent / f"{target_db.name}.staging-{stamp}"
    shutil.copyfile(backup_path, staging)
    if not verify_backup(staging):
        _safe_remove(staging)
        raise ValueError("Restored copy failed verification — live database left untouched")

    # Persist the barrier BEFORE moving any canonical byte. An interrupted
    # process or incomplete recovery must remain blocked on every later start.
    barrier = _restore_barrier(target_db)
    try:
        with barrier.open("x", encoding="utf-8") as handle:
            json.dump({"staging": staging.name, "stamp": stamp}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(target_db.parent)
    except FileExistsError as exc:
        raise RestoreRecoveryError("Another restore owns the recovery barrier") from exc
    except OSError:
        _safe_remove(barrier)
        raise
    moved: list[tuple[Path, Path]] = []
    try:
        for suffix in ("", "-wal", "-shm"):
            current = Path(str(target_db) + suffix)
            if current.exists():
                recovery = Path(f"{current}.failed-{stamp}")
                current.replace(recovery)
                moved.append((current, recovery))
        staging.replace(target_db)
    except OSError as forward_error:
        _rollback_restore_moves(moved, target_db.parent, forward_error)
        barrier.unlink()
        _fsync_directory(target_db.parent)
        raise

    try:
        _fsync_directory(target_db.parent)
    except OSError:
        # Publication is complete and the restored file is readable. Directory
        # fsync is a durability improvement, not a power-loss guarantee.
        logger.warning("Could not fsync restored database directory")
    try:
        barrier.unlink()
        _fsync_directory(target_db.parent)
    except OSError as exc:
        if barrier.exists():
            raise RestoreRecoveryError("Restored database recovery barrier remains") from exc
        logger.warning("Restore completed; could not sync recovery barrier removal")
    logger.info("Restored database from backup %s", backup_path.name)
    return True


def _rollback_restore_moves(
    moved: list[tuple[Path, Path]], directory: Path, forward_error: OSError
) -> None:
    """Return originals to canonical paths, retaining a copy if rename rollback faults."""
    recovery_failures: list[str] = []
    for current, recovery in reversed(moved):
        try:
            recovery.replace(current)
            continue
        except OSError:
            # A second rename fault must not strand the original away from its
            # canonical name. Copying retains the recovery artifact as well.
            try:
                shutil.copy2(recovery, current)
            except OSError as copy_error:
                recovery_failures.append(
                    f"{current.name}:{type(copy_error).__name__}"
                )
    try:
        _fsync_directory(directory)
    except OSError:
        logger.warning("Could not fsync database directory after restore rollback")
    if recovery_failures:
        details = ", ".join(recovery_failures)
        raise RestoreRecoveryError(
            f"Restore failed and canonical recovery was incomplete ({details})"
        ) from forward_error


def _safe_remove(path: Path) -> None:
    try:
        path.unlink()
    except OSError as exc:
        logger.debug("Could not remove staging file %s: %s", path.name, type(exc).__name__)


def prune_backups(
    dest_dir: Path | None = None,
    keep: int = DEFAULT_KEEP,
    *,
    pattern: str = "*.db",
) -> list[Path]:
    """Delete all but the ``keep`` newest matching backups.

    The optional pattern keeps independently managed classes of rollback point
    from evicting one another.
    """
    destination = Path(dest_dir) if dest_dir else backups_dir()
    if not destination.exists():
        return []
    with backup_operation(destination):
        backups = sorted(
            destination.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True
        )
        removed: list[Path] = []
        for old in backups[keep:]:
            try:
                old.unlink()
                removed.append(old)
            except OSError:
                logger.warning("Could not prune old backup %s", old.name)
        if removed:
            _fsync_directory(destination)
        return removed
