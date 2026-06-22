"""
WC26 Match Simulation — pyclob Example
=======================================

Simulates a complete prediction market lifecycle for Mexico vs South Africa
(World Cup 2026 Group A, Matchday 1):

    1. Create accounts and seed house liquidity
    2. Users trade based on their views
    3. Inspect the order book, positions, and P&L
    4. Settle after the match result (Mexico wins 2-1)
    5. Verify final P&L and leaderboard

Run with: python examples/wc26_match.py
"""

from pyclob import OrderBook, Order, Account, Side

def main():
    print("=" * 70)
    print("  pyclob — WC26 Match Simulation: Mexico vs South Africa")
    print("  Group A, Matchday 1 — Estadio Azteca, Mexico City")
    print("=" * 70)

    # ── 1. Setup ────────────────────────────────────────────────────
    # Three market types per group fixture, each a binary contract:
    #   RESULT (3-way):  home / draw / away
    #   OVER/UNDER 2.5:  over / under
    #   BTTS:            yes / no

    markets = {
        "result.home":  OrderBook("MEX_RSA.result.home"),   # Mexico wins
        "result.draw":  OrderBook("MEX_RSA.result.draw"),   # Draw
        "result.away":  OrderBook("MEX_RSA.result.away"),   # South Africa wins
        "ou.over":      OrderBook("MEX_RSA.ou.over"),       # Over 2.5 goals
        "ou.under":     OrderBook("MEX_RSA.ou.under"),      # Under 2.5 goals
        "btts.yes":     OrderBook("MEX_RSA.btts.yes"),      # Both teams score
        "btts.no":      OrderBook("MEX_RSA.btts.no"),       # ≤1 team scores
    }

    # Accounts: house (liquidity provider) + 3 traders
    accounts = {
        "house":   Account("house",   starting_bankroll_c=100_000_000),  # $1M
        "alice":   Account("alice",   starting_bankroll_c=1_000_000),    # $10K
        "bob":     Account("bob",     starting_bankroll_c=1_000_000),
        "charlie": Account("charlie", starting_bankroll_c=1_000_000),
    }

    # ── 2. Seed house liquidity ─────────────────────────────────────
    # The house provides initial two-sided markets at calibrated prices.
    # Implied probabilities: MEX 40%, Draw 27%, RSA 33%

    seed_config = {
        "result.home": 40,   # Mexico ~40%
        "result.draw": 27,   # Draw ~27%
        "result.away": 33,   # South Africa ~33%
        "ou.over":     50,   # Over 2.5 ~50%
        "ou.under":    50,   # Under 2.5 ~50%
        "btts.yes":    54,   # Both score ~54%
        "btts.no":     46,   # Clean sheet ~46%
    }

    print("\nSeeding house liquidity...")
    for mkt_key, mid in seed_config.items():
        book = markets[mkt_key]
        for offset in range(1, 5):
            # Asks above mid
            if mid + offset <= 99:
                book.place_order(
                    Order("house", Side.SELL, mid + offset, 100), accounts
                )
            # Bids below mid
            if mid - offset >= 1:
                book.place_order(
                    Order("house", Side.BUY, mid - offset, 100), accounts
                )
    print("   → 7 markets seeded with 4 bid/ask levels each\n")

    # ── 3. Trading session ──────────────────────────────────────────
    print("Trading session begins...\n")

    # Alice thinks Mexico is undervalued at 40¢ — buys 200 YES
    fills = markets["result.home"].place_order(
        Order("alice", Side.BUY, 43, 200), accounts
    )
    print(f"   Alice BUY 200 MEX YES @ 43¢ limit")
    for f in fills:
        print(f"     → filled {f.qty} @ {f.price}¢")

    # Bob is bearish on Mexico — sells 150 YES (= buys NO)
    fills = markets["result.home"].place_order(
        Order("bob", Side.SELL, 38, 150), accounts
    )
    print(f"\n   Bob SELL 150 MEX YES @ 38¢ limit")
    for f in fills:
        print(f"     → filled {f.qty} @ {f.price}¢")

    # Charlie bets on goals: buys OVER 2.5 and BTTS YES
    fills1 = markets["ou.over"].place_order(
        Order("charlie", Side.BUY, 52, 100), accounts
    )
    fills2 = markets["btts.yes"].place_order(
        Order("charlie", Side.BUY, 56, 100), accounts
    )
    print(f"\n   Charlie BUY 100 OVER 2.5 @ 52¢ limit")
    for f in fills1:
        print(f"     → filled {f.qty} @ {f.price}¢")
    print(f"   Charlie BUY 100 BTTS YES @ 56¢ limit")
    for f in fills2:
        print(f"     → filled {f.qty} @ {f.price}¢")

    # Alice takes profit on half her Mexico position at 44¢
    # She needs a counterparty — Charlie will bid
    markets["result.home"].place_order(
        Order("charlie", Side.BUY, 44, 100), accounts
    )
    fills = markets["result.home"].place_order(
        Order("alice", Side.SELL, 44, 100), accounts
    )
    print(f"\n   Alice SELL 100 MEX YES @ 44¢ (taking profit)")
    for f in fills:
        print(f"     → filled {f.qty} @ {f.price}¢")

    # ── 4. Book snapshot ────────────────────────────────────────────
    print("\n" + "─" * 70)
    print("Order book snapshot — MEX RESULT (home):\n")
    d = markets["result.home"].depth(5)
    print(f"   {'BIDS':>12}  {'PRICE':>6}  {'ASKS':<12}")
    print(f"   {'────':>12}  {'─────':>6}  {'────':<12}")

    all_prices = sorted(
        set(b["price"] for b in d["bids"]) | set(a["price"] for a in d["asks"]),
        reverse=True
    )
    bid_map = {b["price"]: b["qty"] for b in d["bids"]}
    ask_map = {a["price"]: a["qty"] for a in d["asks"]}
    for px in all_prices:
        bid_str = str(bid_map[px]) if px in bid_map else ""
        ask_str = str(ask_map[px]) if px in ask_map else ""
        print(f"   {bid_str:>12}  {px:>5}¢  {ask_str:<12}")

    print(f"\n   Best bid: {markets['result.home'].best_bid}¢")
    print(f"   Best ask: {markets['result.home'].best_ask}¢")
    print(f"   Spread:   {markets['result.home'].spread}¢")
    print(f"   Last:     {markets['result.home'].last_price}¢")
    print(f"   Volume:   {markets['result.home'].volume} shares")

    # ── 5. Pre-settlement positions ─────────────────────────────────
    print("\n" + "─" * 70)
    print(" Positions before settlement:\n")
    for name in ["alice", "bob", "charlie"]:
        acct = accounts[name]
        print(f"   {name.upper()}: bankroll ${acct.bankroll_c/100:.2f}, "
              f"realized ${acct.realized_c/100:.2f}")
        for mkt_key, book in markets.items():
            pos = acct.positions.get(book.market_id)
            if pos and not pos.is_empty:
                direction = "LONG YES" if pos.shares > 0 else "SHORT YES"
                print(f"     {mkt_key}: {pos.shares:+d} ({direction}) "
                      f"@ {pos.avg_cost_c:.1f}¢ avg")
        print()

    # ── 6. Match result: Mexico 2 – 1 South Africa ──────────────────
    print("─" * 70)
    print("FULL TIME: Mexico 2 – 1 South Africa")
    print("   → Result: HOME wins")
    print("   → Total goals: 3 (≥ 3 → OVER wins)")
    print("   → Both scored: YES (2-1)")
    print("─" * 70)
    print("\n Settling all markets...\n")

    # Determine winners
    settlement_map = {
        "result.home":  True,   # Mexico won → home YES
        "result.draw":  False,
        "result.away":  False,
        "ou.over":      True,   # 3 goals ≥ 3 → over YES
        "ou.under":     False,
        "btts.yes":     True,   # Both scored → btts YES
        "btts.no":      False,
    }

    for mkt_key, won in settlement_map.items():
        result = markets[mkt_key].settle(won=won, accounts=accounts)
        status = "YES " if won else "NO "
        print(f"   {mkt_key:20s}  {status}  "
              f"positions={result['positions_settled']}, "
              f"paid=${result['total_paid_c']/100:.2f}, "
              f"orders_cancelled={result['orders_cancelled']}")

    # ── 7. Final leaderboard ────────────────────────────────────────
    print("\n" + "─" * 70)
    print("🏆 FINAL LEADERBOARD\n")

    traders = [(n, accounts[n]) for n in ["alice", "bob", "charlie"]]
    traders.sort(key=lambda x: x[1].realized_c, reverse=True)

    for rank, (name, acct) in enumerate(traders, 1):
        pnl = acct.realized_c / 100
        emoji = "🥇" if rank == 1 else "🥈" if rank == 2 else "🥉"
        sign = "+" if pnl >= 0 else ""
        print(f"   {emoji} #{rank}  {name.upper():10s}  "
              f"realized {sign}${pnl:.2f}  "
              f"bankroll ${acct.bankroll_c/100:.2f}")

    print("\n" + "=" * 70)
    print("  Simulation complete. All positions settled, reserves refunded.")
    print("=" * 70)


if __name__ == "__main__":
    main()
