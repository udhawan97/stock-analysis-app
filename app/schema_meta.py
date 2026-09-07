"""Database schema versioning and the safe-migration entry point.

The app's tables are created by ``create_all`` and evolved by the idempotent
raw-SQL steps in ``app.database.ensure_startup_migrations``. This module wraps
both in a *protected* sequence:

* An ``app_meta`` key/value table records the on-disk ``schema_version``.
* When the stored version is behind the code's ``SCHEMA_VERSION`` **and** the
  database already holds user data, a verified backup is taken *before* any
  migration runs.
* If the migration then raises, the verified backup is restored and the broken
  database is set aside as ``*.failed-*`` for inspection.

Invariant: holdings data is never mutated by a version-bumping migration without
a recoverable, integrity-checked copy on disk first.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

# Bump this whenever a new migration changes the on-disk schema shape. A bump
# triggers the backup-first path on existing databases. SCHEMA_VERSION = 1 is the
# baseline: the schema shipped through v4.3.x (hold_class, is_watchlist,
# verdict_snapshots, the performance indexes, and the snapshot uniqueness index).
# v2 adds the DCA tables (dca_plans, dca_contributions) — additive-only, created
# by create_all, so MIN_COMPATIBLE_APP_VERSION is unchanged.
# v3 adds the additive dca_plans.catchup_floor column (ALTER in
# ensure_startup_migrations) — still additive, MIN_COMPATIBLE unchanged.
# v4 adds the additive verdict_snapshots.portfolio_id column (ALTER + backfill
# to portfolio 1) for per-portfolio verdict history — additive, MIN_COMPATIBLE
# unchanged.
# v5 adds optional thesis review timestamps and cadence to holdings. Both are
# additive and ignored by older binaries, so rollback compatibility is unchanged.
# v6 adds optional integer target weights to holdings. The column is additive,
# nullable, and ignored by older binaries, so rollback compatibility is unchanged.
# v7 enforces one active row per (portfolio_id, ticker). A read-only preflight
# blocks legacy duplicates for explicit user resolution; it never auto-dedupes.
# v8 adds realized sale currency and price provenance. Legacy currency remains
# NULL and provenance is marked unknown, so no migration guesses a USD fact.
# v9 adds DCA plan/contribution currency provenance. Legacy rows remain NULL and
# unknown, so catch-up/apply fail closed without ticker-based currency inference.
SCHEMA_VERSION = 9

# Oldest app version whose ORM models can still read this schema. Additive-only
# migrations (new tables/columns/indexes) keep this unchanged, so a normal
# rollback to a recent prior version always works. A *destructive* migration must
# raise this value; every populated version bump requires a verified backup.
MIN_COMPATIBLE_APP_VERSION = "4.3.0"

_META_TABLE = "app_meta"

# Tables that indicate the database already holds real user data. Checked by
# name rather than just "holdings" so a database that has, say, realized
# trades or verdict history but (temporarily) zero active holdings still gets
# the backup-first treatment.
_USER_DATA_TABLES = (
    "holdings",
    "realized_trades",
    "verdict_snapshots",
    "portfolio_snapshots",
    "ai_summaries",
    "dca_plans",
    "dca_contributions",
)


@dataclass
class MigrationResult:
    """Outcome of :func:`apply_migrations_safely`, for logging and diagnostics."""

    ran_migration: bool
    backed_up: bool
    backup_path: str | None
    restored: bool
    previous_schema_version: int
    schema_version: int


class DuplicateActiveHoldingsError(RuntimeError):
    """Legacy active duplicates must be resolved before the v7 index is installed."""


def _duplicate_query(execute) -> list[tuple[int, str, int]]:
    table = execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='holdings'"
    ).fetchone()
    if table is None:
        return []
    return [
        (int(row[0]), str(row[1]), int(row[2]))
        for row in execute(
            "SELECT portfolio_id, UPPER(TRIM(ticker)), COUNT(*) "
            "FROM holdings WHERE is_active = 1 "
            "GROUP BY portfolio_id, UPPER(TRIM(ticker)) HAVING COUNT(*) > 1 "
            "ORDER BY portfolio_id, UPPER(TRIM(ticker))"
        ).fetchall()
    ]


def _active_holding_duplicates(engine: Engine) -> list[tuple[int, str, int]]:
    """Inspect legacy duplicates without creating app_meta, WAL, locks, or backups."""
    database = engine.url.database
    if database and database != ":memory:":
        path = Path(database).expanduser().resolve()
        if not path.is_file():
            return []
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        try:
            return _duplicate_query(connection.execute)
        finally:
            connection.close()

    with engine.connect() as connection:
        def execute(statement):
            return connection.exec_driver_sql(statement)

        return _duplicate_query(execute)


def preflight_active_holding_uniqueness(engine: Engine) -> None:
    """Stop before migration if active holdings need an explicit data decision."""
    if not str(engine.url).startswith("sqlite"):
        return
    _raise_for_active_holding_duplicates(_active_holding_duplicates(engine))


def _raise_for_active_holding_duplicates(
    conflicts: list[tuple[int, str, int]],
) -> None:
    """Raise the stable user-facing conflict message for a duplicate inventory."""
    if not conflicts:
        return
    summary = ", ".join(
        f"portfolio {portfolio_id} / {ticker} ({count} rows)"
        for portfolio_id, ticker, count in conflicts[:10]
    )
    if len(conflicts) > 10:
        summary += f", plus {len(conflicts) - 10} more"
    raise DuplicateActiveHoldingsError(
        "Active holding duplicates require an explicit data decision before "
        f"FolioOrb can migrate: {summary}. No portfolio rows or schema metadata "
        "were changed."
    )


def _ensure_app_meta(conn) -> None:
    conn.execute(
        text(f"CREATE TABLE IF NOT EXISTS {_META_TABLE} (key VARCHAR PRIMARY KEY, value VARCHAR)")
    )


def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name=:n"),
        {"n": name},
    ).fetchone()
    return row is not None


def _read_meta(conn, key: str, default: str | None = None) -> str | None:
    row = conn.execute(
        text(f"SELECT value FROM {_META_TABLE} WHERE key=:k"), {"k": key}
    ).fetchone()
    return row[0] if row else default


def _write_meta(conn, key: str, value: str) -> None:
    conn.execute(
        text(
            f"INSERT INTO {_META_TABLE}(key, value) VALUES(:k, :v) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        ),
        {"k": key, "v": value},
    )


def read_schema_version(engine: Engine) -> int:
    """Return the on-disk schema version, creating ``app_meta`` if absent (0 if unset)."""
    with engine.begin() as conn:
        _ensure_app_meta(conn)
        raw = _read_meta(conn, "schema_version", "0")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _file_sqlite_path(engine: Engine) -> Path | None:
    if not str(engine.url).startswith("sqlite"):
        return None
    database = engine.url.database
    if not database or database == ":memory:":
        return None
    return Path(database).expanduser().resolve()


def _active_holding_duplicates_on_connection(conn) -> list[tuple[int, str, int]]:
    """Repeat the duplicate preflight while the migration writer lock is held."""
    return _duplicate_query(conn.exec_driver_sql)


def apply_migrations_safely(engine: Engine) -> MigrationResult:
    """Run schema creation and migrations with backup-first / restore-on-failure.

    Steps:
      1. Take SQLite's cross-process writer lock and repeat the duplicate guard.
      2. Ensure ``app_meta`` and read the stored schema version.
      3. If the version is behind and the DB already holds data, take a verified
         backup first. Abort and roll back if that backup cannot be verified.
      4. Run ``create_all`` (additive: only ever creates missing tables) followed
         by ``ensure_startup_migrations`` (idempotent).
      5. On failure, roll back and restore the verified backup, then re-raise.
      6. On success, stamp metadata and commit before releasing the writer lock.
    """
    from app import models
    from app.database import ensure_startup_migrations
    from app.version import __version__

    # The v7 uniqueness migration must never guess which duplicate row to keep.
    # This inspection uses SQLite read-only mode for an existing file and runs
    # before app_meta, backup locks, schema writes, or the engine's WAL PRAGMAs.
    preflight_active_holding_uniqueness(engine)

    database_path = _file_sqlite_path(engine)
    backup_path = None
    backed_up = False
    restored = False
    connection = engine.connect()
    try:
        if str(engine.url).startswith("sqlite"):
            # BEGIN IMMEDIATE reserves the one SQLite writer slot before the
            # second preflight. It remains held across backup, schema work,
            # index creation, and the version stamp, so an older app process
            # cannot insert a conflicting row inside the migration window.
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            _raise_for_active_holding_duplicates(
                _active_holding_duplicates_on_connection(connection)
            )
        else:
            connection.begin()

        _ensure_app_meta(connection)
        stored = int(_read_meta(connection, "schema_version", "0") or 0)
        had_data = any(
            _table_exists(connection, name) for name in _USER_DATA_TABLES
        )
        needs_bump = stored < SCHEMA_VERSION

        if needs_bump and had_data and database_path is not None:
            try:
                from app import paths
                from app.services import backup_service

                point = backup_service.create_verified_backup(
                    label=f"pre-migrate-v{stored}",
                    source_db=database_path,
                    dest_dir=paths.data_dir() / backup_service.BACKUP_DIRNAME,
                )
                backup_path = point.database
                backed_up = True
            except Exception as exc:  # pylint: disable=broad-except
                logger.error(
                    "Could not create pre-migration backup: %s", type(exc).__name__
                )
                raise RuntimeError(
                    "Migration stopped: a verified pre-migration backup is required. "
                    "Resolve backup storage availability and retry startup."
                ) from exc

        models.Base.metadata.create_all(bind=connection)
        ensure_startup_migrations(connection)

        if needs_bump:
            _write_meta(connection, "schema_version", str(SCHEMA_VERSION))
            _write_meta(
                connection,
                "min_compatible_app_version",
                MIN_COMPATIBLE_APP_VERSION,
            )
        _write_meta(connection, "last_run_app_version", __version__)
        connection.commit()
    except Exception:
        connection.rollback()
        connection.close()
        if backup_path and backed_up and database_path is not None:
            from app.services import backup_service

            engine.dispose()
            restored = backup_service.restore_backup(backup_path, database_path)
            logger.error(
                "Migration failed; restored pre-migration backup %s", backup_path.name
            )
        raise
    finally:
        connection.close()

    return MigrationResult(
        ran_migration=needs_bump,
        backed_up=backed_up,
        backup_path=str(backup_path) if backup_path else None,
        restored=restored,
        previous_schema_version=stored,
        schema_version=SCHEMA_VERSION,
    )
