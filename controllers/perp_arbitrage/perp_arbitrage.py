"""
Perp Arbitrage Controller
Re-exports the controller from hummingbot.strategy_v2.controllers
"""

from hummingbot.strategy_v2.controllers.perp_arbitrage_controller import (
    PerpArbitrageController,
    PerpArbitrageControllerConfig,
)

__all__ = ["PerpArbitrageController", "PerpArbitrageControllerConfig"]
