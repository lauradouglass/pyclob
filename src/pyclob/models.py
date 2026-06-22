"""Core data models for the pyclob matching engine."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class Side(Enum):
    """Order side. BUY = long YES, SELL = short YES (long NO)."""
    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self) -> Side:
        return Side.SELL if self == Side.BUY else Side.BUY


class OrderStatus(Enum):
    OPEN = "OPEN"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"


@dataclass
class Order:
    """A limit order on a binary event contract.

    Prices are in integer cents (1–99) representing implied probability.
    A BUY at 40 means "I'll pay 40¢ for a contract worth $1 if YES."
    A SELL at 40 means "I'll sell YES at 40¢" (= buy NO at 60¢).

    Attributes:
        id: Unique order identifier.
        account_id: Owner account.
        side: BUY or SELL.
        price: Limit price in cents (1–99).
        qty: Original quantity.
        filled_qty: Shares filled so far.
        remaining_qty: Shares still resting.
        status: Current lifecycle status.
        created_at: Timestamp for time priority.
    """
    account_id: str
    side: Side
    price: int
    qty: int
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    filled_qty: int = 0
    remaining_qty: int = field(init=False)
    status: OrderStatus = OrderStatus.OPEN
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not 1 <= self.price <= 99:
            raise ValueError(f"Price must be 1–99 cents, got {self.price}")
        if self.qty < 1:
            raise ValueError(f"Quantity must be ≥ 1, got {self.qty}")
        self.remaining_qty = self.qty - self.filled_qty

    def fill(self, qty: int) -> None:
        """Record a partial or full fill."""
        if qty > self.remaining_qty:
            raise ValueError(f"Cannot fill {qty}, only {self.remaining_qty} remaining")
        self.filled_qty += qty
        self.remaining_qty -= qty
        self.status = OrderStatus.FILLED if self.remaining_qty == 0 else OrderStatus.PARTIAL

    def cancel(self) -> int:
        """Cancel this order. Returns the remaining qty that was cancelled."""
        if self.status not in (OrderStatus.OPEN, OrderStatus.PARTIAL):
            raise ValueError(f"Cannot cancel order in status {self.status.value}")
        cancelled_qty = self.remaining_qty
        self.remaining_qty = 0
        self.status = OrderStatus.CANCELLED
        return cancelled_qty


@dataclass(frozen=True)
class Fill:
    """An immutable record of a matched trade.

    Attributes:
        id: Unique fill identifier.
        market_id: Which market this fill belongs to.
        taker_order_id: The aggressing order.
        maker_order_id: The resting order that was hit.
        taker_account_id: Taker's account.
        maker_account_id: Maker's account.
        taker_side: The taker's side (BUY or SELL).
        price: Fill price in cents (always the maker's limit price).
        qty: Number of shares matched.
        timestamp: When the fill occurred.
    """
    market_id: str
    taker_order_id: str
    maker_order_id: str
    taker_account_id: str
    maker_account_id: str
    taker_side: Side
    price: int
    qty: int
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
