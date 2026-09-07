# pylint: disable=protected-access,redefined-outer-name,unused-argument,unnecessary-lambda
"""Protected migration sequence: version stamping, backup-first, restore-on-fail.

Uses real on-disk SQLite files so the backup/restore paths (which operate on the
live database file) are exercised end to end. The configured DATABASE_URL and the
backup service's data directory are redirected to a temp location per test.
"""
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from app import paths, schema_meta
from app.config import settings
from app.services import backup_service


@pytest.fixture
def file_db(tmp_path, monkeypatch):
    """A file-backed SQLite engine wired into config + backup paths."""
    db_path = tmp_path / "portfolio.db"
    url = f"sqlite:///{db_path.as_posix()}"
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    engine = create_engine(url, connect_args={"check_same_thread": False})
    try:
        yield engine, db_path
    finally:
        engine.dispose()


def test_read_schema_version_defaults_to_zero(file_db):
    engine, _ = file_db
    assert schema_meta.read_schema_version(engine) == 0


def test_fresh_db_stamps_version_without_backup(file_db):
    engine, _ = file_db
    result = schema_meta.apply_migrations_safely(engine)

    assert result.schema_version == schema_meta.SCHEMA_VERSION
    assert result.previous_schema_version == 0
    assert result.backed_up is False  # nothing to lose on a fresh DB
    assert schema_meta.read_schema_version(engine) == schema_meta.SCHEMA_VERSION
    # No backups created for a first-run empty database.
    assert not list((backup_service.backups_dir()).glob("*.db"))


def test_existing_data_is_backed_up_and_preserved(file_db):
    engine, _ = file_db
    # Seed a realistic holdings table with data, and force "needs migration".
    from app import models

    models.Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO portfolios (name) VALUES ('My Portfolio')"))
        conn.execute(
            text(
                "INSERT INTO holdings (portfolio_id, ticker, shares, avg_cost, is_active) "
                "VALUES (1, 'VOO', 10, 400, 1)"
            )
        )
        schema_meta._ensure_app_meta(conn)
        schema_meta._write_meta(conn, "schema_version", "0")

    result = schema_meta.apply_migrations_safely(engine)

    assert result.ran_migration is True
    assert result.backed_up is True
    assert result.backup_path is not None
    # Holding survived the migration.
    with engine.begin() as conn:
        tickers = [r[0] for r in conn.execute(text("SELECT ticker FROM holdings"))]
    assert tickers == ["VOO"]
    # A verified pre-migration backup exists and contains the holding.
    backups = list(backup_service.backups_dir().glob("pre-migrate-*.db"))
    assert len(backups) == 1
    assert backup_service.verify_backup(backups[0], expected_min_holdings=1)


def test_v5_to_v6_adds_nullable_targets_idempotently_and_preserves_rows(file_db):
    engine, _ = file_db
    from app import models

    models.Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO portfolios (name) VALUES ('Targets')"))
        conn.execute(
            text(
                "INSERT INTO holdings (portfolio_id, ticker, shares, avg_cost, is_active) "
                "VALUES (1, 'AAPL', 2, 100, 1)"
            )
        )
        conn.execute(text("ALTER TABLE holdings DROP COLUMN target_weight_bps"))
        schema_meta._ensure_app_meta(conn)
        schema_meta._write_meta(conn, "schema_version", "5")

    first = schema_meta.apply_migrations_safely(engine)
    second = schema_meta.apply_migrations_safely(engine)

    assert first.previous_schema_version == 5
    assert first.schema_version == schema_meta.SCHEMA_VERSION
    assert first.backed_up is True
    assert second.ran_migration is False
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT ticker, shares, avg_cost, target_weight_bps FROM holdings")
        ).one()
    assert tuple(row) == ("AAPL", 2.0, 100.0, None)


