"""pyclob — A pure-Python central limit order book for binary event contracts.

Usage::

    from pyclob import OrderBook, Order, Account, Side

    # Create accounts
    alice = Account("alice", starting_bankroll_c=1_000_000)
    bob   = Account("bob",   starting_bankroll_c=1_000_000)
    accounts = {"alice": alice, "bob": bob}

    # Create an order book for a market
    book = OrderBook("MEX_vs_RSA.result.home")

    # Alice bids 40¢ for 100 shares of YES
    order = Order(account_id="alice", side=Side.BUY, price=40, qty=100)
    fills = book.place_order(order, accounts)

    # Bob sells at 40¢ — matches Alice's bid
    order2 = Order(account_id="bob", side=Side.SELL, price=40, qty=100)
    fills2 = book.place_order(order2, accounts)

    # Settle: MEX won → YES pays $1
    result = book.settle(won=True, accounts=accounts)
"""

__version__ = "0.1.0"

from .models import Order, Fill, Side, OrderStatus
from .account import Account, Position, InsufficientBankroll
from .book import OrderBook

__all__ = [
    "OrderBook",
    "Order",
    "Fill",
    "Side",
    "OrderStatus",
    "Account",
    "Position",
    "InsufficientBankroll",
]
