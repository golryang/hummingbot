import asyncio
import logging
from decimal import Decimal
from typing import Dict, Union

from hummingbot.connector.utils import split_hb_trading_pair
from hummingbot.core.data_type.common import OrderType, PositionAction, TradeType
from hummingbot.core.event.events import (
    BuyOrderCreatedEvent,
    MarketOrderFailureEvent,
    OrderFilledEvent,
    SellOrderCreatedEvent,
)
from hummingbot.logger import HummingbotLogger
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase
from hummingbot.strategy_v2.executors.executor_base import ExecutorBase
from hummingbot.strategy_v2.executors.perp_arbitrage_executor.data_types import PerpArbitrageExecutorConfig
from hummingbot.strategy_v2.models.base import RunnableStatus
from hummingbot.strategy_v2.models.executors import CloseType, TrackedOrder


class PerpArbitrageExecutor(ExecutorBase):
    """
    Perpetual Arbitrage Executor with Position Holding

    Workflow:
    1. Entry: Open long position on one exchange, short on another when spread > entry_profitability
    2. Monitor: Continuously monitor spread between exchanges
    3. Exit: Close both positions when spread < exit_profitability
    4. Complete: Return to controller for next opportunity

    This is designed for perpetual futures arbitrage where positions are held until exit conditions are met.
    """
    _logger = None

    @classmethod
    def logger(cls) -> HummingbotLogger:
        if cls._logger is None:
            cls._logger = logging.getLogger(__name__)
        return cls._logger

    def __init__(self,
                 strategy: ScriptStrategyBase,
                 config: PerpArbitrageExecutorConfig,
                 update_interval: float = 1.0,
                 max_retries: int = 3):
        super().__init__(strategy=strategy,
                         connectors=[config.long_market.connector_name, config.short_market.connector_name],
                         config=config,
                         update_interval=update_interval)
        self.config = config
        self.long_market = config.long_market
        self.short_market = config.short_market
        self.order_amount = config.order_amount
        self.entry_profitability = config.entry_profitability
        self.exit_profitability = config.exit_profitability
        self.max_retries = max_retries

        # Position tracking
        self._long_order: TrackedOrder = TrackedOrder()  # Entry long
        self._short_order: TrackedOrder = TrackedOrder()  # Entry short
        self._close_long_order: TrackedOrder = TrackedOrder()  # Exit long (sell)
        self._close_short_order: TrackedOrder = TrackedOrder()  # Exit short (buy)

        # Price tracking
        self._long_price = Decimal("0")
        self._short_price = Decimal("0")
        self._current_spread_pct = Decimal("0")
        self._entry_spread_pct = Decimal("0")

        # State tracking
        self._position_opened = False
        self._position_closing = False
        self._opening_positions = False  # Lock to prevent race condition
        self._cumulative_failures = 0

        # Retry tracking per order
        self._long_order_retries = 0
        self._short_order_retries = 0
        self._close_long_retries = 0
        self._close_short_retries = 0

        # PnL tracking
        self._entry_long_price = Decimal("0")
        self._entry_short_price = Decimal("0")
        self._exit_long_price = Decimal("0")
        self._exit_short_price = Decimal("0")

        # Stop loss check interval (check every N seconds to reduce overhead)
        self._stop_loss_check_interval = 10  # Check every 10 seconds
        self._last_stop_loss_check = 0

        # Timestamp tracking for fill time analysis
        self._long_order_placed_time = 0
        self._short_order_placed_time = 0
        self._long_fill_time = 0
        self._short_fill_time = 0
        self._close_long_placed_time = 0
        self._close_short_placed_time = 0
        self._close_long_fill_time = 0
        self._close_short_fill_time = 0

        # Partial fill tracking
        self._first_fill_time = 0  # When first order fills (timeout starts)
        self._partial_fill_detected = False
        self._market_recovery_used = False  # Track if we had to use MARKET

    async def validate_sufficient_balance(self) -> bool:
        """
        Validate that both exchanges have sufficient balance for the arbitrage trade.
        Returns True if sufficient, False otherwise.

        For perpetual futures, both long and short positions require quote asset (USDT/USDC) as margin.
        """
        try:
            # Get available balance on long market
            long_connector = self.connectors[self.long_market.connector_name]
            long_quote_asset = self.long_market.trading_pair.split("-")[1]
            long_balance = long_connector.get_available_balance(long_quote_asset)

            # Get available balance on short market
            # For perpetuals, short positions also need quote asset (margin)
            short_connector = self.connectors[self.short_market.connector_name]
            short_quote_asset = self.short_market.trading_pair.split("-")[1]
            short_balance = short_connector.get_available_balance(short_quote_asset)

            # Calculate required amounts (considering leverage)
            leverage = self.config.leverage if self.config.leverage > 0 else 1
            required_long_quote = self.order_amount * self._long_price / Decimal(leverage)
            required_short_quote = self.order_amount * self._short_price / Decimal(leverage)

            # Check if balances are sufficient
            sufficient = True

            if long_balance < required_long_quote:
                self.logger().error(
                    f"❌ Insufficient balance on {self.long_market.connector_name} for long position. "
                    f"Required: {required_long_quote:.2f} {long_quote_asset}, Available: {long_balance:.2f}. "
                    f"Check for existing positions or pending orders!"
                )
                sufficient = False

            if short_balance < required_short_quote:
                self.logger().error(
                    f"❌ Insufficient balance on {self.short_market.connector_name} for short position. "
                    f"Required: {required_short_quote:.2f} {short_quote_asset}, Available: {short_balance:.2f}. "
                    f"Check for existing positions or pending orders!"
                )
                sufficient = False

            if not sufficient:
                self.logger().error(
                    "⚠️ STOPPING EXECUTOR: Insufficient funds. "
                    "Possible causes: 1) Existing open positions, 2) Pending orders, 3) Insufficient margin"
                )
                self.close_type = CloseType.INSUFFICIENT_BALANCE
                self.stop()
            else:
                self.logger().info(
                    f"✅ Balance check passed. "
                    f"{self.long_market.connector_name}: {long_balance:.2f} {long_quote_asset}, "
                    f"{self.short_market.connector_name}: {short_balance:.2f} {short_quote_asset}"
                )

            return sufficient

        except Exception as e:
            self.logger().error(f"Error validating balance: {e}")
            return False

    async def control_task(self):
        """
        Main control loop:
        1. RUNNING: Position not opened yet, monitoring for entry
        2. Position opened: Monitoring for exit
        3. SHUTTING_DOWN: Closing positions
        """
        if self.status == RunnableStatus.RUNNING:
            try:
                await self.update_prices()

                if not self._position_opened and not self._opening_positions:
                    # Entry logic already handled by controller, just open positions
                    # Use lock flag to prevent race condition from multiple ticks
                    self._opening_positions = True
                    await self.open_positions()
                elif self._position_opened and not self._position_closing:
                    # Monitor entry orders for partial fills
                    long_filled = self._long_order.order and self._long_order.order.is_filled
                    short_filled = self._short_order.order and self._short_order.order.is_filled
                    current_time = self._strategy.current_timestamp

                    # Partial fill detection for entry orders
                    if long_filled and not short_filled:
                        # Long filled, short not filled
                        if self._first_fill_time == 0:
                            self._first_fill_time = current_time
                            self.logger().info(f"⏱️ Long filled first. Starting {self.config.partial_fill_timeout}s timeout for Short...")

                        elapsed = current_time - self._first_fill_time
                        if elapsed >= self.config.partial_fill_timeout:
                            self.logger().warning(
                                f"⏰ Timeout exceeded ({elapsed:.1f}s). Converting Short order to MARKET"
                            )
                            self._partial_fill_detected = True
                            # Cancel existing short order ONLY if it's still open
                            short_order = self._strategy.get_order(
                                self.short_market.connector_name,
                                self._short_order.order_id
                            )
                            if short_order and short_order.is_open:
                                self._strategy.cancel(
                                    connector_name=self.short_market.connector_name,
                                    trading_pair=self.short_market.trading_pair,
                                    order_id=self._short_order.order_id
                                )
                                # Only place new order if cancel succeeded
                                self.place_short_order(force_market=True)
                            else:
                                self.logger().info(f"Short order already filled/cancelled, skipping MARKET conversion")

                    elif short_filled and not long_filled:
                        # Short filled, long not filled
                        if self._first_fill_time == 0:
                            self._first_fill_time = current_time
                            self.logger().info(f"⏱️ Short filled first. Starting {self.config.partial_fill_timeout}s timeout for Long...")

                        elapsed = current_time - self._first_fill_time
                        if elapsed >= self.config.partial_fill_timeout:
                            self.logger().warning(
                                f"⏰ Timeout exceeded ({elapsed:.1f}s). Converting Long order to MARKET"
                            )
                            self._partial_fill_detected = True
                            # Cancel existing long order ONLY if it's still open
                            long_order = self._strategy.get_order(
                                self.long_market.connector_name,
                                self._long_order.order_id
                            )
                            if long_order and long_order.is_open:
                                self._strategy.cancel(
                                    connector_name=self.long_market.connector_name,
                                    trading_pair=self.long_market.trading_pair,
                                    order_id=self._long_order.order_id
                                )
                                # Only place new order if cancel succeeded
                                self.place_long_order(force_market=True)
                            else:
                                self.logger().info(f"Long order already filled/cancelled, skipping MARKET conversion")

                    elif long_filled and short_filled:
                        # Both filled - check exit conditions
                        # Check stop loss periodically (every 10s to reduce overhead)
                        # Note: For hedge strategies, stop loss is rarely needed
                        # Only triggers if stop_loss_pct < 100% (enabled)
                        if self.config.stop_loss_pct < Decimal("1.0"):
                            if current_time - self._last_stop_loss_check >= self._stop_loss_check_interval:
                                current_pnl_pct = self._calculate_unrealized_pnl_pct()
                                self._last_stop_loss_check = current_time

                                if current_pnl_pct < -self.config.stop_loss_pct:
                                    self.logger().warning(f"Stop loss triggered: Unrealized PnL {current_pnl_pct:.4%} < -{self.config.stop_loss_pct:.4%}")
                                    self.close_type = CloseType.STOP_LOSS
                                    await self.close_positions()
                                    return

                        # Check normal exit condition (spread-based, checked every tick)
                        if abs(self._current_spread_pct) <= self.exit_profitability:
                            self.logger().info(f"Exit condition met: spread {abs(self._current_spread_pct):.4%} <= {self.exit_profitability:.4%}")
                            await self.close_positions()
            except Exception as e:
                self.logger().error(f"Error in control task: {e}", exc_info=True)

        elif self.status == RunnableStatus.SHUTTING_DOWN:
            if self._cumulative_failures > self.max_retries:
                self.close_type = CloseType.FAILED
                self.stop()
            else:
                # Partial fill detection for close orders
                close_long_filled = self._close_long_order.order and self._close_long_order.order.is_filled
                close_short_filled = self._close_short_order.order and self._close_short_order.order.is_filled
                current_time = self._strategy.current_timestamp

                if close_long_filled and not close_short_filled:
                    # Close long filled, close short not filled
                    if self._first_fill_time == 0:
                        self._first_fill_time = current_time
                        self.logger().info(f"⏱️ Close Long filled first. Starting {self.config.partial_fill_timeout}s timeout for Close Short...")

                    elapsed = current_time - self._first_fill_time
                    if elapsed >= self.config.partial_fill_timeout:
                        self.logger().warning(
                            f"⏰ Timeout exceeded ({elapsed:.1f}s). Converting Close Short order to MARKET"
                        )
                        self._partial_fill_detected = True
                        # Cancel existing close short order ONLY if it's still open
                        close_short_order = self._strategy.get_order(
                            self.short_market.connector_name,
                            self._close_short_order.order_id
                        )
                        if close_short_order and close_short_order.is_open:
                            self._strategy.cancel(
                                connector_name=self.short_market.connector_name,
                                trading_pair=self.short_market.trading_pair,
                                order_id=self._close_short_order.order_id
                            )
                            # Only place new order if cancel succeeded
                            self.place_close_short_order(force_market=True)
                        else:
                            self.logger().info(f"Close Short order already filled/cancelled, skipping MARKET conversion")

                elif close_short_filled and not close_long_filled:
                    # Close short filled, close long not filled
                    if self._first_fill_time == 0:
                        self._first_fill_time = current_time
                        self.logger().info(f"⏱️ Close Short filled first. Starting {self.config.partial_fill_timeout}s timeout for Close Long...")

                    elapsed = current_time - self._first_fill_time
                    if elapsed >= self.config.partial_fill_timeout:
                        self.logger().warning(
                            f"⏰ Timeout exceeded ({elapsed:.1f}s). Converting Close Long order to MARKET"
                        )
                        self._partial_fill_detected = True
                        # Cancel existing close long order ONLY if it's still open
                        close_long_order = self._strategy.get_order(
                            self.long_market.connector_name,
                            self._close_long_order.order_id
                        )
                        if close_long_order and close_long_order.is_open:
                            self._strategy.cancel(
                                connector_name=self.long_market.connector_name,
                                trading_pair=self.long_market.trading_pair,
                                order_id=self._close_long_order.order_id
                            )
                            # Only place new order if cancel succeeded
                            self.place_close_long_order(force_market=True)
                        else:
                            self.logger().info(f"Close Long order already filled/cancelled, skipping MARKET conversion")

                else:
                    # Check if both are filled
                    self.check_close_orders_status()

    async def update_prices(self):
        """Update current prices and calculate spread"""
        try:
            long_price_task = asyncio.create_task(
                self._get_price(self.long_market.connector_name, self.long_market.trading_pair)
            )
            short_price_task = asyncio.create_task(
                self._get_price(self.short_market.connector_name, self.short_market.trading_pair)
            )

            self._long_price, self._short_price = await asyncio.gather(long_price_task, short_price_task)

            # Calculate spread percentage
            if self._long_price > 0 and self._short_price > 0:
                # Spread = (short_price - long_price) / long_price
                # Positive spread means we profit from long on cheap exchange, short on expensive
                self._current_spread_pct = (self._short_price - self._long_price) / self._long_price

        except Exception as e:
            self.logger().error(f"Error updating prices: {e}")

    async def _get_price(self, connector_name: str, trading_pair: str) -> Decimal:
        """Get current mid price"""
        try:
            connector = self.connectors[connector_name]
            order_book = connector.get_order_book(trading_pair)
            best_bid = Decimal(str(order_book.get_price(True)))  # best bid
            best_ask = Decimal(str(order_book.get_price(False)))  # best ask
            return (best_bid + best_ask) / Decimal("2")
        except Exception as e:
            self.logger().error(f"Error getting price for {connector_name}:{trading_pair}: {e}")
            return Decimal("0")

    async def open_positions(self):
        """Open long and short positions"""
        # Validate balance BEFORE placing orders
        if not await self.validate_sufficient_balance():
            self.logger().error("Cannot open positions due to insufficient balance. Executor stopped.")
            return

        # Check for existing positions BEFORE opening new ones
        try:
            long_connector = self.connectors[self.long_market.connector_name]
            short_connector = self.connectors[self.short_market.connector_name]

            # Get existing position info
            long_position = long_connector.get_position(self.long_market.trading_pair)
            short_position = short_connector.get_position(self.short_market.trading_pair)

            if long_position and abs(long_position.amount) > 0:
                self.logger().warning(
                    f"⚠️ Existing LONG position detected on {self.long_market.connector_name}: "
                    f"{long_position.amount} BTC. This may cause hedge issues!"
                )

            if short_position and abs(short_position.amount) > 0:
                self.logger().warning(
                    f"⚠️ Existing SHORT position detected on {self.short_market.connector_name}: "
                    f"{short_position.amount} BTC. This may cause hedge issues!"
                )
        except Exception as e:
            self.logger().warning(f"Could not check existing positions: {e}")

        self.logger().info(
            f"Opening positions - Long: {self.long_market.connector_name}, Short: {self.short_market.connector_name}, "
            f"Amount: {self.order_amount} BTC"
        )
        self._position_opened = True
        self._entry_spread_pct = self._current_spread_pct

        # Place long order
        self.place_long_order()
        # Place short order
        self.place_short_order()

    def place_long_order(self, force_market: bool = False):
        """
        Open long position (BUY)

        Args:
            force_market: If True, use MARKET order regardless of config (for partial fill recovery)
        """
        if force_market:
            order_type = OrderType.MARKET
            price = self._long_price
            self.logger().warning(f"🚨 MARKET order forced for Long (partial fill recovery)")
            self._market_recovery_used = True
        elif self.config.order_type == OrderType.LIMIT:
            order_type = OrderType.LIMIT
            order_book = self.connectors[self.long_market.connector_name].get_order_book(self.long_market.trading_pair)
            price = Decimal(str(order_book.get_price(False)))  # best ask
        else:
            order_type = self.config.order_type
            price = self._long_price

        # Record order placement time
        self._long_order_placed_time = self._strategy.current_timestamp

        self._long_order.order_id = self.place_order(
            connector_name=self.long_market.connector_name,
            trading_pair=self.long_market.trading_pair,
            order_type=order_type,
            side=TradeType.BUY,
            amount=self.order_amount,
            price=price,
            position_action=PositionAction.OPEN
        )
        order_type_str = "MARKET" if force_market else str(order_type)
        self.logger().info(f"Placed LONG {order_type_str} order on {self.long_market.connector_name}: {self._long_order.order_id}")

    def place_short_order(self, force_market: bool = False):
        """
        Open short position (SELL)

        Args:
            force_market: If True, use MARKET order regardless of config (for partial fill recovery)
        """
        if force_market:
            order_type = OrderType.MARKET
            price = self._short_price
            self.logger().warning(f"🚨 MARKET order forced for Short (partial fill recovery)")
            self._market_recovery_used = True
        elif self.config.order_type == OrderType.LIMIT:
            order_type = OrderType.LIMIT
            order_book = self.connectors[self.short_market.connector_name].get_order_book(self.short_market.trading_pair)
            price = Decimal(str(order_book.get_price(True)))  # best bid
        else:
            order_type = self.config.order_type
            price = self._short_price

        # Record order placement time
        self._short_order_placed_time = self._strategy.current_timestamp

        # DEBUG: Log exact amount being sent
        self.logger().info(
            f"🔍 DEBUG: Placing SHORT order with amount={self.order_amount} (type: {type(self.order_amount)}), "
            f"config.order_amount={self.config.order_amount}"
        )

        self._short_order.order_id = self.place_order(
            connector_name=self.short_market.connector_name,
            trading_pair=self.short_market.trading_pair,
            order_type=order_type,
            side=TradeType.SELL,
            amount=self.order_amount,
            price=price,
            position_action=PositionAction.OPEN
        )
        order_type_str = "MARKET" if force_market else str(order_type)
        self.logger().info(f"Placed SHORT {order_type_str} order on {self.short_market.connector_name}: {self._short_order.order_id}")

    async def close_positions(self):
        """Close both positions"""
        self.logger().info(f"Closing positions - Spread: {self._current_spread_pct:.4%}")
        self._position_closing = True
        self._status = RunnableStatus.SHUTTING_DOWN

        # Close long position (SELL)
        self.place_close_long_order()
        # Close short position (BUY)
        self.place_close_short_order()

    def place_close_long_order(self, force_market: bool = False):
        """
        Close long position (SELL)

        Args:
            force_market: If True, use MARKET order regardless of config (for partial fill recovery)
        """
        if force_market:
            order_type = OrderType.MARKET
            price = self._long_price
            self.logger().warning(f"🚨 MARKET order forced for Close Long (partial fill recovery)")
            self._market_recovery_used = True
        elif self.config.order_type == OrderType.LIMIT:
            order_type = OrderType.LIMIT
            order_book = self.connectors[self.long_market.connector_name].get_order_book(self.long_market.trading_pair)
            price = Decimal(str(order_book.get_price(True)))  # best bid
        else:
            order_type = self.config.order_type
            price = self._long_price

        # Record order placement time
        self._close_long_placed_time = self._strategy.current_timestamp

        self._close_long_order.order_id = self.place_order(
            connector_name=self.long_market.connector_name,
            trading_pair=self.long_market.trading_pair,
            order_type=order_type,
            side=TradeType.SELL,
            amount=self.order_amount,
            price=price,
            position_action=PositionAction.CLOSE
        )
        order_type_str = "MARKET" if force_market else str(order_type)
        self.logger().info(f"Placed CLOSE LONG {order_type_str} order on {self.long_market.connector_name}: {self._close_long_order.order_id}")

    def place_close_short_order(self, force_market: bool = False):
        """
        Close short position (BUY)

        Args:
            force_market: If True, use MARKET order regardless of config (for partial fill recovery)
        """
        if force_market:
            order_type = OrderType.MARKET
            price = self._short_price
            self.logger().warning(f"🚨 MARKET order forced for Close Short (partial fill recovery)")
            self._market_recovery_used = True
        elif self.config.order_type == OrderType.LIMIT:
            order_type = OrderType.LIMIT
            order_book = self.connectors[self.short_market.connector_name].get_order_book(self.short_market.trading_pair)
            price = Decimal(str(order_book.get_price(False)))  # best ask
        else:
            order_type = self.config.order_type
            price = self._short_price

        # Record order placement time
        self._close_short_placed_time = self._strategy.current_timestamp

        self._close_short_order.order_id = self.place_order(
            connector_name=self.short_market.connector_name,
            trading_pair=self.short_market.trading_pair,
            order_type=order_type,
            side=TradeType.BUY,
            amount=self.order_amount,
            price=price,
            position_action=PositionAction.CLOSE
        )
        order_type_str = "MARKET" if force_market else str(order_type)
        self.logger().info(f"Placed CLOSE SHORT {order_type_str} order on {self.short_market.connector_name}: {self._close_short_order.order_id}")

    def check_close_orders_status(self):
        """Check if both close orders are filled"""
        close_long_filled = self._close_long_order.order and self._close_long_order.order.is_filled
        close_short_filled = self._close_short_order.order and self._close_short_order.order.is_filled

        if close_long_filled and close_short_filled:
            self.close_type = CloseType.COMPLETED
            self.logger().info("Both positions closed successfully")
            self.stop()

    def process_order_created_event(self, _, market, event: Union[BuyOrderCreatedEvent, SellOrderCreatedEvent]):
        """Track order creation and reset retry counter"""
        if self._long_order.order_id == event.order_id:
            self._long_order.order = self.get_in_flight_order(self.long_market.connector_name, event.order_id)
            self._long_order_retries = 0  # Reset on successful creation
            self.logger().info("Long order created")
        elif self._short_order.order_id == event.order_id:
            self._short_order.order = self.get_in_flight_order(self.short_market.connector_name, event.order_id)
            self._short_order_retries = 0  # Reset on successful creation
            self.logger().info("Short order created")
        elif self._close_long_order.order_id == event.order_id:
            self._close_long_order.order = self.get_in_flight_order(self.long_market.connector_name, event.order_id)
            self._close_long_retries = 0  # Reset on successful creation
            self.logger().info("Close long order created")
        elif self._close_short_order.order_id == event.order_id:
            self._close_short_order.order = self.get_in_flight_order(self.short_market.connector_name, event.order_id)
            self._close_short_retries = 0  # Reset on successful creation
            self.logger().info("Close short order created")

    def process_order_filled_event(self, _, market, event: OrderFilledEvent):
        """Track order fills and record prices with detailed timing"""
        current_time = self._strategy.current_timestamp

        if self._long_order.order_id == event.order_id:
            self._entry_long_price = event.price
            self._long_fill_time = current_time
            elapsed = self._long_fill_time - self._long_order_placed_time if self._long_order_placed_time > 0 else 0
            market_flag = " [MARKET RECOVERY]" if self._market_recovery_used else ""
            self.logger().info(
                f"✅ Long position opened at ${self._entry_long_price:,.2f} "
                f"(fill time: {elapsed:.1f}s){market_flag}"
            )

        elif self._short_order.order_id == event.order_id:
            self._entry_short_price = event.price
            self._short_fill_time = current_time
            elapsed = self._short_fill_time - self._short_order_placed_time if self._short_order_placed_time > 0 else 0
            market_flag = " [MARKET RECOVERY]" if self._market_recovery_used else ""
            self.logger().info(
                f"✅ Short position opened at ${self._entry_short_price:,.2f} "
                f"(fill time: {elapsed:.1f}s){market_flag}"
            )

        elif self._close_long_order.order_id == event.order_id:
            self._exit_long_price = event.price
            self._close_long_fill_time = current_time
            elapsed = self._close_long_fill_time - self._close_long_placed_time if self._close_long_placed_time > 0 else 0
            market_flag = " [MARKET RECOVERY]" if self._market_recovery_used else ""
            self.logger().info(
                f"✅ Long position closed at ${self._exit_long_price:,.2f} "
                f"(fill time: {elapsed:.1f}s){market_flag}"
            )

        elif self._close_short_order.order_id == event.order_id:
            self._exit_short_price = event.price
            self._close_short_fill_time = current_time
            elapsed = self._close_short_fill_time - self._close_short_placed_time if self._close_short_placed_time > 0 else 0
            market_flag = " [MARKET RECOVERY]" if self._market_recovery_used else ""
            self.logger().info(
                f"✅ Short position closed at ${self._exit_short_price:,.2f} "
                f"(fill time: {elapsed:.1f}s){market_flag}"
            )

    def process_order_failed_event(self, _, market, event: MarketOrderFailureEvent):
        """Handle order failures with retry limits"""
        self.logger().error(f"Order failed: {event.order_id} - {event.error_message}")
        self._cumulative_failures += 1

        # Check for fatal errors that should not retry
        fatal_errors = ["Insufficient margin", "Insufficient balance", "asset=0", "Margin is insufficient"]
        if any(err in str(event.error_message) for err in fatal_errors):
            self.logger().error(f"Fatal error detected: {event.error_message}. Stopping executor.")

            # Check if we have partial position (hedge broken)
            long_filled = self._long_order.order and self._long_order.order.is_filled
            short_filled = self._short_order.order and self._short_order.order.is_filled

            if long_filled and not short_filled:
                self.logger().error(
                    "⚠️ CRITICAL: Long position opened but short failed! Hedge is BROKEN! "
                    "Manual intervention required to close long position."
                )
            elif short_filled and not long_filled:
                self.logger().error(
                    "⚠️ CRITICAL: Short position opened but long failed! Hedge is BROKEN! "
                    "Manual intervention required to close short position."
                )

            self.close_type = CloseType.INSUFFICIENT_BALANCE
            self.stop()
            return

        # Retry failed orders with limit
        if self._long_order.order_id == event.order_id and not self._position_closing:
            self._long_order_retries += 1
            if self._long_order_retries <= self.max_retries:
                self.logger().warning(f"Retrying long order ({self._long_order_retries}/{self.max_retries})")
                self.place_long_order()
            else:
                self.logger().error(f"Max retries exceeded for long order. Stopping.")
                self.close_type = CloseType.FAILED
                self.stop()
        elif self._short_order.order_id == event.order_id and not self._position_closing:
            self._short_order_retries += 1
            if self._short_order_retries <= self.max_retries:
                self.logger().warning(f"Retrying short order ({self._short_order_retries}/{self.max_retries})")
                self.place_short_order()
            else:
                self.logger().error(f"Max retries exceeded for short order. Stopping.")
                self.close_type = CloseType.FAILED
                self.stop()
        elif self._close_long_order.order_id == event.order_id:
            self._close_long_retries += 1
            if self._close_long_retries <= self.max_retries:
                self.logger().warning(f"Retrying close long order ({self._close_long_retries}/{self.max_retries})")
                self.place_close_long_order()
            else:
                self.logger().error(f"Max retries exceeded for close long order. Stopping.")
                self.close_type = CloseType.FAILED
                self.stop()
        elif self._close_short_order.order_id == event.order_id:
            self._close_short_retries += 1
            if self._close_short_retries <= self.max_retries:
                self.logger().warning(f"Retrying close short order ({self._close_short_retries}/{self.max_retries})")
                self.place_close_short_order()
            else:
                self.logger().error(f"Max retries exceeded for close short order. Stopping.")
                self.close_type = CloseType.FAILED
                self.stop()

    def get_net_pnl_quote(self) -> Decimal:
        """Calculate net PnL"""
        if self.close_type == CloseType.COMPLETED:
            # Long PnL: (exit_price - entry_price) * amount
            long_pnl = (self._exit_long_price - self._entry_long_price) * self.order_amount
            # Short PnL: (entry_price - exit_price) * amount
            short_pnl = (self._entry_short_price - self._exit_short_price) * self.order_amount
            # Total PnL
            total_pnl = long_pnl + short_pnl
            # Subtract fees
            return total_pnl - self.cum_fees_quote
        return Decimal("0")

    def get_net_pnl_pct(self) -> Decimal:
        """Calculate net PnL percentage"""
        if self.is_closed and self._entry_long_price > 0:
            return self.net_pnl_quote / (self._entry_long_price * self.order_amount)
        return Decimal("0")

    def _calculate_unrealized_pnl_pct(self) -> Decimal:
        """
        Calculate unrealized PnL percentage based on current market prices.
        Used for real-time stop loss monitoring.
        """
        if self._entry_long_price == 0 or self._entry_short_price == 0:
            return Decimal("0")

        if self._long_price == 0 or self._short_price == 0:
            return Decimal("0")

        # Long PnL: (current_price - entry_price) * amount
        long_pnl = (self._long_price - self._entry_long_price) * self.order_amount

        # Short PnL: (entry_price - current_price) * amount
        short_pnl = (self._entry_short_price - self._short_price) * self.order_amount

        # Total unrealized PnL
        total_pnl = long_pnl + short_pnl

        # PnL percentage (based on long position notional value)
        pnl_pct = total_pnl / (self._entry_long_price * self.order_amount)

        return pnl_pct

    def get_cum_fees_quote(self) -> Decimal:
        """Calculate cumulative fees"""
        fees = Decimal("0")
        fees += self._long_order.cum_fees_quote
        fees += self._short_order.cum_fees_quote
        fees += self._close_long_order.cum_fees_quote
        fees += self._close_short_order.cum_fees_quote
        return fees

    @property
    def long_order(self) -> TrackedOrder:
        return self._long_order

    @property
    def short_order(self) -> TrackedOrder:
        return self._short_order

    @property
    def close_long_order(self) -> TrackedOrder:
        return self._close_long_order

    @property
    def close_short_order(self) -> TrackedOrder:
        return self._close_short_order

    def get_custom_info(self) -> Dict:
        return {
            "long_exchange": self.long_market.connector_name,
            "short_exchange": self.short_market.connector_name,
            "long_pair": self.long_market.trading_pair,
            "short_pair": self.short_market.trading_pair,
            "long_price": self._long_price,
            "short_price": self._short_price,
            "current_spread_pct": self._current_spread_pct * 100,
            "entry_spread_pct": self._entry_spread_pct * 100,
            "exit_threshold_pct": self.exit_profitability * 100,
            "position_opened": self._position_opened,
            "position_closing": self._position_closing,
            "failures": self._cumulative_failures,
        }

    def to_format_status(self):
        """Format status for display"""
        lines = []
        lines.append(f"""
    Perp Arbitrage Status: {self.status} | Close Type: {self.close_type}
    - LONG: {self.long_market.connector_name}:{self.long_market.trading_pair} @ ${self._long_price:,.2f}
    - SHORT: {self.short_market.connector_name}:{self.short_market.trading_pair} @ ${self._short_price:,.2f}
    - Current Spread: {self._current_spread_pct * 100:.3f}% | Exit Threshold: {self.exit_profitability * 100:.3f}%
    - Position: {'OPENED' if self._position_opened else 'NOT OPENED'} | Closing: {'YES' if self._position_closing else 'NO'}
    -------------------------------------------------------------------------------
    """)
        if self.close_type == CloseType.COMPLETED:
            lines.append(f"Net PnL: {self.net_pnl_quote:.4f} ({self.net_pnl_pct * 100:.2f}%)")
        return lines

    def early_stop(self):
        """
        Called when strategy stops early. Cancel all open orders and close positions.
        """
        self.logger().info("Early stop triggered. Canceling open orders and closing positions...")

        # Cancel all open orders
        if self._long_order.order and self._long_order.order.is_open:
            self._strategy.cancel(
                connector_name=self.long_market.connector_name,
                trading_pair=self.long_market.trading_pair,
                order_id=self._long_order.order_id
            )

        if self._short_order.order and self._short_order.order.is_open:
            self._strategy.cancel(
                connector_name=self.short_market.connector_name,
                trading_pair=self.short_market.trading_pair,
                order_id=self._short_order.order_id
            )

        if self._close_long_order.order and self._close_long_order.order.is_open:
            self._strategy.cancel(
                connector_name=self.long_market.connector_name,
                trading_pair=self.long_market.trading_pair,
                order_id=self._close_long_order.order_id
            )

        if self._close_short_order.order and self._close_short_order.order.is_open:
            self._strategy.cancel(
                connector_name=self.short_market.connector_name,
                trading_pair=self.short_market.trading_pair,
                order_id=self._close_short_order.order_id
            )

        # Check if we have open positions that need to be closed
        long_filled = self._long_order.order and self._long_order.order.is_filled
        short_filled = self._short_order.order and self._short_order.order.is_filled

        if long_filled and short_filled and not self._position_closing:
            # Force close positions with MARKET orders
            self.logger().warning("Forcing position close with MARKET orders due to early stop")
            self.close_type = CloseType.EARLY_STOP
            self._position_closing = True
            self._status = RunnableStatus.SHUTTING_DOWN
            self.place_close_long_order(force_market=True)
            self.place_close_short_order(force_market=True)
        else:
            # No positions to close, just stop
            self._status = RunnableStatus.SHUTTING_DOWN
            if not self._position_opened:
                self.close_type = CloseType.CANCELLED
            self.stop()
