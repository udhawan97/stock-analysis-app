"""Human-readable, local portfolio record exports.

The annual recap is an average-cost ledger aid, not a tax form. The portable
archive exports domain rows rather than SQLite internals and deliberately omits
AI caches, secrets, settings, and backup files.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import datetime, timezone
from decimal import Decimal, DecimalException, ROUND_HALF_UP
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from sqlalchemy.orm import Session

from app.models import (
    DcaContribution,
    DcaPlan,
    Holding,
    Portfolio,
    PortfolioSnapshot,
    RealizedTrade,
)
from app.schema_meta import SCHEMA_VERSION
from app.services import financial_currency
from app.services.holdings_csv import escape_csv_cell
from app.version import __version__

EXPORT_FORMAT_VERSION = 2
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MIN_RECAP_YEAR = 1900
RECAP_LIMITATION = (
    "Average-cost recap only; not a tax form. Excludes non-USD or ambiguous sales, "
    "lots, fees, holding periods, wash sales, and tax classification."
)


def _utc_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
        return aware.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value)


def _csv_bytes(columns: tuple[str, ...], rows: list[dict]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)

    def cell(value):
        return escape_csv_cell(value) if isinstance(value, str) else value

    for row in rows:
        writer.writerow([cell(row.get(column)) for column in columns])
    return ("\ufeff" + buffer.getvalue()).encode("utf-8")


def _stored_decimal(value) -> Decimal:
    """Reject corrupt stored financial facts instead of exporting NaN or infinity."""
    try:
        number = Decimal(str(value if value is not None else 0))
    except (DecimalException, TypeError, ValueError) as exc:
        raise ValueError("Stored sale facts must be finite numbers") from exc
    if not number.is_finite():
        raise ValueError("Stored sale facts must be finite numbers")
    return number


def _money(value) -> Decimal:
    return _stored_decimal(value).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def _decimal_value(value, places: int) -> Decimal:
    number = _stored_decimal(value)
    quantizer = Decimal(1).scaleb(-places)
    return number.quantize(quantizer, rounding=ROUND_HALF_UP)


def build_realized_recap_csv(
    db: Session,
    portfolio_id: int,
    year: int,
) -> bytes:
    """Return stored sale facts for one UTC calendar year plus reconciling totals."""
    current_year = datetime.now(timezone.utc).year
    if year < MIN_RECAP_YEAR or year > current_year:
        raise ValueError(f"Year must be between {MIN_RECAP_YEAR} and {current_year}")
    portfolio = db.query(Portfolio).filter(Portfolio.id == portfolio_id).first()
    if portfolio is None:
        raise ValueError("Portfolio not found")
    trades = (
        db.query(RealizedTrade)
        .filter(
            RealizedTrade.portfolio_id == portfolio_id,
            financial_currency.trusted_reporting_fact_clause(
                RealizedTrade.sale_currency,
                RealizedTrade.sale_price_source,
            ),
        )
        .order_by(RealizedTrade.created_at.asc(), RealizedTrade.id.asc())
        .all()
    )
    selected = [
        trade for trade in trades
        if trade.created_at is not None and trade.created_at.year == year
    ]
    rows = []
    total_proceeds = Decimal("0.00")
    total_basis = Decimal("0.00")
    total_gain = Decimal("0.00")
    for trade in selected:
        shares = _stored_decimal(trade.shares_sold)
        proceeds = _money(shares * _stored_decimal(trade.sale_price))
        basis = _money(shares * _stored_decimal(trade.avg_cost))
        gain = proceeds - basis
        total_proceeds += proceeds
        total_basis += basis
        total_gain += gain
        rows.append({
            "row_type": "trade",
            "portfolio_id": portfolio.id,
            "portfolio_name": portfolio.name,
            "year_utc": year,
            "sale_date_utc": trade.created_at.date().isoformat(),
            "ticker": trade.ticker,
            "shares_sold": _decimal_value(trade.shares_sold, 8),
            "sale_price_usd": _decimal_value(trade.sale_price, 4),
            "average_cost_usd": _decimal_value(trade.avg_cost, 4),
            "proceeds_usd": proceeds,
            "average_cost_basis_usd": basis,
            "realized_gain_usd": gain,
            "basis_method": "average_cost_recap",
            "limitations": RECAP_LIMITATION,
        })
    rows.append({
        "row_type": "total",
        "portfolio_id": portfolio.id,
        "portfolio_name": portfolio.name,
        "year_utc": year,
        "sale_date_utc": "",
        "ticker": "",
        "shares_sold": "",
        "sale_price_usd": "",
        "average_cost_usd": "",
        "proceeds_usd": total_proceeds,
        "average_cost_basis_usd": total_basis,
        "realized_gain_usd": total_gain,
        "basis_method": "average_cost_recap",
        "limitations": RECAP_LIMITATION,
    })
    columns = (
        "row_type", "portfolio_id", "portfolio_name", "year_utc",
        "sale_date_utc", "ticker", "shares_sold", "sale_price_usd",
        "average_cost_usd", "proceeds_usd", "average_cost_basis_usd",
        "realized_gain_usd", "basis_method", "limitations",
    )
    return _csv_bytes(columns, rows)


def _model_rows(db: Session) -> list[tuple[str, tuple[str, ...], list[dict]]]:
    portfolios = db.query(Portfolio).order_by(Portfolio.id.asc()).all()
    holdings = db.query(Holding).order_by(Holding.portfolio_id.asc(), Holding.id.asc()).all()
    trades = (
        db.query(RealizedTrade)
        .order_by(
            RealizedTrade.portfolio_id.asc(),
            RealizedTrade.created_at.asc(),
            RealizedTrade.id.asc(),
        )
        .all()
    )
    snapshots = (
        db.query(PortfolioSnapshot)
        .order_by(
            PortfolioSnapshot.portfolio_id.asc(),
            PortfolioSnapshot.snapshot_date.asc(),
            PortfolioSnapshot.id.asc(),
        )
        .all()
    )
    plans = db.query(DcaPlan).order_by(DcaPlan.portfolio_id.asc(), DcaPlan.id.asc()).all()
    plan_portfolios = {plan.id: plan.portfolio_id for plan in plans}
    contributions = (
        db.query(DcaContribution)
        .order_by(
            DcaContribution.plan_id.asc(),
            DcaContribution.scheduled_date.asc(),
            DcaContribution.id.asc(),
        )
        .all()
    )

    return [
        ("portfolios.csv", (
            "id", "name", "description", "created_at_utc", "updated_at_utc",
        ), [{
            "id": row.id, "name": row.name, "description": row.description,
            "created_at_utc": _utc_text(row.created_at),
            "updated_at_utc": _utc_text(row.updated_at),
        } for row in portfolios]),
        ("holdings.csv", (
            "id", "portfolio_id", "ticker", "company_name", "shares", "avg_cost",
            "is_active", "is_watchlist", "hold_class", "notes",
            "thesis_reviewed_at_utc", "thesis_review_interval_days",
            "target_weight_bps", "added_at_utc",
        ), [{
            "id": row.id, "portfolio_id": row.portfolio_id, "ticker": row.ticker,
            "company_name": row.company_name, "shares": row.shares, "avg_cost": row.avg_cost,
            "is_active": str(bool(row.is_active)).lower(),
            "is_watchlist": str(bool(row.is_watchlist)).lower(),
            "hold_class": row.hold_class, "notes": row.notes,
            "thesis_reviewed_at_utc": _utc_text(row.thesis_reviewed_at),
            "thesis_review_interval_days": row.thesis_review_interval_days,
            "target_weight_bps": row.target_weight_bps,
            "added_at_utc": _utc_text(row.added_at),
        } for row in holdings]),
        ("realized_trades.csv", (
            "id", "portfolio_id", "ticker", "shares_sold", "sale_price",
            "avg_cost", "realized_gain", "sale_currency", "sale_price_source",
            "created_at_utc",
        ), [{
            "id": row.id, "portfolio_id": row.portfolio_id, "ticker": row.ticker,
            "shares_sold": row.shares_sold, "sale_price": row.sale_price,
            "avg_cost": row.avg_cost, "realized_gain": row.realized_gain,
            "sale_currency": row.sale_currency,
            "sale_price_source": row.sale_price_source,
            "created_at_utc": _utc_text(row.created_at),
        } for row in trades]),
        ("portfolio_snapshots.csv", (
            "id", "portfolio_id", "snapshot_date", "total_value", "total_cost_basis",
            "unrealized_gain", "realized_gain", "total_return", "created_at_utc",
        ), [{
            "id": row.id, "portfolio_id": row.portfolio_id,
            "snapshot_date": row.snapshot_date, "total_value": row.total_value,
            "total_cost_basis": row.total_cost_basis,
            "unrealized_gain": row.unrealized_gain, "realized_gain": row.realized_gain,
            "total_return": row.total_return, "created_at_utc": _utc_text(row.created_at),
        } for row in snapshots]),
        ("dca_plans.csv", (
            "id", "portfolio_id", "ticker", "amount", "frequency", "start_date",
            "quote_currency", "quote_currency_source", "is_active",
            "catchup_floor", "created_at_utc",
        ), [{
            "id": row.id, "portfolio_id": row.portfolio_id, "ticker": row.ticker,
            "amount": row.amount, "frequency": row.frequency, "start_date": row.start_date,
            "quote_currency": row.quote_currency,
            "quote_currency_source": row.quote_currency_source,
            "is_active": str(bool(row.is_active)).lower(), "catchup_floor": row.catchup_floor,
            "created_at_utc": _utc_text(row.created_at),
        } for row in plans]),
        ("dca_contributions.csv", (
            "id", "portfolio_id", "plan_id", "scheduled_date", "exec_date", "price",
            "shares", "amount", "price_currency", "price_currency_source", "status",
            "applied_holding_id", "created_at_utc",
        ), [{
            "id": row.id, "portfolio_id": plan_portfolios.get(row.plan_id),
            "plan_id": row.plan_id, "scheduled_date": row.scheduled_date,
            "exec_date": row.exec_date, "price": row.price, "shares": row.shares,
            "amount": row.amount, "price_currency": row.price_currency,
            "price_currency_source": row.price_currency_source, "status": row.status,
            "applied_holding_id": row.applied_holding_id,
            "created_at_utc": _utc_text(row.created_at),
        } for row in contributions]),
    ]


def _zip_write(archive: ZipFile, name: str, data: bytes) -> None:
    info = ZipInfo(filename=name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    archive.writestr(info, data)


def build_portable_archive(db: Session) -> bytes:
    """Build a bounded, deterministically ordered ZIP from one read transaction."""
    if db.in_transaction():
        raise ValueError("Portable export requires a fresh database read session")
    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    members: list[tuple[str, bytes, int]] = []
    uncompressed = 0
    with db.begin():
        connection = db.connection()
        if connection.dialect.name == "sqlite":
            # sqlite3 legacy mode does not begin a transaction for SELECT.
            # A deferred BEGIN freezes all datasets at the first read without
            # reserving the writer slot; the transaction ends before ZIP work.
            if not connection.connection.driver_connection.in_transaction:
                connection.exec_driver_sql("BEGIN")
        datasets = _model_rows(db)
        for name, columns, rows in datasets:
            payload = _csv_bytes(columns, rows)
            uncompressed += len(payload)
            if uncompressed > MAX_ARCHIVE_BYTES:
                raise ValueError("Portable export exceeds the 64 MiB safety limit")
            members.append((name, payload, len(rows)))
        portfolio_ids = [row[0] for row in db.query(Portfolio.id).order_by(Portfolio.id).all()]

    files = [{
        "name": name,
        "rows": row_count,
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    } for name, payload, row_count in members]
    member_order = [name for name, _payload, _row_count in members] + ["manifest.json"]
    manifest = {
        "format_version": EXPORT_FORMAT_VERSION,
        "app_version": __version__,
        "database_schema_version": SCHEMA_VERSION,
        "generated_at_utc": generated_at,
        "portfolio_ids": portfolio_ids,
        "files": files,
        "member_order": member_order,
        "row_count_semantics": "CSV data rows; header excluded.",
        "manifest_included_in_files": False,
        "manifest_exclusion_reason": (
            "manifest.json is excluded from files and checksums to avoid self-reference."
        ),
        "omitted": [
            "AI summaries and verdict caches",
            "application settings and update metadata",
            "API keys and .env files",
            "database backups and SQLite internals",
        ],
        "warnings": [
            "This ZIP contains sensitive human-readable portfolio records.",
            "It is an export for inspection and portability, not a FolioOrb restore file.",
        ],
    }
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if uncompressed + len(manifest_bytes) > MAX_ARCHIVE_BYTES:
        raise ValueError("Portable export exceeds the 64 MiB safety limit")

    buffer = io.BytesIO()
    with ZipFile(buffer, mode="w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for name, payload, _row_count in members:
            if buffer.tell() > MAX_ARCHIVE_BYTES:
                raise ValueError("Portable export exceeds the 64 MiB safety limit")
            _zip_write(archive, name, payload)
            if buffer.tell() > MAX_ARCHIVE_BYTES:
                raise ValueError("Portable export exceeds the 64 MiB safety limit")
        if buffer.tell() > MAX_ARCHIVE_BYTES:
            raise ValueError("Portable export exceeds the 64 MiB safety limit")
        _zip_write(archive, "manifest.json", manifest_bytes)
        if buffer.tell() > MAX_ARCHIVE_BYTES:
            raise ValueError("Portable export exceeds the 64 MiB safety limit")
    payload = buffer.getvalue()
    if len(payload) > MAX_ARCHIVE_BYTES:
        raise ValueError("Portable export exceeds the 64 MiB safety limit")
    return payload
