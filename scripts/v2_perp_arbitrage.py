import os
from decimal import Decimal
from typing import Dict, List, Optional, Set

from pydantic import ConfigDict, Field

from hummingbot.connector.connector_base import ConnectorBase
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy.strategy_v2_base import StrategyV2Base, StrategyV2ConfigBase
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, StopExecutorAction


class V2PerpArbitrageConfig(StrategyV2ConfigBase):
    """
    Configuration for V2 Perpetual Arbitrage Strategy Script
    """
    script_file_name: str = os.path.basename(__file__)

    # Override model_config to allow extra fields from controller configs
    model_config = ConfigDict(extra="allow")

    # Controller configurations will be loaded from YAML files
    controllers_config: List[str] = Field(
        default=["perp_arbitrage_example.yml"],
        json_schema_extra={
            "prompt": lambda mi: "Enter controller config files (comma separated, e.g., perp_arbitrage_example.yml): ",
            "prompt_on_new": True
        }
    )

    # These will be populated automatically from controller configs
    candles_config: List[CandlesConfig] = []
    markets: Dict[str, Set[str]] = {}


class V2PerpArbitrage(StrategyV2Base):
    """
    V2 Perpetual Arbitrage Strategy

    This strategy uses the PerpArbitrageController to automatically execute
    arbitrage trades between two perpetual exchanges when price differences
    exceed the minimum profitability threshold.

    Usage:
    1. Configure your controller in conf/controllers/perp_arbitrage_example.yml
    2. In Hummingbot CLI, run: start --script v2_perp_arbitrage.py
    3. The strategy will automatically monitor prices and execute arbitrage

    Features:
    - Automatic price monitoring on two exchanges
    - Risk management with cooldown periods
    - Concurrent executor limits
    - Real-time profitability calculations
    """

    # Class variable to hold markets
    markets = {}

    def __init__(self, connectors: Dict[str, ConnectorBase], config: V2PerpArbitrageConfig):
        super().__init__(connectors, config)
        self.config = config

    def create_actions_proposal(self) -> List[CreateExecutorAction]:
        """
        Controllers handle action creation, so we return empty list here.
        """
        return []

    def stop_actions_proposal(self) -> List[StopExecutorAction]:
        """
        Controllers handle stop actions, so we return empty list here.
        """
        return []

    def apply_initial_setting(self):
        """
        Apply initial settings like leverage and position mode for perpetual connectors.
        """
        for controller_id, controller in self.controllers.items():
            config_dict = controller.config.model_dump()

            # Set leverage for both connectors
            if "leverage" in config_dict:
                leverage = config_dict["leverage"]

                # Set leverage for connector 1
                if "connector_1" in config_dict:
                    connector_1 = config_dict["connector_1"]
                    if self.is_perpetual(connector_1):
                        trading_pair_1 = config_dict.get("trading_pair_1")
                        self.connectors[connector_1].set_leverage(
                            leverage=leverage,
                            trading_pair=trading_pair_1
                        )
                        self.logger().info(f"Set leverage {leverage}x for {connector_1}:{trading_pair_1}")

                # Set leverage for connector 2
                if "connector_2" in config_dict:
                    connector_2 = config_dict["connector_2"]
                    if self.is_perpetual(connector_2):
                        trading_pair_2 = config_dict.get("trading_pair_2")
                        self.connectors[connector_2].set_leverage(
                            leverage=leverage,
                            trading_pair=trading_pair_2
                        )
                        self.logger().info(f"Set leverage {leverage}x for {connector_2}:{trading_pair_2}")

            # Set position mode to HEDGE for both connectors
            if "connector_1" in config_dict:
                connector_1 = config_dict["connector_1"]
                if self.is_perpetual(connector_1):
                    from hummingbot.core.data_type.common import PositionMode
                    self.connectors[connector_1].set_position_mode(PositionMode.HEDGE)
                    self.logger().info(f"Set HEDGE position mode for {connector_1}")

            if "connector_2" in config_dict:
                connector_2 = config_dict["connector_2"]
                if self.is_perpetual(connector_2):
                    from hummingbot.core.data_type.common import PositionMode
                    self.connectors[connector_2].set_position_mode(PositionMode.HEDGE)
                    self.logger().info(f"Set HEDGE position mode for {connector_2}")

    def on_tick(self):
        """
        Called on each strategy tick. Controllers are updated automatically by the base class.
        """
        super().on_tick()