def test_v7_to_current_backs_up_then_marks_legacy_sale_currency_ambiguous(file_db):
    engine, _ = file_db
    from app import models

    models.Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE realized_trades DROP COLUMN sale_price_source"))
        conn.execute(text("ALTER TABLE realized_trades DROP COLUMN sale_currency"))
        conn.execute(text("INSERT INTO portfolios (name) VALUES ('Legacy')"))
        conn.execute(
            text(
                "INSERT INTO realized_trades "
                "(portfolio_id, ticker, shares_sold, sale_price, avg_cost, realized_gain) "
                "VALUES (1, 'VOD.L', 2, 250, 200, 100)"
            )
        )
        schema_meta._ensure_app_meta(conn)
        schema_meta._write_meta(conn, "schema_version", "7")

    first = schema_meta.apply_migrations_safely(engine)
    second = schema_meta.apply_migrations_safely(engine)

    assert first.previous_schema_version == 7
    assert first.schema_version == schema_meta.SCHEMA_VERSION
    assert first.backed_up is True
    assert second.ran_migration is False
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT ticker, shares_sold, realized_gain, sale_currency, "
                "sale_price_source FROM realized_trades"
            )
        ).one()
    assert tuple(row) == ("VOD.L", 2.0, 100.0, None, "legacy_unknown")

    backup = sqlite3.connect(first.backup_path)
    try:
        backup_columns = {
            item[1] for item in backup.execute("PRAGMA table_info(realized_trades)")
        }
        backup_row = backup.execute(
            "SELECT ticker, shares_sold, realized_gain FROM realized_trades"
        ).fetchone()
    finally:
        backup.close()
    assert "sale_currency" not in backup_columns
    assert "sale_price_source" not in backup_columns
    assert backup_row == ("VOD.L", 2.0, 100.0)


def test_v8_to_v9_backs_up_then_marks_legacy_dca_currency_ambiguous(file_db):
    engine, _ = file_db
    from app import models

    models.Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE dca_contributions DROP COLUMN price_currency_source"))
        conn.execute(text("ALTER TABLE dca_contributions DROP COLUMN price_currency"))
        conn.execute(text("ALTER TABLE dca_plans DROP COLUMN quote_currency_source"))
        conn.execute(text("ALTER TABLE dca_plans DROP COLUMN quote_currency"))
        conn.execute(text("INSERT INTO portfolios (name) VALUES ('Legacy DCA')"))
        conn.execute(text(
            "INSERT INTO dca_plans "
            "(portfolio_id, ticker, amount, frequency, start_date, is_active) "
            "VALUES (1, 'VOD.L', 50, 'weekly', '2026-06-05', 1)"
        ))
        conn.execute(text(
            "INSERT INTO dca_contributions "
            "(plan_id, scheduled_date, exec_date, price, shares, amount, status) "
            "VALUES (1, '2026-06-05', '2026-06-05', 250, 0.2, 50, 'pending')"
        ))
        schema_meta._ensure_app_meta(conn)
        schema_meta._write_meta(conn, "schema_version", "8")

    first = schema_meta.apply_migrations_safely(engine)
    second = schema_meta.apply_migrations_safely(engine)

    assert first.previous_schema_version == 8
    assert first.schema_version == schema_meta.SCHEMA_VERSION
    assert first.backed_up is True
    assert second.ran_migration is False
    with engine.begin() as conn:
        plan = conn.execute(text(
            "SELECT ticker, quote_currency, quote_currency_source FROM dca_plans"
        )).one()
        contribution = conn.execute(text(
            "SELECT price, price_currency, price_currency_source "
            "FROM dca_contributions"
        )).one()
    assert tuple(plan) == ("VOD.L", None, "legacy_unknown")
    assert tuple(contribution) == (250.0, None, "legacy_unknown")

    backup = sqlite3.connect(first.backup_path)
    try:
        plan_columns = {
            item[1] for item in backup.execute("PRAGMA table_info(dca_plans)")
        }
        contribution_columns = {
            item[1]
            for item in backup.execute("PRAGMA table_info(dca_contributions)")
        }
        plan_row = backup.execute(
            "SELECT ticker, amount, frequency, start_date FROM dca_plans"
        ).fetchone()
        contribution_row = backup.execute(
            "SELECT price, shares, amount, status FROM dca_contributions"
        ).fetchone()
    finally:
        backup.close()
    assert "quote_currency" not in plan_columns
    assert "quote_currency_source" not in plan_columns
    assert "price_currency" not in contribution_columns
    assert "price_currency_source" not in contribution_columns
    assert plan_row == ("VOD.L", 50.0, "weekly", "2026-06-05")
    assert contribution_row == (250.0, 0.2, 50.0, "pending")


