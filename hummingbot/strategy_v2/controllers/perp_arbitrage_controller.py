import asyncio
from decimal import Decimal
from typing import Dict, List

from pydantic import Field, field_validator

from hummingbot.client.ui.interface_utils import format_df_for_printout
from hummingbot.core.data_type.common import MarketDict, OrderType, PriceType
from hummingbot.strategy_v2.controllers.controller_base import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.executors.data_types import ConnectorPair
from hummingbot.strategy_v2.executors.perp_arbitrage_executor.data_types import PerpArbitrageExecutorConfig
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, ExecutorAction


class PerpArbitrageControllerConfig(ControllerConfigBase):
    """
    Configuration for perpetual-to-perpetual arbitrage strategy.
    Monitors price differences between two perpetual exchanges and executes arbitrage when profitable.
    """
    controller_name: str = Field(
        default="perp_arbitrage_btc",
        json_schema_extra={
            "prompt": "Enter a name for this controller (e.g., perp_arbitrage_btc): ",
            "prompt_on_new": True
        }
    )
    controller_type: str = "perp_arbitrage"

    # Exchange 1 (Buying market)
    connector_1: str = Field(
        default="hyperliquid_perpetual",
        json_schema_extra={
            "prompt": "Enter the first connector name (e.g., hyperliquid_perpetual): ",
            "prompt_on_new": True
        }
    )
    trading_pair_1: str = Field(
        default="BTC-USD",
        json_schema_extra={
            "prompt": "Enter the trading pair for connector 1 (e.g., BTC-USD): ",
            "prompt_on_new": True
        }
    )

    # Exchange 2 (Selling market)
    connector_2: str = Field(
        default="binance_perpetual",
        json_schema_extra={
            "prompt": "Enter the second connector name (e.g., binance_perpetual): ",
            "prompt_on_new": True
        }
    )
    trading_pair_2: str = Field(
        default="BTC-USDT",
        json_schema_extra={
            "prompt": "Enter the trading pair for connector 2 (e.g., BTC-USDT): ",
            "prompt_on_new": True
        }
    )

    # Order configuration
    order_amount: Decimal = Field(
        default=Decimal("0.01"),
        gt=0,
        json_schema_extra={
            "prompt": "Enter the order amount in base asset (e.g., 0.01 BTC): ",
            "prompt_on_new": True,
            "is_updatable": True
        }
    )

    # Entry profitability threshold (spread to open positions)
    # TARGET (PRODUCTION): 0.00133 = 0.133% = $160 at $120k BTC
    # TESTING: 0.00075 = 0.075% = $90 at $120k BTC
    entry_profitability: Decimal = Field(
        default=Decimal("0.00075"),
        gt=0,
        json_schema_extra={
            "prompt": "Enter entry profitability threshold (e.g., 0.00133 for $160 spread at $120k, testing: 0.00075 for $90): ",
            "prompt_on_new": True,
            "is_updatable": True
        }
    )

    # Exit profitability threshold (spread to close positions)
    # TARGET (PRODUCTION): 0.000125 = 0.0125% = $15 at $120k BTC
    # TESTING: 0.00058 = 0.058% = $70 at $120k BTC
    exit_profitability: Decimal = Field(
        default=Decimal("0.00058"),
        gt=0,
        json_schema_extra={
            "prompt": "Enter exit profitability threshold (e.g., 0.000125 for $15 spread at $120k, testing: 0.00058 for $70): ",
            "prompt_on_new": True,
            "is_updatable": True
        }
    )

    # Gas/Fee conversion price (for AMM connectors)
    gas_conversion_price: Decimal = Field(
        default=Decimal("3000"),
        gt=0,
        json_schema_extra={
            "prompt": "Enter gas token price in USD (e.g., 3000 for ETH): ",
            "prompt_on_new": False,
            "is_updatable": True
        }
    )

    # Cooldown period between arbitrage executions
    cooldown_time: int = Field(
        default=60,
        gt=0,
        json_schema_extra={
            "prompt": "Enter cooldown time in seconds between arbitrage executions (e.g., 60): ",
            "prompt_on_new": True,
            "is_updatable": True
        }
    )

    # Maximum number of concurrent arbitrage executors
    max_concurrent_executors: int = Field(
        default=1,
        gt=0,
        json_schema_extra={
            "prompt": "Enter maximum number of concurrent arbitrage executors (e.g., 1): ",
            "prompt_on_new": True,
            "is_updatable": True
        }
    )

    # Order type (MARKET or LIMIT)
    order_type: OrderType = Field(
        default=OrderType.LIMIT,
        json_schema_extra={
            "prompt": "Enter order type (MARKET/LIMIT, LIMIT for maker fees): ",
            "prompt_on_new": True,
            "is_updatable": True
        }
    )

    # Leverage for perpetual trading
    leverage: int = Field(
        default=1,
        gt=0,
        le=100,
        json_schema_extra={
            "prompt": "Enter leverage for perpetual trading (1-100, use 1 for no leverage): ",
            "prompt_on_new": True,
            "is_updatable": False
        }
    )

    # Stop loss per position (unrealized PnL threshold)
    stop_loss_pct: Decimal = Field(
        default=Decimal("1.0"),
        gt=0,
        json_schema_extra={
            "prompt": "Enter stop loss percentage per position (1.0 = 100% disabled, 0.10 = 10%): ",
            "prompt_on_new": False,
            "is_updatable": True
        }
    )

    # Stop total PnL (cumulative loss threshold)
    max_loss_pct: Decimal = Field(
        default=Decimal("0.05"),
        gt=0,
        json_schema_extra={
            "prompt": "Enter max cumulative loss percentage (0.05 = 5% stops strategy): ",
            "prompt_on_new": False,
            "is_updatable": True
        }
    )

    # Partial fill timeout
    partial_fill_timeout: int = Field(
        default=60,
        gt=0,
        json_schema_extra={
            "prompt": "Enter partial fill timeout in seconds (60 = wait 60s then convert to MARKET): ",
            "prompt_on_new": False,
            "is_updatable": True
        }
    )

    @field_validator("entry_profitability", "exit_profitability", "order_amount", "gas_conversion_price", "stop_loss_pct", "max_loss_pct", mode="before")
    @classmethod
    def validate_decimals(cls, v):
        if isinstance(v, str):
            return Decimal(v) if v else None
        return v

    @field_validator('order_type', mode="before")
    @classmethod
    def validate_order_type(cls, v) -> OrderType:
        if isinstance(v, OrderType):
            return v
        elif v is None:
            return OrderType.LIMIT
        elif isinstance(v, str):
            cleaned_str = v.replace("OrderType.", "").upper()
            if cleaned_str in OrderType.__members__:
                return OrderType[cleaned_str]
        elif isinstance(v, int):
            try:
                return OrderType(v)
            except ValueError:
                pass
        raise ValueError(f"Invalid order type: {v}. Valid options are: {', '.join(OrderType.__members__)}")

    def update_markets(self, markets: MarketDict) -> MarketDict:
        markets = markets.add_or_update(self.connector_1, self.trading_pair_1)
        markets = markets.add_or_update(self.connector_2, self.trading_pair_2)
        return markets


