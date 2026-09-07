"""Portable archives freeze cross-table records while another SQLite session writes."""
import csv
import io
import json
from zipfile import ZipFile

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.models import Base, DcaContribution, DcaPlan, Holding, Portfolio, RealizedTrade
from app.services import portfolio_records


def _seed_records(engine):
    with Session(engine) as seed:
        seed.add(Portfolio(id=1, name="Before"))
        seed.add(Holding(id=1, portfolio_id=1, ticker="EXAMPLE", shares=10, avg_cost=100))
        seed.add(DcaPlan(id=1, portfolio_id=1, ticker="EXAMPLE", amount=100,
                         frequency="monthly", start_date="2026-01-01"))
        seed.add(DcaContribution(id=1, plan_id=1, scheduled_date="2026-01-01",
                                 exec_date="2026-01-01", amount=100, price=100,
                                 shares=1, status="pending"))
        seed.commit()

def test_archive_freezes_sales_dca_and_portfolios_before_concurrent_commit(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'archive.db'}")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
    Base.metadata.create_all(engine)
    _seed_records(engine)
    interleaved = []
    compressed = []
    with Session(engine, autoflush=False) as reader:
        def commit_changes(_connection, _cursor, statement, _parameters, _context, _many):
            if "FROM realized_trades" not in statement or interleaved:
                return
            interleaved.append(_connection.connection.driver_connection)
            with Session(engine) as writer:
                writer.get(Holding, 1).shares = 6  # sale of 5, followed by DCA buy of 1
                writer.add(RealizedTrade(
                    portfolio_id=1, ticker="EXAMPLE", shares_sold=5, sale_price=120,
                    avg_cost=100, realized_gain=100,
                ))
                writer.get(DcaContribution, 1).status = "applied"
                writer.get(DcaContribution, 1).applied_holding_id = 1
                writer.get(DcaPlan, 1).amount = 200
                writer.get(Portfolio, 1).name = "After"
                writer.add(Portfolio(id=2, name="New"))
                writer.commit()  # succeeds while the exporter holds a read snapshot

        original_write = getattr(portfolio_records, "_zip_write")

        def assert_snapshot_released(archive, name, data):
            assert not reader.in_transaction()
            assert not interleaved[0].in_transaction
            compressed.append(name)
            original_write(archive, name, data)

        event.listen(engine, "before_cursor_execute", commit_changes)
        monkeypatch.setattr(portfolio_records, "_zip_write", assert_snapshot_released)
        try:
            payload = portfolio_records.build_portable_archive(reader)
        finally:
            event.remove(engine, "before_cursor_execute", commit_changes)

    try:
        assert len(interleaved) == 1
        assert len(compressed) == 7
        with ZipFile(io.BytesIO(payload)) as archive:
            def rows(name):
                return list(csv.DictReader(io.StringIO(
                    archive.read(name).decode("utf-8-sig")
                )))

            assert rows("holdings.csv")[0]["shares"] == "10.0"
            assert not rows("realized_trades.csv")
            assert rows("dca_contributions.csv")[0]["status"] == "pending"
            assert rows("dca_contributions.csv")[0]["applied_holding_id"] == ""
            assert rows("dca_plans.csv")[0]["amount"] == "100.0"
            assert [row["name"] for row in rows("portfolios.csv")] == ["Before"]
            assert json.loads(archive.read("manifest.json"))["portfolio_ids"] == [1]
        with Session(engine) as current:
            assert current.get(Holding, 1).shares == 6
            assert current.query(RealizedTrade).count() == 1
            assert current.get(DcaContribution, 1).status == "applied"
            assert current.get(Portfolio, 2).name == "New"
    finally:
        engine.dispose()