def test_backup_rejects_result_that_lost_holdings(file_db, monkeypatch):
    """Regression: verification must use the DB's real holdings count, not 0.

    A hardcoded expected_min_holdings=0 would accept a "backup" that silently
    ended up with zero rows even though the live DB has one — this proves the
    fix (computing the real pre-backup count) catches that and skips the
    otherwise-unsafe migration path.
    """
    engine, _ = file_db
    from app import models

    models.Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO portfolios (name) VALUES ('P')"))
        conn.execute(
            text(
                "INSERT INTO holdings (portfolio_id, ticker, shares, avg_cost, is_active) "
                "VALUES (1, 'AAPL', 1, 1, 1)"
            )
        )
        schema_meta._ensure_app_meta(conn)
        schema_meta._write_meta(conn, "schema_version", "0")

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

    with pytest.raises(RuntimeError, match="verified pre-migration backup"):
        schema_meta.apply_migrations_safely(engine)
    assert schema_meta.read_schema_version(engine) == 0
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM holdings").scalar_one() == 1


def test_had_data_detects_non_holdings_user_tables(file_db):
    """A DB with only verdict_snapshots (no holdings rows yet) still backs up first."""
    engine, _ = file_db
    from app import models

    models.Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO verdict_snapshots (ticker, action, confidence, hold_class) "
                "VALUES ('VOO', 'hold', 80, 'auto')"
            )
        )
        schema_meta._ensure_app_meta(conn)
        schema_meta._write_meta(conn, "schema_version", "0")

    result = schema_meta.apply_migrations_safely(engine)

    assert result.backed_up is True
    assert result.backup_path is not None


def test_failed_migration_restores_backup(file_db, monkeypatch):
    engine, db_path = file_db
    from app import models

    models.Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO portfolios (name) VALUES ('P')"))
        conn.execute(
            text(
                "INSERT INTO holdings (portfolio_id, ticker, shares, avg_cost, is_active) "
                "VALUES (1, 'MSFT', 5, 100, 1)"
            )
        )
        schema_meta._ensure_app_meta(conn)
        schema_meta._write_meta(conn, "schema_version", "0")

    def _boom(target_engine=None):
        # Corrupt the live DB, then fail — the restore must undo this.
        target_engine.execute(text("DELETE FROM holdings"))
        raise RuntimeError("migration exploded")

    monkeypatch.setattr("app.database.ensure_startup_migrations", _boom)

    with pytest.raises(RuntimeError):
        schema_meta.apply_migrations_safely(engine)

    # After restore, the holding is back and a .failed-* file was preserved.
    check = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        with check.begin() as conn:
            tickers = [r[0] for r in conn.execute(text("SELECT ticker FROM holdings"))]
    finally:
        check.dispose()
    assert tickers == ["MSFT"]
    assert list(db_path.parent.glob("portfolio.db.failed-*"))


