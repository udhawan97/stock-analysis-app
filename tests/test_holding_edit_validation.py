"""Holding edits reject nonfinite facts before the router can mutate records."""
import pytest
from pydantic import ValidationError

from app.models import Holding, PortfolioSnapshot, RealizedTrade
from app.routers import portfolio
from app.schemas import HoldingCreate, HoldingUpdate


@pytest.mark.parametrize("field", ["shares", "avg_cost"])
@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan"),
                                 "Infinity", "-Infinity", "NaN"])
def test_holding_create_and_edit_require_finite_facts(field, value):
    for schema in (HoldingCreate, HoldingUpdate):
        values = {"shares": 10, "avg_cost": 100, field: value}
        if schema is HoldingCreate:
            values["ticker"] = "EXAMPLE"
        with pytest.raises(ValidationError):
            schema(**values)


@pytest.mark.parametrize("field", ["shares", "avg_cost"])
@pytest.mark.parametrize("value", ["Infinity", "-Infinity", "NaN"])
def test_invalid_edit_preserves_holdings_trades_and_snapshots(
    db, api_client, field, value
):
    holding = Holding(portfolio_id=1, ticker="EXAMPLE", shares=10, avg_cost=100)
    db.add_all([
        holding,
        RealizedTrade(
            portfolio_id=1, ticker="EXAMPLE", shares_sold=1, sale_price=110,
            avg_cost=100, realized_gain=10,
        ),
        PortfolioSnapshot(
            portfolio_id=1, snapshot_date="2026-01-01", total_value=1100,
            total_cost_basis=1000, unrealized_gain=100, realized_gain=10, total_return=110,
        ),
    ])
    db.commit()
    holding_id = holding.id
    before = {
        model.__tablename__: [tuple(row) for row in db.execute(model.__table__.select())]
        for model in (Holding, RealizedTrade, PortfolioSnapshot)
    }
    client = api_client(portfolio.router)

    response = client.put(f"/api/portfolio/holdings/{holding_id}", json={field: value})

    assert response.status_code == 422
    db.expire_all()
    after = {
        model.__tablename__: [tuple(row) for row in db.execute(model.__table__.select())]
        for model in (Holding, RealizedTrade, PortfolioSnapshot)
    }
    assert after == before
    assert client.get("/api/portfolio/holdings").status_code == 200


def test_finite_edit_and_explicit_sale_remain_valid(db, api_client):
    holding = Holding(portfolio_id=1, ticker="EXAMPLE", shares=10, avg_cost=100)
    db.add(holding)
    db.commit()
    holding_id = holding.id
    client = api_client(portfolio.router)

    assert client.put(
        f"/api/portfolio/holdings/{holding_id}", json={"avg_cost": 110}
    ).status_code == 200
    assert client.put(
        f"/api/portfolio/holdings/{holding_id}",
        json={"shares": 5, "sale_price": 120, "sale_date": "2026-01-01"},
    ).status_code == 200

    db.expire_all()
    assert db.get(Holding, holding_id).shares == 5
    trade = db.query(RealizedTrade).one()
    assert (trade.shares_sold, trade.avg_cost, trade.realized_gain) == (5, 110, 50)
    assert client.get("/api/portfolio/holdings").status_code == 200
