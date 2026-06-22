"""Test suite for pyclob matching engine.

Covers: basic matching, price-time priority, partial fills, sweeps,
self-trade prevention, reserves, refunds, cost basis, realized P&L,
settlement, cancel + refund, insufficient bankroll, and edge cases.
"""

import pytest
from pyclob import OrderBook, Order, Account, Side, OrderStatus, InsufficientBankroll

# ── Helpers ─────────────────────────────────────────────────────────

def make_accounts(**kwargs) -> dict[str, Account]:
    """Create accounts with default $10,000 bankroll."""
    return {
        name: Account(name, starting_bankroll_c=bal)
        for name, bal in kwargs.items()
    }


def setup_basic():
    """Standard 2-user setup."""
    accounts = make_accounts(alice=1_000_000, bob=1_000_000)
    book = OrderBook("test.market")
    return book, accounts


# ── Basic matching ──────────────────────────────────────────────────

class TestBasicMatching:
    def test_buy_matches_sell(self):
        book, accts = setup_basic()
        # Alice posts a sell at 60¢
        book.place_order(Order("alice", Side.SELL, 60, 100), accts)
        # Bob buys at 60¢ — should match
        fills = book.place_order(Order("bob", Side.BUY, 60, 100), accts)
        assert len(fills) == 1
        assert fills[0].price == 60
        assert fills[0].qty == 100
        assert fills[0].taker_side == Side.BUY

    def test_no_match_when_prices_dont_cross(self):
        book, accts = setup_basic()
        book.place_order(Order("alice", Side.SELL, 60, 100), accts)
        # Bob bids 59¢ — doesn't cross the 60¢ ask
        fills = book.place_order(Order("bob", Side.BUY, 59, 100), accts)
        assert len(fills) == 0
        assert book.best_bid == 59
        assert book.best_ask == 60

    def test_price_improvement(self):
        """Taker at 65¢ should fill at maker's 60¢ (price improvement)."""
        book, accts = setup_basic()
        book.place_order(Order("alice", Side.SELL, 60, 100), accts)
        fills = book.place_order(Order("bob", Side.BUY, 65, 100), accts)
        assert fills[0].price == 60  # maker's price, not taker's limit

    def test_sell_matches_bid(self):
        book, accts = setup_basic()
        book.place_order(Order("alice", Side.BUY, 40, 100), accts)
        fills = book.place_order(Order("bob", Side.SELL, 40, 100), accts)
        assert len(fills) == 1
        assert fills[0].price == 40


# ── Price-time priority ────────────────────────────────────────────

class TestPriceTimePriority:
    def test_best_price_fills_first(self):
        book, accts = setup_basic()
        accts["charlie"] = Account("charlie", 1_000_000)
        # Alice sells at 60, Charlie sells at 55
        book.place_order(Order("alice", Side.SELL, 60, 100), accts)
        book.place_order(Order("charlie", Side.SELL, 55, 100), accts)
        # Bob buys at 60 — should fill at 55 first (better price)
        fills = book.place_order(Order("bob", Side.BUY, 60, 100), accts)
        assert fills[0].price == 55
        assert fills[0].maker_account_id == "charlie"

    def test_time_priority_at_same_price(self):
        book, accts = setup_basic()
        accts["charlie"] = Account("charlie", 1_000_000)
        # Alice and Charlie both sell at 60, Alice first
        o1 = Order("alice", Side.SELL, 60, 50)
        o2 = Order("charlie", Side.SELL, 60, 50)
        book.place_order(o1, accts)
        book.place_order(o2, accts)
        # Bob buys 50 — should match Alice (earlier)
        fills = book.place_order(Order("bob", Side.BUY, 60, 50), accts)
        assert fills[0].maker_account_id == "alice"


# ── Partial fills and sweeps ───────────────────────────────────────

class TestPartialFills:
    def test_partial_fill(self):
        book, accts = setup_basic()
        book.place_order(Order("alice", Side.SELL, 60, 100), accts)
        fills = book.place_order(Order("bob", Side.BUY, 60, 30), accts)
        assert fills[0].qty == 30
        # Alice's order should be PARTIAL with 70 remaining
        alice_orders = book.open_orders("alice")
        assert len(alice_orders) == 1
        assert alice_orders[0].remaining_qty == 70
        assert alice_orders[0].status == OrderStatus.PARTIAL

    def test_sweep_multiple_levels(self):
        book, accts = setup_basic()
        accts["charlie"] = Account("charlie", 1_000_000)
        # Sell at 55 (50 shares) and 60 (50 shares)
        book.place_order(Order("alice", Side.SELL, 55, 50), accts)
        book.place_order(Order("charlie", Side.SELL, 60, 50), accts)
        # Bob buys 80 at 60 — sweeps 50@55 then 30@60
        fills = book.place_order(Order("bob", Side.BUY, 60, 80), accts)
        assert len(fills) == 2
        assert fills[0].price == 55 and fills[0].qty == 50
        assert fills[1].price == 60 and fills[1].qty == 30

    def test_resting_order_after_partial_fill(self):
        book, accts = setup_basic()
        book.place_order(Order("alice", Side.SELL, 60, 50), accts)
        fills = book.place_order(Order("bob", Side.BUY, 55, 100), accts)
        # No match (55 < 60), Bob's full order rests
        assert len(fills) == 0
        assert book.best_bid == 55
        bob_orders = book.open_orders("bob")
        assert bob_orders[0].remaining_qty == 100


