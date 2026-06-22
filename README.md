# pyclob

A pure-Python central limit order book (CLOB) matching engine for binary event contracts.

```
pip install pyclob
```

```python
from pyclob import OrderBook, Order, Account, Side

alice = Account("alice")
bob   = Account("bob")
accounts = {"alice": alice, "bob": bob}

book = OrderBook("MEX_vs_RSA.result.home")

# Alice sells YES at 40¢ — she thinks Mexico won't win
book.place_order(Order("alice", Side.SELL, 40, 100), accounts)

# Bob buys YES at 40¢ — he thinks Mexico will win
fills = book.place_order(Order("bob", Side.BUY, 40, 100), accounts)
# → [Fill(price=40, qty=100, taker=bob, maker=alice)]

# Mexico wins 2-1 → YES pays $1.00
result = book.settle(won=True, accounts=accounts)
# Bob: +$60.00 realized  |  Alice: -$60.00 realized
```

---

## What This Is

pyclob implements the core mechanics of an electronic exchange — the same matching architecture used by NASDAQ, CME, and prediction market platforms like Kalshi and Polymarket. It is designed to be small enough to read in an afternoon and correct enough to run a real contest.

This library was extracted from [WC26-X](https://github.com/lauradouglass/wc26x), a live play-money prediction market exchange I built for the 2026 FIFA World Cup. The WC26-X matching engine runs in PostgreSQL (PL/pgSQL); pyclob is the same logic in pure Python, designed for simulation, backtesting, and education.

---

## Core Concepts

### Binary Event Contracts

A binary contract pays **$1.00** if an outcome occurs, **$0.00** otherwise. The price in cents represents the market's implied probability:

- Trading at **40¢** → market implies ~40% probability
- Trading at **73¢** → market implies ~73% probability

Every contract has two sides: **YES** (the outcome happens) and **NO** (it doesn't). Buying YES at 40¢ is equivalent to selling NO at 60¢ — they're the same trade.

### The Order Book

pyclob implements a **continuous double-auction** order book with **price-time priority**:

1. **Price priority:** The best-priced resting order fills first. For incoming buys, the lowest ask; for incoming sells, the highest bid.

2. **Time priority:** At the same price level, the earliest order fills first (FIFO).

3. **Price improvement:** When a buy at 45¢ matches a resting sell at 41¢, the fill occurs at **41¢** (the maker's price). The taker gets a better deal than their limit; the maker gets exactly what they asked for.

---

## The Math

### Reserve System

When an order is placed, cash is **escrowed** from the account's bankroll immediately. The reserve equals the worst-case loss:

```
BUY reserve  = price × quantity
SELL reserve = (100 - price) × quantity
```

**Example:** A SELL at 40¢ for 100 shares reserves `(100 - 40) × 100 = 6,000¢`. This covers the seller's maximum obligation: if YES happens, they owe $1/share but received 40¢/share, so net loss = 60¢/share.

**Invariant:** An account's bankroll can never go negative. If the reserve exceeds available cash, the order is rejected with `InsufficientBankroll`. This is enforced at order time, not at settlement — **risk is bounded by construction.**

When a fill occurs at a better price than the limit, the surplus reserve is refunded:

```
BUY refund  = (limit_price - fill_price) × fill_qty
SELL refund = (fill_price - limit_price) × fill_qty
```

### Position Accounting

Positions track a **signed share count** and a **weighted-average cost basis**:

| shares | Meaning | Profit when |
|--------|---------|-------------|
| `> 0`  | Long YES | Outcome happens (settlement = 100¢) |
| `< 0`  | Short YES / Long NO | Outcome doesn't happen (settlement = 0¢) |

When a fill hits an account's position, one of three accounting paths applies:

**Path 1 — Opening or adding (same direction):**

The cost basis is the weighted average of the old and new positions:

$$C_{new} = \frac{|S_{old}| \cdot C_{old} + Q_{fill} \cdot P_{fill}}{|S_{new}|}$$

No P&L is realized. The position grows at a blended cost.

**Path 2 — Reducing (opposite direction, not crossing zero):**

The closing portion realizes P&L against the existing cost basis:

If closing a long:

$$\text{Realized P\&L} = (P_{fill} - C_{old}) \times Q_{close}$$

If closing a short:

$$\text{Realized P\&L} = (C_{old} - P_{fill}) \times Q_{close}$$

The remaining position keeps its original cost basis unchanged.

**Path 3 — Flipping (opposite direction, crossing zero):**

The entire old position is closed (realizing P&L on all old shares), then a new position opens at the fill price with the leftover quantity.

**Example:**
```python
# Buy 100 at 40¢, then sell 60 at 55¢:
# → Close 60 shares: realized = (55 - 40) × 60 = 900¢ ($9.00 profit)
# → Remaining: 40 shares long at 40¢ avg cost (unchanged)
```

### Settlement

When a market resolves, settlement is a two-phase process:

**Phase 1 — Cancel resting orders.** All open/partial orders are cancelled and their reserves refunded to the owner's bankroll.

**Phase 2 — Pay out positions.** Each position settles at the terminal price:

| Position | Outcome | Payout per share | Realized per share |
|----------|---------|------------------|--------------------|
| Long YES | YES wins | 100¢ | `100 - avg_cost` |
| Long YES | NO wins | 0¢ | `0 - avg_cost` |
| Short YES | YES wins | 0¢ | `avg_cost - 100` |
| Short YES | NO wins | 100¢ | `avg_cost - 0` |

Settlement P&L is **additive** with any P&L realized from trading during the market's lifetime. A trader who bought at 40¢, sold half at 55¢, and held the rest to settlement (YES wins) would realize:

```
Trading P&L:    (55 - 40) × 50 = 750¢
Settlement P&L: (100 - 40) × 50 = 3,000¢
Total:          3,750¢ = $37.50
```

---

## API Reference

### OrderBook

```python
book = OrderBook(market_id: str)

book.place_order(order, accounts) → list[Fill]
book.cancel_order(order_id, accounts) → int  # shares cancelled
book.cancel_all(account_id, accounts) → int
book.settle(won: bool, accounts) → dict

book.best_bid → int | None
book.best_ask → int | None
book.spread → int | None
book.midpoint → float | None
book.depth(levels=5) → {"bids": [...], "asks": [...]}
book.open_orders(account_id=None) → list[Order]
```

### Order

```python
order = Order(
    account_id: str,
    side: Side.BUY | Side.SELL,
    price: int,     # 1–99 cents
    qty: int,       # ≥ 1
)
```

### Account

```python
acct = Account(account_id: str, starting_bankroll_c: int = 1_000_000)

acct.bankroll_c    # available cash in cents
acct.reserved_c    # cash locked in orders
acct.realized_c    # cumulative realized P&L
acct.total_equity_c  # bankroll + reserved

acct.get_position(market_id) → Position
acct.settle_position(market_id, settlement_price) → float  # payout
```

### Position

```python
pos = acct.get_position("some.market")

pos.shares          # signed: >0 long YES, <0 short YES
pos.avg_cost_c      # weighted-average cost basis
pos.unrealized_pnl(mark_price) → float  # unrealized P&L at a given price
```

---

## Testing

```bash
git clone https://github.com/lauradouglass/pyclob.git
cd pyclob
pip install -e .
python -m pytest -v
```

29 tests covering:

- Basic matching (buy/sell, no-cross, price improvement)
- Price-time priority (best price first, FIFO at same level)
- Partial fills and multi-level sweeps
- Self-trade prevention
- Reserve system (buy/sell reserves, insufficient bankroll, refund on price improvement)
- Cancel and refund (full cancel, partial cancel after fill)
- Position accounting (cost basis, weighted average, realized P&L, partial close)
- Settlement (YES wins, NO wins, order cancellation, additive P&L)
- Book introspection (depth, spread, midpoint, empty book)

---

## Key Notes

**Integer cents, not floats.** All prices and bankrolls are in integer cents. Cost basis uses Python `float` for weighted-average precision but all external interfaces are cent-denominated. This avoids accumulated rounding errors across thousands of fills.

**Synchronous, single-threaded.** pyclob does not use threads, locks, or async. Each `place_order` call is logically atomic — it either completes fully or raises an exception. This makes the engine easy to reason about and test. For concurrent access, wrap it in a database transaction (as WC26-X does with PostgreSQL's `FOR UPDATE` locking).

**No persistence.** The order book lives in memory. For persistence, serialize the state or use pyclob as the matching logic inside a database-backed system.

**Maker-price execution.** Fills always occur at the resting order's price, not the aggressor's limit. This is standard exchange behavior and provides price improvement to takers.

---

## Related

- [WC26-X](https://github.com/lauradouglass/wc26x) — Live prediction market exchange for the 2026 World Cup. Uses the same matching logic in PostgreSQL with real-time WebSocket push, Supabase Auth, and admin settlement UI.

---

## License

MIT

## Author

Laura Douglas — [LinkedIn](https://linkedin.com/in/laura-douglas-904a741ab)
BS Computer Engineering, Drexel University. Incoming MS Mathematics in Finance, NYU Courant.