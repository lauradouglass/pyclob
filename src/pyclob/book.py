"""OrderBook: central limit order book with price-time priority matching.

This module implements a continuous double-auction matching engine for
binary event contracts. The matching algorithm is the same used by major
electronic exchanges:

    1. An incoming order is checked against the opposite side of the book.
    2. Resting orders are consumed in price-time priority:
       - Best price first (highest bid for sells, lowest ask for buys)
       - At the same price, earliest order first (FIFO)
    3. Each match produces an immutable Fill record.
    4. If the incoming order is not fully filled, the remainder rests
       in the book as a new limit order.

Price improvement: when a buy order at 45¢ matches a resting sell at 41¢,
the fill occurs at 41¢ (the maker's price). The taker gets a better deal;
the maker gets exactly what they asked for.

Self-trade prevention: an order will never match against another order
from the same account.
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from .models import Order, OrderStatus, Fill, Side
from .account import Account, InsufficientBankroll


@dataclass
class BookLevel:
    """A single price level in the book, holding orders in FIFO order."""
    price: int
    orders: list[Order] = field(default_factory=list)

    @property
    def total_qty(self) -> int:
        return sum(o.remaining_qty for o in self.orders)

    def is_empty(self) -> bool:
        return all(o.remaining_qty == 0 for o in self.orders)


class OrderBook:
    """A central limit order book for a single binary event market.

    Usage::

        book = OrderBook("MEX_vs_RSA.result.home")
        fills = book.place_order(order, accounts)

    Args:
        market_id: Identifier for this market (e.g., "match1.result.home").
    """

    def __init__(self, market_id: str):
        self.market_id = market_id
        self._bids: list[BookLevel] = []  # sorted high→low by price
        self._asks: list[BookLevel] = []  # sorted low→high by price
        self._orders: dict[str, Order] = {}  # order_id → Order
        self.fills: list[Fill] = []
        self.last_price: Optional[int] = None
        self.volume: int = 0

    # ── Public API ──────────────────────────────────────────────────

    def place_order(
        self,
        order: Order,
        accounts: dict[str, Account],
    ) -> list[Fill]:
        """Place a limit order: match against the book, rest the remainder.

        This is the main entry point — equivalent to place_order() in
        the PostgreSQL engine. The entire operation is logically atomic:
        if any step fails (e.g., insufficient bankroll), no state is modified.

        Args:
            order: The incoming limit order.
            accounts: Dict of account_id → Account for all participants.

        Returns:
            List of Fill records produced by this order.

        Raises:
            InsufficientBankroll: If the taker can't cover the reserve.
            KeyError: If account_id not found in accounts dict.
        """
        taker_acct = accounts[order.account_id]

        # ── 1. Compute and debit reserve ────────────────────────────
        reserve_c = self._compute_reserve(order.side, order.price, order.qty)
        taker_acct.reserve(reserve_c)  # raises InsufficientBankroll

        # ── 2. Match against opposite side ──────────────────────────
        new_fills: list[Fill] = []
        opposite_levels = self._asks if order.side == Side.BUY else self._bids
        total_refund = 0

        while order.remaining_qty > 0 and opposite_levels:
            level = opposite_levels[0]
            if not self._prices_cross(order.side, order.price, level.price):
                break

            # Walk the level's orders (FIFO / time priority)
            matched_any = False
            for maker_order in list(level.orders):
                if order.remaining_qty == 0:
                    break
                if maker_order.remaining_qty == 0:
                    continue
                # Self-trade prevention
                if maker_order.account_id == order.account_id:
                    continue

                matched_any = True

                fill_qty = min(order.remaining_qty, maker_order.remaining_qty)
                fill_price = maker_order.price  # price improvement to taker

                # Create fill record
                f = Fill(
                    market_id=self.market_id,
                    taker_order_id=order.id,
                    maker_order_id=maker_order.id,
                    taker_account_id=order.account_id,
                    maker_account_id=maker_order.account_id,
                    taker_side=order.side,
                    price=fill_price,
                    qty=fill_qty,
                )
                new_fills.append(f)
                self.fills.append(f)

                # Update orders
                order.fill(fill_qty)
                maker_order.fill(fill_qty)

                # Position updates
                taker_side_eff = 1 if order.side == Side.BUY else -1
                maker_side_eff = 1 if maker_order.side == Side.BUY else -1

                maker_acct = accounts[maker_order.account_id]

                taker_realized = taker_acct.get_position(self.market_id).apply_fill(
                    taker_side_eff, fill_qty, fill_price
                )
                maker_realized = maker_acct.get_position(self.market_id).apply_fill(
                    maker_side_eff, fill_qty, fill_price
                )

                if taker_realized != 0:
                    taker_acct.realize(taker_realized)
                if maker_realized != 0:
                    maker_acct.realize(maker_realized)

                # Taker refund: reserved at limit, filled at better price
                if order.side == Side.BUY:
                    refund_per = order.price - fill_price
                else:
                    refund_per = fill_price - order.price
                if refund_per > 0:
                    refund = refund_per * fill_qty
                    taker_acct.release(refund)
                    total_refund += refund

                # Update market stats
                self.last_price = fill_price
                self.volume += fill_qty

            # Clean up empty orders from the level
            level.orders = [o for o in level.orders if o.remaining_qty > 0]
            if level.is_empty():
                opposite_levels.pop(0)
            elif not matched_any:
                # All remaining orders at this level are same-account → stop
                break

        # ── 3. Rest the unfilled remainder ──────────────────────────
        if order.remaining_qty > 0:
            self._insert_order(order)

        self._orders[order.id] = order
        return new_fills

    def cancel_order(self, order_id: str, accounts: dict[str, Account]) -> int:
        """Cancel a resting order and refund the reserve.

        Returns the number of shares cancelled.
        """
        order = self._orders.get(order_id)
        if order is None:
            raise ValueError(f"Order {order_id} not found")

        remaining = order.remaining_qty
        order.cancel()

        # Refund reserve on unfilled portion
        refund_c = self._compute_reserve(order.side, order.price, remaining)
        accounts[order.account_id].release(refund_c)

        # Remove from book levels
        self._remove_from_levels(order)

        return remaining

    def cancel_all(self, account_id: str, accounts: dict[str, Account]) -> int:
        """Cancel all resting orders for an account. Returns total shares cancelled."""
        total = 0
        for oid, order in list(self._orders.items()):
            if order.account_id == account_id and order.status in (
                OrderStatus.OPEN, OrderStatus.PARTIAL
            ):
                total += self.cancel_order(oid, accounts)
        return total

    def settle(self, won: bool, accounts: dict[str, Account]) -> dict:
        """Settle this market. Cancels all resting orders, pays out positions.

        Args:
            won: True if YES outcome occurred (settlement price = 100¢).
                 False if NO outcome occurred (settlement price = 0¢).
            accounts: All participant accounts.

        Returns:
            Summary dict with settlement statistics.
        """
        settlement_price = 100 if won else 0
        orders_cancelled = 0
        positions_settled = 0
        total_paid_c = 0.0

        # Phase 1: cancel all resting orders
        for order in list(self._orders.values()):
            if order.status in (OrderStatus.OPEN, OrderStatus.PARTIAL):
                remaining = order.remaining_qty
                if remaining > 0:
                    order.cancel()
                    refund_c = self._compute_reserve(order.side, order.price, remaining)
                    accounts[order.account_id].release(refund_c)
                    orders_cancelled += 1

        self._bids.clear()
        self._asks.clear()

        # Phase 2: settle all positions
        settled_accounts = set()
        for acct in accounts.values():
            pos = acct.positions.get(self.market_id)
            if pos is not None and not pos.is_empty:
                payout = acct.settle_position(self.market_id, settlement_price)
                total_paid_c += payout
                positions_settled += 1
                settled_accounts.add(acct.id)

        return {
            "market_id": self.market_id,
            "won": won,
            "settlement_price": settlement_price,
            "orders_cancelled": orders_cancelled,
            "positions_settled": positions_settled,
            "total_paid_c": total_paid_c,
        }

    # ── Book introspection ──────────────────────────────────────────

    @property
    def best_bid(self) -> Optional[int]:
        """Highest bid price, or None if no bids."""
        return self._bids[0].price if self._bids else None

    @property
    def best_ask(self) -> Optional[int]:
        """Lowest ask price, or None if no asks."""
        return self._asks[0].price if self._asks else None

    @property
    def spread(self) -> Optional[int]:
        """Bid-ask spread in cents, or None if one side is empty."""
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask - self.best_bid
        return None

    @property
    def midpoint(self) -> Optional[float]:
        """Midpoint price, or None if one side is empty."""
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2.0
        return None

    def depth(self, levels: int = 5) -> dict:
        """Return top N levels of bids and asks.

        Returns::

            {
                "bids": [{"price": 39, "qty": 100}, ...],
                "asks": [{"price": 41, "qty": 100}, ...],
            }
        """
        return {
            "bids": [
                {"price": lvl.price, "qty": lvl.total_qty}
                for lvl in self._bids[:levels]
            ],
            "asks": [
                {"price": lvl.price, "qty": lvl.total_qty}
                for lvl in self._asks[:levels]
            ],
        }

    def open_orders(self, account_id: Optional[str] = None) -> list[Order]:
        """Return all resting orders, optionally filtered by account."""
        result = []
        for order in self._orders.values():
            if order.status in (OrderStatus.OPEN, OrderStatus.PARTIAL):
                if account_id is None or order.account_id == account_id:
                    result.append(order)
        return result

    # ── Private helpers ─────────────────────────────────────────────

    @staticmethod
    def _compute_reserve(side: Side, price: int, qty: int) -> int:
        """Compute the cash reserve for an order.

        BUY:  reserve = price × qty         (max loss if YES doesn't happen)
        SELL: reserve = (100 - price) × qty  (max loss if YES happens)
        """
        if side == Side.BUY:
            return price * qty
        else:
            return (100 - price) * qty

    @staticmethod
    def _prices_cross(taker_side: Side, taker_price: int, maker_price: int) -> bool:
        """Check if a taker's limit price crosses a maker's price."""
        if taker_side == Side.BUY:
            return taker_price >= maker_price  # buy at or above the ask
        else:
            return taker_price <= maker_price  # sell at or below the bid

    def _insert_order(self, order: Order) -> None:
        """Insert an order into the correct side of the book, maintaining sort."""
        if order.side == Side.BUY:
            self._insert_into_levels(self._bids, order, reverse=True)
        else:
            self._insert_into_levels(self._asks, order, reverse=False)

    @staticmethod
    def _insert_into_levels(levels: list[BookLevel], order: Order, reverse: bool) -> None:
        """Insert order into sorted level list. Creates a new level if needed."""
        for lvl in levels:
            if lvl.price == order.price:
                lvl.orders.append(order)
                return

        new_level = BookLevel(price=order.price, orders=[order])
        if reverse:
            # Bids: highest first
            idx = 0
            for i, lvl in enumerate(levels):
                if order.price > lvl.price:
                    idx = i
                    break
                idx = i + 1
            levels.insert(idx, new_level)
        else:
            # Asks: lowest first
            idx = 0
            for i, lvl in enumerate(levels):
                if order.price < lvl.price:
                    idx = i
                    break
                idx = i + 1
            levels.insert(idx, new_level)

    def _remove_from_levels(self, order: Order) -> None:
        """Remove a cancelled/filled order from the book levels."""
        levels = self._bids if order.side == Side.BUY else self._asks
        for lvl in levels:
            if lvl.price == order.price:
                lvl.orders = [o for o in lvl.orders if o.id != order.id]
                break
        # Clean up empty levels
        if order.side == Side.BUY:
            self._bids = [l for l in self._bids if not l.is_empty()]
        else:
            self._asks = [l for l in self._asks if not l.is_empty()]
