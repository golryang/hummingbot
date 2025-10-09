from decimal import Decimal
from typing import Literal, Optional

from hummingbot.core.data_type.common import OrderType
from hummingbot.strategy_v2.executors.data_types import ConnectorPair, ExecutorConfigBase


class PerpArbitrageExecutorConfig(ExecutorConfigBase):
    """
    Configuration for Perpetual Arbitrage Executor with position holding
    """
    type: Literal["perp_arbitrage_executor"] = "perp_arbitrage_executor"

    # Markets
    long_market: ConnectorPair  # Where to open long position
    short_market: ConnectorPair  # Where to open short position

    # Position size
    order_amount: Decimal

    # Entry threshold (spread to enter position)
    entry_profitability: Decimal

    # Exit threshold (spread to close position)
    exit_profitability: Decimal

    # Stop loss threshold (max unrealized loss % before forced close per position)
    # For hedge strategies, this is rarely needed since market moves are neutral
    # Default: 100% (effectively disabled)
    stop_loss_pct: Decimal = Decimal("1.0")  # Default: 100% (disabled)

    # Stop total PnL threshold (max cumulative loss % before stopping strategy)
    # This is checked AFTER each trade completes at the controller level
    # Default: 5% cumulative loss stops creating new positions
    max_loss_pct: Decimal = Decimal("0.05")  # Default: 5% total PnL loss

    # Partial fill timeout (seconds to wait for second order after first fills)
    # If one side fills but the other doesn't within this time, convert to MARKET
    # This ensures hedge is maintained even with partial fills
    partial_fill_timeout: int = 60  # Default: 60 seconds

    # Order configuration
    order_type: OrderType = OrderType.LIMIT

    # Leverage (if applicable)
    leverage: int = 1