# ── Self-trade prevention ──────────────────────────────────────────

class TestSelfTrade:
    def test_no_self_match(self):
        book, accts = setup_basic()
        book.place_order(Order("alice", Side.SELL, 40, 100), accts)
        # Alice tries to buy at 40 — should NOT match her own sell
        fills = book.place_order(Order("alice", Side.BUY, 40, 100), accts)
        assert len(fills) == 0
        # Both orders should rest
        assert len(book.open_orders("alice")) == 2


# ── Reserve system ─────────────────────────────────────────────────

class TestReserves:
    def test_buy_reserve(self):
        book, accts = setup_basic()
        book.place_order(Order("alice", Side.BUY, 40, 100), accts)
        # Reserve = 40 × 100 = 4000¢
        assert accts["alice"].bankroll_c == 1_000_000 - 4_000

    def test_sell_reserve(self):
        book, accts = setup_basic()
        book.place_order(Order("alice", Side.SELL, 40, 100), accts)
        # Reserve = (100-40) × 100 = 6000¢
        assert accts["alice"].bankroll_c == 1_000_000 - 6_000

    def test_insufficient_bankroll_rejects(self):
        accts = make_accounts(broke=500)
        book = OrderBook("test")
        with pytest.raises(InsufficientBankroll):
            book.place_order(Order("broke", Side.BUY, 40, 100), accts)

    def test_refund_on_price_improvement(self):
        book, accts = setup_basic()
        book.place_order(Order("alice", Side.SELL, 40, 100), accts)
        # Bob bids 45 (reserves 45×100=4500), fills at 40 (refund 5×100=500)
        initial = accts["bob"].bankroll_c
        book.place_order(Order("bob", Side.BUY, 45, 100), accts)
        # Net cost to Bob = 40×100 = 4000
        assert accts["bob"].bankroll_c == initial - 4_000


# ── Cancel + refund ────────────────────────────────────────────────

class TestCancel:
    def test_cancel_refunds_reserve(self):
        book, accts = setup_basic()
        o = Order("alice", Side.BUY, 40, 100)
        book.place_order(o, accts)
        assert accts["alice"].bankroll_c == 1_000_000 - 4_000
        book.cancel_order(o.id, accts)
        assert accts["alice"].bankroll_c == 1_000_000  # fully refunded

    def test_cancel_partial_refunds_remainder(self):
        book, accts = setup_basic()
        o = Order("alice", Side.SELL, 50, 100)
        book.place_order(o, accts)
        # Bob fills 30 of Alice's 100
        book.place_order(Order("bob", Side.BUY, 50, 30), accts)
        # Alice cancels remaining 70
        cancelled = book.cancel_order(o.id, accts)
        assert cancelled == 70
        # Refund = 70 × (100-50) = 3500
        assert o.status == OrderStatus.CANCELLED


# ── Position accounting & P&L ──────────────────────────────────────

class TestPositionAccounting:
    def test_opening_long_cost_basis(self):
        book, accts = setup_basic()
        book.place_order(Order("alice", Side.SELL, 40, 100), accts)
        book.place_order(Order("bob", Side.BUY, 40, 100), accts)
        pos = accts["bob"].get_position("test.market")
        assert pos.shares == 100
        assert pos.avg_cost_c == 40.0

    def test_opening_short_cost_basis(self):
        book, accts = setup_basic()
        book.place_order(Order("alice", Side.BUY, 40, 100), accts)
        book.place_order(Order("bob", Side.SELL, 40, 100), accts)
        pos = accts["bob"].get_position("test.market")
        assert pos.shares == -100
        assert pos.avg_cost_c == 40.0

    def test_weighted_avg_on_add(self):
        book, accts = setup_basic()
        accts["charlie"] = Account("charlie", 1_000_000)
        # Bob buys 100 at 40, then 100 at 50
        book.place_order(Order("alice", Side.SELL, 40, 100), accts)
        book.place_order(Order("bob", Side.BUY, 40, 100), accts)
        book.place_order(Order("charlie", Side.SELL, 50, 100), accts)
        book.place_order(Order("bob", Side.BUY, 50, 100), accts)
        pos = accts["bob"].get_position("test.market")
        assert pos.shares == 200
        assert pos.avg_cost_c == pytest.approx(45.0)

    def test_realized_pnl_on_close(self):
        book, accts = setup_basic()
        accts["charlie"] = Account("charlie", 1_000_000)
        # Bob buys 100 at 40
        book.place_order(Order("alice", Side.SELL, 40, 100), accts)
        book.place_order(Order("bob", Side.BUY, 40, 100), accts)
        # Bob sells 100 at 60 — realizes (60-40)×100 = 2000¢ profit
        book.place_order(Order("charlie", Side.BUY, 60, 100), accts)
        book.place_order(Order("bob", Side.SELL, 60, 100), accts)
        assert accts["bob"].realized_c == pytest.approx(2_000)
        pos = accts["bob"].get_position("test.market")
        assert pos.shares == 0

    def test_partial_close_keeps_remainder(self):
        book, accts = setup_basic()
        accts["charlie"] = Account("charlie", 1_000_000)
        book.place_order(Order("alice", Side.SELL, 40, 100), accts)
        book.place_order(Order("bob", Side.BUY, 40, 100), accts)
        # Bob sells 60 at 50 — realizes (50-40)×60 = 600
        book.place_order(Order("charlie", Side.BUY, 50, 60), accts)
        book.place_order(Order("bob", Side.SELL, 50, 60), accts)
        assert accts["bob"].realized_c == pytest.approx(600)
        pos = accts["bob"].get_position("test.market")
        assert pos.shares == 40
        assert pos.avg_cost_c == pytest.approx(40.0)  # unchanged