def test_migration_writer_lock_covers_backup_schema_index_and_stamp(
    tmp_path, monkeypatch
):
    """A separate older process cannot write inside the v6-to-v7 window."""
    db_path = tmp_path / "portfolio.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        "CREATE TABLE holdings ("
        "id INTEGER PRIMARY KEY, portfolio_id INTEGER NOT NULL, "
        "ticker VARCHAR(10) NOT NULL, is_active BOOLEAN);"
        "CREATE TABLE app_meta (key VARCHAR PRIMARY KEY, value VARCHAR);"
        "INSERT INTO app_meta(key, value) VALUES('schema_version', '6');"
        "INSERT INTO holdings(portfolio_id, ticker, is_active) VALUES (1, 'AAPL', 1);"
    )
    connection.commit()
    connection.close()
    url = f"sqlite:///{db_path.as_posix()}"
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    engine = create_engine(url, connect_args={"check_same_thread": False})
    real_backup = backup_service.create_verified_backup
    writer_result = {}

    def backup_with_competing_process(*args, **kwargs):
        script = (
            "import sqlite3,sys\n"
            "db=sqlite3.connect(sys.argv[1], timeout=0.1)\n"
            "db.execute(\"INSERT INTO holdings "
            "(portfolio_id,ticker,is_active) VALUES (1,'MSFT',1)\")\n"
            "db.commit()\n"
        )
        attempt = subprocess.run(
            [sys.executable, "-c", script, str(db_path)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        writer_result["returncode"] = attempt.returncode
        writer_result["stderr"] = attempt.stderr
        return real_backup(*args, **kwargs)

    monkeypatch.setattr(
        backup_service, "create_verified_backup", backup_with_competing_process
    )
    try:
        result = schema_meta.apply_migrations_safely(engine)
        assert result.schema_version == schema_meta.SCHEMA_VERSION
        assert writer_result["returncode"] != 0
        assert "locked" in writer_result["stderr"].lower()
        with engine.connect() as check:
            assert check.execute(text("SELECT COUNT(*) FROM holdings")).scalar_one() == 1
            assert check.execute(
                text(
                    "SELECT 1 FROM sqlite_master WHERE type='index' "
                    "AND name='ux_holdings_active_portfolio_ticker'"
                )
            ).scalar_one() == 1
    finally:
        engine.dispose()


def test_active_duplicate_preflight_is_read_only_and_stops_migration(tmp_path, monkeypatch):
    """Legacy duplicates require a user decision before any migration side effect."""
    db_path = tmp_path / "portfolio.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        "CREATE TABLE holdings ("
        "id INTEGER PRIMARY KEY, portfolio_id INTEGER NOT NULL, "
        "ticker VARCHAR(10) NOT NULL, is_active BOOLEAN);"
        "INSERT INTO holdings(portfolio_id, ticker, is_active) VALUES (1, 'AAPL', 1);"
        "INSERT INTO holdings(portfolio_id, ticker, is_active) VALUES (1, 'aapl', 1);"
    )
    connection.commit()
    connection.close()
    baseline = db_path.read_bytes()
    url = f"sqlite:///{db_path.as_posix()}"
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    engine = create_engine(url, connect_args={"check_same_thread": False})

    try:
        with pytest.raises(
            schema_meta.DuplicateActiveHoldingsError, match="explicit data decision"
        ):
            schema_meta.apply_migrations_safely(engine)
    finally:
        engine.dispose()

    assert db_path.read_bytes() == baseline
    assert not (tmp_path / "backups").exists()
    check = sqlite3.connect(db_path)
    try:
        objects = {
            row[0]
            for row in check.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
            )
        }
        rows = check.execute(
            "SELECT ticker, is_active FROM holdings ORDER BY id"
        ).fetchall()
    finally:
        check.close()
    assert "app_meta" not in objects
    assert "ux_holdings_active_portfolio_ticker" not in objects
    assert rows == [("AAPL", 1), ("aapl", 1)]


def test_active_duplicate_preflight_preserves_wal_database_payload(tmp_path, monkeypatch):
    """SQLite may touch SHM bookkeeping, but DB/WAL payload and schema stay intact."""
    db_path = tmp_path / "portfolio.db"
    writer = sqlite3.connect(db_path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.executescript(
        "CREATE TABLE holdings ("
        "id INTEGER PRIMARY KEY, portfolio_id INTEGER NOT NULL, "
        "ticker VARCHAR(10) NOT NULL, is_active BOOLEAN);"
        "INSERT INTO holdings(portfolio_id, ticker, is_active) VALUES (1, 'MSFT', 1);"
        "INSERT INTO holdings(portfolio_id, ticker, is_active) VALUES (1, ' msft ', 1);"
    )
    writer.commit()
    wal_path = Path(f"{db_path}-wal")
    assert wal_path.exists()
    primary_before = db_path.read_bytes()
    wal_before = wal_path.read_bytes()
    url = f"sqlite:///{db_path.as_posix()}"
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    engine = create_engine(url, connect_args={"check_same_thread": False})

    try:
        with pytest.raises(schema_meta.DuplicateActiveHoldingsError):
            schema_meta.apply_migrations_safely(engine)

        assert db_path.read_bytes() == primary_before
        assert wal_path.read_bytes() == wal_before
        objects = {
            row[0]
            for row in writer.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
            )
        }
        rows = writer.execute(
            "SELECT ticker, is_active FROM holdings ORDER BY id"
        ).fetchall()
        assert "app_meta" not in objects
        assert "ux_holdings_active_portfolio_ticker" not in objects
        assert rows == [("MSFT", 1), (" msft ", 1)]
    finally:
        engine.dispose()
        writer.close()


def test_v6_to_v7_adds_active_holding_uniqueness_without_deduping(tmp_path, monkeypatch):
    db_path = tmp_path / "portfolio.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        "CREATE TABLE holdings ("
        "id INTEGER PRIMARY KEY, portfolio_id INTEGER NOT NULL, "
        "ticker VARCHAR(10) NOT NULL, is_active BOOLEAN);"
        "CREATE TABLE app_meta (key VARCHAR PRIMARY KEY, value VARCHAR);"
        "INSERT INTO app_meta(key, value) VALUES('schema_version', '6');"
        "INSERT INTO holdings(portfolio_id, ticker, is_active) VALUES (1, 'AAPL', 1);"
        "INSERT INTO holdings(portfolio_id, ticker, is_active) VALUES (1, 'AAPL', 0);"
    )
    connection.commit()
    connection.close()
    url = f"sqlite:///{db_path.as_posix()}"
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    engine = create_engine(url, connect_args={"check_same_thread": False})

    try:
        result = schema_meta.apply_migrations_safely(engine)
        assert result.previous_schema_version == 6
        assert result.schema_version == schema_meta.SCHEMA_VERSION
        with engine.begin() as connection:
            index_sql = connection.execute(
                text(
                    "SELECT sql FROM sqlite_master WHERE type='index' "
                    "AND name='ux_holdings_active_portfolio_ticker'"
                )
            ).scalar_one()
            assert "WHERE is_active = 1" in index_sql
            assert "UPPER(TRIM(ticker))" in index_sql
            connection.execute(
                text(
                    "INSERT INTO holdings(portfolio_id, ticker, is_active) "
                    "VALUES (1, 'AAPL', 0)"
                )
            )
            with pytest.raises(IntegrityError, match="UNIQUE constraint failed"):
                connection.execute(
                    text(
                        "INSERT INTO holdings(portfolio_id, ticker, is_active) "
                        "VALUES (1, 'AAPL', 1)"
                    )
                )
            with pytest.raises(IntegrityError, match="UNIQUE constraint failed"):
                connection.execute(
                    text(
                        "INSERT INTO holdings(portfolio_id, ticker, is_active) "
                        "VALUES (1, ' aapl ', 1)"
                    )
                )
    finally:
        engine.dispose()


@pytest.mark.parametrize("stored_version", [None, "5"])
def test_backup_failure_rolls_back_schema_and_rows_then_retry_succeeds(
    file_db, monkeypatch, stored_version
):
    engine, _ = file_db
    from app import models

    models.Base.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO portfolios (name) VALUES ('Preserved')"))
        connection.execute(text(
            "INSERT INTO holdings (portfolio_id, ticker, shares, avg_cost, is_active) "
            "VALUES (1, 'EXAMPLE', 10, 100, 1)"
        ))
        connection.execute(text("ALTER TABLE holdings DROP COLUMN target_weight_bps"))
        if stored_version is not None:
            schema_meta._ensure_app_meta(connection)
            schema_meta._write_meta(connection, "schema_version", stored_version)

    def state():
        with engine.connect() as connection:
            schema = connection.exec_driver_sql(
                "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
            ).fetchall()
            holdings = connection.exec_driver_sql("SELECT * FROM holdings").fetchall()
            metadata = (
                connection.exec_driver_sql("SELECT * FROM app_meta ORDER BY key").fetchall()
                if stored_version is not None else []
            )
            return schema, holdings, metadata

    before = state()
    real_backup = backup_service.create_verified_backup

    def unavailable_backup(**_kwargs):
        raise OSError("Synthetic backup storage unavailable")

    monkeypatch.setattr(backup_service, "create_verified_backup", unavailable_backup)
    with pytest.raises(RuntimeError, match="verified pre-migration backup"):
        schema_meta.apply_migrations_safely(engine)
    assert state() == before

    monkeypatch.setattr(backup_service, "create_verified_backup", real_backup)
    result = schema_meta.apply_migrations_safely(engine)
    assert result.backed_up is True
    assert result.schema_version == schema_meta.SCHEMA_VERSION
    with engine.connect() as connection:
        row = connection.exec_driver_sql(
            "SELECT ticker, shares, avg_cost, target_weight_bps FROM holdings"
        ).one()
        assert tuple(row) == ("EXAMPLE", 10, 100, None)
