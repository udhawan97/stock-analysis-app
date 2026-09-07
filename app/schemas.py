from typing import Literal, Optional
from datetime import date
from pydantic import BaseModel, Field, PositiveInt, field_validator, model_validator

from app.services.ticker import validated_ticker_shape


# Pydantic schemas define the shape of data coming IN (requests).
# They are separate from the SQLAlchemy models in models.py.
#
# - "Create" schemas = data the client sends to us (request body)
# - "Update" schemas = data the client sends to modify an existing record
#
# Responses are shaped by the routers as plain dicts; this module deliberately
# holds no response models, having previously carried two that nothing used.
#
# Pydantic validates data automatically — e.g. it rejects negative share counts
# before the request even reaches our route function.


# ── Holding Schemas ────────────────────────────────────────────────────

_HOLD_CLASS_VALUES = {"auto", "anchor", "trade", "core"}


def _normalize_hold_class(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    hold_class = str(value).strip().lower()
    if hold_class not in _HOLD_CLASS_VALUES:
        raise ValueError("hold_class must be one of: auto, anchor, trade, core")
    return hold_class

class HoldingCreate(BaseModel):
    """Data required to add a new holding."""
    ticker: str = Field(..., min_length=1, max_length=10,
                        description="Stock ticker symbol, e.g. VOO")
    shares: Optional[float] = Field(
        default=0.0, ge=0, allow_inf_nan=False,
        description="Number of shares (must be > 0 for positions)",
    )
    avg_cost: Optional[float] = Field(
        None, gt=0, allow_inf_nan=False, description="Average purchase price per share"
    )
    notes: Optional[str] = Field(None, max_length=500)
    thesis_review_interval_days: Optional[int] = Field(None, ge=7, le=730)
    is_watchlist: Optional[bool] = False  # True = research-only, excluded from P&L
    hold_class: Optional[str] = Field(default="auto")

    @field_validator("ticker")
    @classmethod
    def uppercase_ticker(cls, v):
        # Normalize the ticker so "voo", "VOO", and " Voo " all become "VOO"
        return validated_ticker_shape(v)

    @field_validator("hold_class")
    @classmethod
    def valid_hold_class(cls, v):
        return _normalize_hold_class(v) or "auto"

    @model_validator(mode="after")
    def shares_required_for_positions(self):
        if not self.is_watchlist and (self.shares is None or self.shares <= 0):
            raise ValueError("shares must be greater than 0 unless research mode is on")
        return self


class HoldingUpdate(BaseModel):
    """Fields that can be changed on an existing holding. All fields are optional."""
    shares: Optional[float] = Field(None, gt=0, allow_inf_nan=False)
    avg_cost: Optional[float] = Field(None, gt=0, allow_inf_nan=False)
    notes: Optional[str] = Field(None, max_length=500)
    thesis_review_interval_days: Optional[int] = Field(None, ge=7, le=730)
    mark_thesis_reviewed: Optional[bool] = None
    is_active: Optional[bool] = None
    is_watchlist: Optional[bool] = None  # Toggle research mode without affecting P&L
    hold_class: Optional[str] = None
    # When a share reduction records a realized sale, the client may supply the
    # actual sale price and date (e.g. a sale made last month). Both optional;
    # absent → live price and today's date, preserving the old behavior.
    sale_price: Optional[float] = Field(None, gt=0, allow_inf_nan=False)
    sale_date: Optional[str] = None

    @field_validator("hold_class")
    @classmethod
    def valid_hold_class(cls, v):
        return _normalize_hold_class(v)

    @field_validator("sale_date")
    @classmethod
    def valid_sale_date(cls, v):
        if v is None:
            return None
        try:
            parsed = date.fromisoformat(str(v).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError("sale_date must be an ISO date, e.g. 2026-01-15") from exc
        if parsed > date.today():
            raise ValueError("sale_date cannot be in the future")
        return parsed.isoformat()


class HoldingRemoval(BaseModel):
    """Optional facts supplied when removing an owned position.

    An omitted price asks FolioOrb to use a current, explicitly USD market
    quote. A supplied price is always an actual user-entered USD sale fact and
    must carry that provenance as one indivisible request contract.
    """

    sale_price: Optional[float] = Field(None, gt=0, allow_inf_nan=False)
    sale_currency: Optional[Literal["USD"]] = None
    sale_price_source: Optional[Literal["manual_entry"]] = None
    sale_date: Optional[str] = None

    @model_validator(mode="after")
    def explicit_sale_fields_move_together(self):
        explicit_fields = (self.sale_currency, self.sale_price_source)
        if self.sale_price is None and any(value is not None for value in explicit_fields):
            raise ValueError("sale currency and source require a sale price")
        if self.sale_price is not None and explicit_fields != ("USD", "manual_entry"):
            raise ValueError(
                "an explicit sale price requires USD currency and manual_entry source"
            )
        return self

    @field_validator("sale_date")
    @classmethod
    def valid_sale_date(cls, v):
        if v is None:
            return None
        try:
            parsed = date.fromisoformat(str(v).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError("sale_date must be an ISO date, e.g. 2026-01-15") from exc
        if parsed > date.today():
            raise ValueError("sale_date cannot be in the future")
        return parsed.isoformat()


class RealizedTradeUpdate(BaseModel):
    """Corrections to an already-recorded sale. All fields are optional.

    ``realized_gain`` is deliberately absent: it is derived from the other three
    and is recomputed on every edit, so there is no way to store a gain that
    disagrees with the numbers it is supposed to come from.
    """
    shares_sold: Optional[float] = Field(None, gt=0, allow_inf_nan=False)
    sale_price: Optional[float] = Field(None, gt=0, allow_inf_nan=False)
    avg_cost: Optional[float] = Field(None, gt=0, allow_inf_nan=False)
    sale_date: Optional[str] = None

    @field_validator("sale_date")
    @classmethod
    def valid_sale_date(cls, v):
        if v is None:
            return None
        try:
            parsed = date.fromisoformat(str(v).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError("sale_date must be an ISO date, e.g. 2026-01-15") from exc
        if parsed > date.today():
            raise ValueError("sale_date cannot be in the future")
        return parsed.isoformat()


# ── Portfolio Schemas ──────────────────────────────────────────────────

class PortfolioCreate(BaseModel):
    """Data required to create a new portfolio."""
    name: str = Field(default="My Portfolio", max_length=100)
    description: Optional[str] = None


# ── DCA (dollar-cost-averaging) Schemas ─────────────────────────────────

_DCA_FREQUENCIES = {"daily", "weekly", "monthly"}


class DcaPlanCreate(BaseModel):
    """Data required to create a recurring DCA plan."""
    ticker: str = Field(..., min_length=1, max_length=10)
    amount: float = Field(
        ..., gt=0, allow_inf_nan=False, description="Dollars invested per interval"
    )
    frequency: str = Field(..., description="daily, weekly, or monthly")
    start_date: str = Field(..., description="First buy date, ISO 'YYYY-MM-DD'")

    @field_validator("ticker")
    @classmethod
    def uppercase_ticker(cls, v):
        return validated_ticker_shape(v)

    @field_validator("frequency")
    @classmethod
    def valid_frequency(cls, v):
        freq = str(v).strip().lower()
        if freq not in _DCA_FREQUENCIES:
            raise ValueError("frequency must be one of: daily, weekly, monthly")
        return freq

    @field_validator("start_date")
    @classmethod
    def valid_start_date(cls, v):
        try:
            parsed = date.fromisoformat(str(v).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError("start_date must be an ISO date, e.g. 2026-01-15") from exc
        if parsed > date.today():
            raise ValueError("start_date cannot be in the future")
        return parsed.isoformat()


class DcaPlanUpdate(BaseModel):
    """Fields that can be changed on an existing DCA plan."""
    amount: Optional[float] = Field(None, gt=0, allow_inf_nan=False)
    is_active: Optional[bool] = None  # False = pause the plan


class DcaBulkAction(BaseModel):
    """The exact buys the user reviewed and consented to mutate together."""

    contribution_ids: list[PositiveInt] = Field(..., min_length=1, max_length=5000)

    @field_validator("contribution_ids")
    @classmethod
    def unique_contribution_ids(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("contribution_ids must be unique")
        return value