# ── Settlement ─────────────────────────────────────────────────────

class TestSettlement:
    def test_yes_wins_long_payout(self):
        book, accts = setup_basic()
        book.place_order(Order("alice", Side.SELL, 40, 100), accts)
        book.place_order(Order("bob", Side.BUY, 40, 100), accts)
        result = book.settle(won=True, accounts=accts)
        # Bob had 100 long YES at 40¢ → payout = 100 × 100¢ = 10,000¢
        # Alice was short YES → payout = 0
        assert result["total_paid_c"] == pytest.approx(10_000)
        assert accts["bob"].realized_c == pytest.approx(6_000)  # (100-40)×100

    def test_yes_loses_short_payout(self):
        book, accts = setup_basic()
        book.place_order(Order("alice", Side.SELL, 40, 100), accts)
        book.place_order(Order("bob", Side.BUY, 40, 100), accts)
        result = book.settle(won=False, accounts=accts)
        # Alice was short YES at 40 → payout = 100 × 100¢ (she gets full)
        # Bob had long YES at 40 → payout = 0
        assert accts["alice"].realized_c == pytest.approx(4_000)  # (40-0)×100
        assert accts["bob"].realized_c == pytest.approx(-4_000)   # (0-40)×100

    def test_settlement_cancels_resting_orders(self):
        book, accts = setup_basic()
        book.place_order(Order("alice", Side.BUY, 30, 100), accts)
        result = book.settle(won=True, accounts=accts)
        assert result["orders_cancelled"] == 1
        assert len(book.open_orders()) == 0
        # Reserve should be fully refunded
        assert accts["alice"].bankroll_c == 1_000_000

    def test_settlement_idempotent_positions(self):
        book, accts = setup_basic()
        book.place_order(Order("alice", Side.SELL, 40, 100), accts)
        book.place_order(Order("bob", Side.BUY, 40, 100), accts)
        book.settle(won=True, accounts=accts)
        # Positions should be cleared
        assert accts["bob"].positions.get("test.market") is None

    def test_settlement_with_prior_realized(self):
        """Realized from trading + settlement should be additive."""
        book, accts = setup_basic()
        accts["charlie"] = Account("charlie", 1_000_000)
        # Bob buys 100 at 40, sells 50 at 50 (realizes 500), holds 50
        book.place_order(Order("alice", Side.SELL, 40, 100), accts)
        book.place_order(Order("bob", Side.BUY, 40, 100), accts)
        book.place_order(Order("charlie", Side.BUY, 50, 50), accts)
        book.place_order(Order("bob", Side.SELL, 50, 50), accts)
        trading_realized = accts["bob"].realized_c
        assert trading_realized == pytest.approx(500)
        # Settle: YES wins. Bob's remaining 50@40 settles at 100.
        book.settle(won=True, accounts=accts)
        # Settlement realized = (100-40)×50 = 3000
        assert accts["bob"].realized_c == pytest.approx(500 + 3_000)


# ── Book introspection ─────────────────────────────────────────────

class TestBookIntrospection:
    def test_depth(self):
        book, accts = setup_basic()
        book.place_order(Order("alice", Side.BUY, 38, 50), accts)
        book.place_order(Order("alice", Side.BUY, 39, 100), accts)
        book.place_order(Order("bob", Side.SELL, 41, 75), accts)
        d = book.depth(levels=5)
        assert d["bids"][0] == {"price": 39, "qty": 100}
        assert d["bids"][1] == {"price": 38, "qty": 50}
        assert d["asks"][0] == {"price": 41, "qty": 75}

    def test_spread_and_midpoint(self):
        book, accts = setup_basic()
        book.place_order(Order("alice", Side.BUY, 39, 100), accts)
        book.place_order(Order("bob", Side.SELL, 41, 100), accts)
        assert book.spread == 2
        assert book.midpoint == 40.0

    def test_empty_book(self):
        book = OrderBook("empty")
        assert book.best_bid is None
        assert book.best_ask is None
        assert book.spread is None
        assert book.midpoint is None