class PerpArbitrageController(ControllerBase):
    """
    Perpetual-to-Perpetual Arbitrage Controller

    Monitors price differences between two perpetual futures exchanges and executes
    arbitrage trades when the price gap exceeds the minimum profitability threshold.

    Strategy Logic:
    1. Continuously monitor prices on both exchanges
    2. Calculate price difference (spread) between exchanges
    3. When spread > min_profitability + fees, execute arbitrage:
       - Buy on cheaper exchange
       - Sell on expensive exchange
    4. Manage positions and risk through executor
    """

    def __init__(self, config: PerpArbitrageControllerConfig, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config

        # Initialize market data for both exchanges
        self.market_data_provider.initialize_rate_sources([
            ConnectorPair(connector_name=config.connector_1, trading_pair=config.trading_pair_1),
            ConnectorPair(connector_name=config.connector_2, trading_pair=config.trading_pair_2)
        ])

        # Track last arbitrage execution time
        self._last_execution_time = 0

        # Price data
        self._exchange_1_price = Decimal("0")
        self._exchange_2_price = Decimal("0")
        self._price_spread_pct = Decimal("0")
        self._profitability = Decimal("0")

        # Total PnL tracking (cumulative across all trades)
        self._total_pnl_quote = Decimal("0")
        self._total_volume_quote = Decimal("0")
        self._total_trades = 0
        self._strategy_stopped = False

    async def update_processed_data(self):
        """
        Update price data and calculate arbitrage opportunities.
        """
        try:
            # Get prices from both exchanges concurrently
            price_1_task = asyncio.create_task(
                self._get_price(self.config.connector_1, self.config.trading_pair_1)
            )
            price_2_task = asyncio.create_task(
                self._get_price(self.config.connector_2, self.config.trading_pair_2)
            )

            self._exchange_1_price, self._exchange_2_price = await asyncio.gather(
                price_1_task, price_2_task
            )

            # Calculate spread and profitability
            if self._exchange_1_price > 0 and self._exchange_2_price > 0:
                # Calculate percentage spread
                self._price_spread_pct = (
                    (self._exchange_2_price - self._exchange_1_price) / self._exchange_1_price
                )

                # Use absolute spread for comparison
                self._profitability = abs(self._price_spread_pct)

            # Store processed data
            self.processed_data = {
                "exchange_1_price": self._exchange_1_price,
                "exchange_2_price": self._exchange_2_price,
                "spread_pct": self._price_spread_pct * 100,  # Convert to percentage
                "profitability": self._profitability,
                "signal": 1 if self._profitability > self.config.entry_profitability else 0
            }

        except Exception as e:
            self.logger().error(f"Error updating processed data: {e}")
            self.processed_data = {
                "exchange_1_price": Decimal("0"),
                "exchange_2_price": Decimal("0"),
                "spread_pct": Decimal("0"),
                "profitability": Decimal("0"),
                "signal": 0
            }

    async def _get_price(self, connector_name: str, trading_pair: str) -> Decimal:
        """Get mid price for a trading pair on a connector."""
        try:
            return self.market_data_provider.get_price_by_type(
                connector_name, trading_pair, PriceType.MidPrice
            )
        except Exception as e:
            self.logger().error(f"Error getting price for {connector_name}:{trading_pair}: {e}")
            return Decimal("0")

    def determine_executor_actions(self) -> List[ExecutorAction]:
        """
        Determine if arbitrage opportunity exists and create executor actions.
        """
        actions = []

        # Update total PnL from completed executors
        self._update_total_pnl()

        # Check if strategy should stop due to total PnL loss
        if self._strategy_stopped:
            self.logger().warning(f"Strategy stopped due to total PnL loss exceeding {self.config.max_loss_pct:.1%}")
            return []

        # Check if we can create new arbitrage executor
        if self._can_execute_arbitrage():
            actions.append(self._create_arbitrage_action())

        return actions

    def _update_total_pnl(self):
        """
        Update total PnL from all completed executors and check if strategy should stop.
        This is called AFTER each trade completes (not continuously during trades).
        """
        completed_executors = self.filter_executors(
            executors=self.executors_info,
            filter_func=lambda x: x.is_done
        )

        total_pnl = Decimal("0")
        total_volume = Decimal("0")

        for executor in completed_executors:
            total_pnl += executor.net_pnl_quote
            total_volume += executor.filled_amount_quote

        self._total_pnl_quote = total_pnl
        self._total_volume_quote = total_volume
        self._total_trades = len(completed_executors)

        # Check if cumulative PnL exceeds max loss threshold
        if total_volume > 0:
            total_pnl_pct = total_pnl / total_volume

            if total_pnl_pct < -self.config.max_loss_pct:
                if not self._strategy_stopped:
                    self.logger().error(
                        f"Stop Total PnL triggered! "
                        f"Cumulative PnL: {total_pnl_pct:.4%} ({total_pnl:.2f} quote) "
                        f"< -{self.config.max_loss_pct:.4%} "
                        f"after {self._total_trades} trades. "
                        f"Strategy will stop creating new positions."
                    )
                    self._strategy_stopped = True

    def _can_execute_arbitrage(self) -> bool:
        """
        Check if conditions are met to execute arbitrage.
        """
        # Check signal
        signal = self.processed_data.get("signal", 0)
        if signal == 0:
            return False

        # Check cooldown period
        current_time = self.market_data_provider.time()
        if current_time - self._last_execution_time < self.config.cooldown_time:
            return False

        # Check maximum concurrent executors
        active_executors = self.filter_executors(
            executors=self.executors_info,
            filter_func=lambda x: x.is_active
        )
        if len(active_executors) >= self.config.max_concurrent_executors:
            return False

        return True

    def _create_arbitrage_action(self) -> CreateExecutorAction:
        """
        Create a perpetual arbitrage executor action.
        """
        # Determine which exchange to long/short based on price difference
        if self._exchange_1_price < self._exchange_2_price:
            # Long on cheaper exchange 1, short on expensive exchange 2
            long_market = ConnectorPair(
                connector_name=self.config.connector_1,
                trading_pair=self.config.trading_pair_1
            )
            short_market = ConnectorPair(
                connector_name=self.config.connector_2,
                trading_pair=self.config.trading_pair_2
            )
        else:
            # Long on cheaper exchange 2, short on expensive exchange 1
            long_market = ConnectorPair(
                connector_name=self.config.connector_2,
                trading_pair=self.config.trading_pair_2
            )
            short_market = ConnectorPair(
                connector_name=self.config.connector_1,
                trading_pair=self.config.trading_pair_1
            )

        # Create perpetual arbitrage executor config
        # Debug: log controller_id
        self.logger().info(f"🔍 DEBUG: Creating executor with controller_id={self.config.id} (type: {type(self.config.id)})")

        executor_config = PerpArbitrageExecutorConfig(
            timestamp=self.market_data_provider.time(),
            controller_id=self.config.id if self.config.id is not None else "perp_arbitrage",
            long_market=long_market,
            short_market=short_market,
            order_amount=self.config.order_amount,
            entry_profitability=self.config.entry_profitability,
            exit_profitability=self.config.exit_profitability,
            stop_loss_pct=self.config.stop_loss_pct,
            max_loss_pct=self.config.max_loss_pct,
            partial_fill_timeout=self.config.partial_fill_timeout,
            order_type=self.config.order_type,
            leverage=self.config.leverage
        )

        # Update last execution time
        self._last_execution_time = self.market_data_provider.time()

        return CreateExecutorAction(
            controller_id=self.config.id,
            executor_config=executor_config
        )

    def to_format_status(self) -> List[str]:
        """
        Format status for display.
        """
        lines = []
        lines.append("\n" + "="*80)
        lines.append("Perpetual Arbitrage Controller Status")
        lines.append("="*80)

        # Exchange prices
        lines.append(f"\n{self.config.connector_1} ({self.config.trading_pair_1}): ${self._exchange_1_price:,.2f}")
        lines.append(f"{self.config.connector_2} ({self.config.trading_pair_2}): ${self._exchange_2_price:,.2f}")

        # Spread and profitability
        lines.append(f"\nPrice Spread: {self._price_spread_pct * 100:.3f}%")
        lines.append(f"Absolute Spread: {self._profitability * 100:.3f}%")
        lines.append(f"Entry Threshold: {self.config.entry_profitability * 100:.3f}%")
        lines.append(f"Exit Threshold: {self.config.exit_profitability * 100:.3f}%")

        # Active executors
        active_executors = self.filter_executors(
            executors=self.executors_info,
            filter_func=lambda x: x.is_active
        )
        lines.append(f"\nActive Executors: {len(active_executors)}/{self.config.max_concurrent_executors}")

        # Cooldown status
        current_time = self.market_data_provider.time()
        time_since_last = current_time - self._last_execution_time
        cooldown_remaining = max(0, self.config.cooldown_time - time_since_last)
        lines.append(f"Cooldown Remaining: {cooldown_remaining:.0f}s")

        # Arbitrage signal
        signal = self.processed_data.get("signal", 0)
        signal_status = "✓ OPPORTUNITY DETECTED" if signal > 0 else "✗ No opportunity"
        lines.append(f"\nArbitrage Signal: {signal_status}")

        # Total PnL tracking
        if self._total_trades > 0:
            total_pnl_pct = self._total_pnl_quote / self._total_volume_quote if self._total_volume_quote > 0 else Decimal("0")
            lines.append(f"\nTotal Trades: {self._total_trades}")
            lines.append(f"Cumulative PnL: {total_pnl_pct:.4%} ({self._total_pnl_quote:.2f} quote)")
            lines.append(f"Strategy Status: {'STOPPED' if self._strategy_stopped else 'RUNNING'}")

        lines.append("="*80 + "\n")

        return lines
