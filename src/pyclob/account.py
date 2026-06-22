"""Account: bankroll management, reserve system, and position tracking.

The reserve system ensures no account can ever owe more than its bankroll.
When an order is placed, cash is escrowed immediately:

    BUY reserve  = price × qty           (max loss if YES doesn't happen)
    SELL reserve = (100 - price) × qty   (max loss if YES happens)

Reserves are refunded when:
    - An order is cancelled (full remaining reserve returned)
    - A fill occurs at a better price than the limit (difference refunded)
    - A market settles (positions pay out, remaining orders cancelled)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .models import Side


@dataclass
class Position:
    """A position in a single market.

    shares > 0  →  long YES  (profit if outcome happens)
    shares < 0  →  short YES / long NO  (profit if outcome doesn't happen)

    avg_cost_c tracks the weighted-average entry price for P&L computation.
    """
    shares: int = 0
    avg_cost_c: float = 0.0

    @property
    def is_empty(self) -> bool:
        return self.shares == 0

    def unrealized_pnl(self, mark_price: int) -> float:
        """Unrealized P&L in cents at a given mark price.

        For long:  (mark - avg_cost) × shares
        For short: (avg_cost - mark) × |shares|
        """
        if self.shares == 0:
            return 0.0
        if self.shares > 0:
            return (mark_price - self.avg_cost_c) * self.shares
        else:
            return (self.avg_cost_c - mark_price) * abs(self.shares)

    def apply_fill(self, side_effect: int, qty: int, price: int) -> float:
        """Apply a fill to this position. Returns realized P&L in cents.

        Args:
            side_effect: +1 for buying YES, -1 for selling YES (buying NO).
            qty: Number of shares filled.
            price: Fill price in cents.

        Returns:
            Realized P&L in cents (0 if opening/adding, nonzero if closing).

        Position accounting follows three paths:

        1. Opening or adding (same direction):
            avg_cost = (|old_shares| × old_avg + qty × price) / |new_shares|

        2. Reducing (opposite direction, not flipping):
            realized = (price - avg_cost) × closing_qty   [if closing long]
            realized = (avg_cost - price) × closing_qty   [if closing short]
            avg_cost unchanged on remainder.

        3. Flipping (opposite direction, crossing zero):
            Close the old position entirely (realize P&L on all old shares),
            then open fresh at the fill price with the leftover quantity.
        """
        d_shares = side_effect * qty
        new_shares = self.shares + d_shares
        realized = 0.0

        if self.shares == 0 or _same_sign(self.shares, d_shares):
            # Path 1: Opening or adding — weighted average cost basis
            old_notional = abs(self.shares) * self.avg_cost_c
            add_notional = qty * price
            if abs(new_shares) > 0:
                self.avg_cost_c = (old_notional + add_notional) / abs(new_shares)
            else:
                self.avg_cost_c = 0.0
        else:
            # Path 2 or 3: Reducing or flipping
            closing_qty = min(abs(self.shares), qty)

            if self.shares > 0:
                # Were long, now selling → realized = (sell_price - avg) × closing
                realized = (price - self.avg_cost_c) * closing_qty
            else:
                # Were short, now buying back → realized = (avg - buy_price) × closing
                realized = (self.avg_cost_c - price) * closing_qty

            leftover = qty - closing_qty
            if new_shares == 0:
                self.avg_cost_c = 0.0
            elif leftover > 0:
                # Flipped: leftover opens fresh at fill price
                self.avg_cost_c = float(price)
            # else: just reduced, avg_cost unchanged

        self.shares = new_shares
        return realized


def _same_sign(a: int, b: int) -> bool:
    """True if a and b have the same sign (both positive or both negative)."""
    return (a > 0 and b > 0) or (a < 0 and b < 0)


class Account:
    """A trading account with bankroll, reserves, and positions.

    The account enforces the invariant:
        bankroll ≥ 0  at all times

    Cash is moved via:
        reserve(amount)   — lock cash for a pending order
        release(amount)   — unlock cash (cancel or fill refund)
        realize(amount)   — add/subtract realized P&L

    Attributes:
        id: Unique account identifier.
        bankroll_c: Available cash in cents.
        reserved_c: Cash locked in pending orders.
        realized_c: Cumulative realized P&L in cents.
        positions: Dict of market_id → Position.
    """

    def __init__(self, account_id: str, starting_bankroll_c: int = 1_000_000):
        self.id = account_id
        self.bankroll_c = starting_bankroll_c
        self.reserved_c = 0
        self.realized_c = 0
        self.positions: dict[str, Position] = {}

    def reserve(self, amount_c: int) -> None:
        """Escrow cash for a pending order. Raises if insufficient."""
        if amount_c > self.bankroll_c:
            raise InsufficientBankroll(
                f"Need {amount_c}¢ but only {self.bankroll_c}¢ available"
            )
        self.bankroll_c -= amount_c
        self.reserved_c += amount_c

    def release(self, amount_c: int) -> None:
        """Release escrowed cash back to bankroll (cancel or refund)."""
        self.reserved_c -= amount_c
        self.bankroll_c += amount_c

    def realize(self, pnl_c: float) -> None:
        """Apply realized P&L to the account."""
        self.realized_c += pnl_c

    def get_position(self, market_id: str) -> Position:
        """Get or create a position for a market."""
        if market_id not in self.positions:
            self.positions[market_id] = Position()
        return self.positions[market_id]

    def settle_position(self, market_id: str, settlement_price: int) -> float:
        """Settle a position at a terminal price (0 or 100).

        Returns the payout in cents credited to bankroll.

        Settlement logic:
            Long YES  + YES wins (price=100): payout = shares × 100
            Long YES  + NO  wins (price=0):   payout = 0
            Short YES + YES wins (price=100): payout = 0
            Short YES + NO  wins (price=0):   payout = |shares| × 100
        """
        pos = self.positions.get(market_id)
        if pos is None or pos.is_empty:
            return 0.0

        if pos.shares > 0:
            payout_c = pos.shares * settlement_price
            realized = (settlement_price - pos.avg_cost_c) * pos.shares
        else:
            payout_c = abs(pos.shares) * (100 - settlement_price)
            realized = (pos.avg_cost_c - settlement_price) * abs(pos.shares)

        self.bankroll_c += int(payout_c)
        self.realized_c += realized

        # Remove the settled position
        del self.positions[market_id]
        return float(payout_c)

    @property
    def total_equity_c(self) -> int:
        """Total equity = bankroll + reserved (cash in orders is still yours)."""
        return self.bankroll_c + self.reserved_c

    def __repr__(self) -> str:
        return (
            f"Account({self.id!r}, bankroll={self.bankroll_c}¢, "
            f"reserved={self.reserved_c}¢, realized={self.realized_c:.0f}¢)"
        )


class InsufficientBankroll(Exception):
    """Raised when an account lacks the funds to cover a reserve."""
    pass
