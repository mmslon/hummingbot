from decimal import Decimal
from typing import List, Optional

from pydantic import Field, field_validator
from pydantic_core.core_schema import ValidationInfo

from hummingbot.core.data_type.common import PriceType
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.controllers.market_making_controller_base import (
    MarketMakingControllerBase,
    MarketMakingControllerConfigBase,
)
from hummingbot.strategy_v2.executors.position_executor.data_types import PositionExecutorConfig


class PMMPriceFollowerConfig(MarketMakingControllerConfigBase):
    """
    Configuration for PMM Price Follower Controller.

    This controller places bid/ask orders and updates them based on the price movement of another token.
    The mid-price follows a reference token with a configurable coefficient.

    Example:
        - Reference token: LTC on current exchange
        - Starting price: 10 USDT
        - Follow coefficient: 2.0
        - If LTC moves up 10%, the mid-price moves up 20% (10% * 2.0)
    """
    controller_name: str = "pmm_price_follower"
    candles_config: List[CandlesConfig] = []

    # Price Following Configuration
    reference_connector: Optional[str] = Field(
        default=None,
        json_schema_extra={
            "prompt": "Enter the connector for the reference price (leave empty to use the same connector): ",
            "prompt_on_new": True}
    )
    reference_trading_pair: str = Field(
        default="LTC-USDT",
        json_schema_extra={
            "prompt": "Enter the reference trading pair to follow (e.g., LTC-USDT, BTC-USDT): ",
            "prompt_on_new": True}
    )
    starting_price: Decimal = Field(
        default=Decimal("10.0"),
        json_schema_extra={
            "prompt": "Enter the starting mid-price for your market making orders (e.g., 10.0): ",
            "prompt_on_new": True, "is_updatable": True}
    )
    follow_coefficient: Decimal = Field(
        default=Decimal("2.0"),
        json_schema_extra={
            "prompt": "Enter the follow coefficient (e.g., 2.0 means your price moves 2x the reference token): ",
            "prompt_on_new": True, "is_updatable": True}
    )
    price_source_type: PriceType = Field(
        default=PriceType.MidPrice,
        json_schema_extra={
            "prompt": "Enter the price source type for reference (MidPrice/LastTrade/BestBid/BestAsk): ",
            "prompt_on_new": True}
    )

    # Optional: Add price boundaries to prevent extreme movements
    min_price: Optional[Decimal] = Field(
        default=None,
        json_schema_extra={
            "prompt": "Enter the minimum price limit (leave empty for no limit): ",
            "prompt_on_new": True, "is_updatable": True}
    )
    max_price: Optional[Decimal] = Field(
        default=None,
        json_schema_extra={
            "prompt": "Enter the maximum price limit (leave empty for no limit): ",
            "prompt_on_new": True, "is_updatable": True}
    )

    @field_validator("reference_connector", mode="before")
    @classmethod
    def set_reference_connector(cls, v, validation_info: ValidationInfo):
        """Use the same connector as the trading connector if not specified."""
        if v is None or v == "":
            return validation_info.data.get("connector_name")
        return v

    @field_validator("price_source_type", mode="before")
    @classmethod
    def validate_price_source_type(cls, v) -> PriceType:
        """Validate and convert price source type."""
        if isinstance(v, PriceType):
            return v
        if isinstance(v, int):
            # Handle integer values (enum values)
            try:
                return PriceType(v)
            except ValueError:
                pass
        if isinstance(v, str):
            cleaned_str = v.replace("PriceType.", "")
            # Try to match with PriceType enum members
            for member in PriceType:
                if member.name.upper() == cleaned_str.upper():
                    return member
        raise ValueError(f"Invalid price source type: {v}. Valid options are: {', '.join([pt.name for pt in PriceType])}")

    @field_validator("starting_price", "follow_coefficient", "min_price", "max_price", mode="before")
    @classmethod
    def validate_decimal_fields(cls, v):
        """Convert string values to Decimal."""
        if v is None or v == "":
            return None
        if isinstance(v, str):
            return Decimal(v)
        return v


class PMMPriceFollowerController(MarketMakingControllerBase):
    """
    Price Follower Market Making Controller.

    This controller implements a market making strategy that follows the price movement of a reference token.
    It places bid and ask orders around a calculated mid-price that moves based on the reference token's
    price changes, multiplied by a follow coefficient.

    The controller:
    1. Tracks the reference token's price on the specified exchange
    2. Calculates price change percentage from the reference token's starting price
    3. Applies the follow coefficient to amplify or dampen the price movement
    4. Updates the mid-price for market making orders accordingly
    5. Places bid/ask orders at configured spreads around the adjusted mid-price
    """

    def __init__(self, config: PMMPriceFollowerConfig, *args, **kwargs):
        self.config = config
        # Store the initial reference price (will be set on first update)
        self._initial_reference_price: Optional[Decimal] = None
        super().__init__(config, *args, **kwargs)

        # Validate trading pair format
        if "-" not in config.trading_pair:
            self.logger().warning(
                f"Trading pair '{config.trading_pair}' should contain a hyphen separator "
                f"(e.g., 'BTC-USDT' not 'BTCUSDT'). This may cause issues with some connectors."
            )
        if "-" not in config.reference_trading_pair:
            self.logger().warning(
                f"Reference trading pair '{config.reference_trading_pair}' should contain a hyphen separator "
                f"(e.g., 'BTC-USDT' not 'BTCUSDT'). This may cause issues with price fetching."
            )

    async def update_processed_data(self):
        """
        Update the processed data by calculating the new mid-price based on the reference token's price movement.

        The formula is:
        1. Get current reference price
        2. Calculate percentage change: (current_ref_price - initial_ref_price) / initial_ref_price
        3. Apply coefficient: adjusted_change = percentage_change * follow_coefficient
        4. Calculate new mid price: starting_price * (1 + adjusted_change)
        5. Apply min/max limits if configured
        """
        try:
            # Get the current reference price
            current_reference_price = self.market_data_provider.get_price_by_type(
                self.config.reference_connector,
                self.config.reference_trading_pair,
                self.config.price_source_type
            )

            if current_reference_price is None or current_reference_price <= 0:
                self.logger().warning(
                    f"Invalid reference price received: {current_reference_price} "
                    f"for {self.config.reference_trading_pair} on {self.config.reference_connector}. "
                    f"Ensure the trading pair format is correct (e.g., 'BTC-USDT' with hyphen). "
                    f"Using starting price as fallback."
                )
                reference_price = self.config.starting_price
                price_change_pct = Decimal("0")
                current_reference_price = None
            else:
                current_reference_price = Decimal(str(current_reference_price))

                # Set initial reference price on first run
                if self._initial_reference_price is None:
                    self._initial_reference_price = current_reference_price
                    self.logger().info(
                        f"Initial reference price set to {self._initial_reference_price} "
                        f"for {self.config.reference_trading_pair} on {self.config.reference_connector}"
                    )

                # Calculate percentage change from initial reference price
                price_change_pct = (current_reference_price - self._initial_reference_price) / self._initial_reference_price

                # Apply the follow coefficient
                adjusted_change_pct = price_change_pct * self.config.follow_coefficient

                # Calculate the new reference price
                reference_price = self.config.starting_price * (Decimal("1") + adjusted_change_pct)

                # Apply min/max price limits if configured
                if self.config.min_price is not None:
                    reference_price = max(reference_price, self.config.min_price)
                if self.config.max_price is not None:
                    reference_price = min(reference_price, self.config.max_price)

                self.logger().debug(
                    f"Reference: {self.config.reference_trading_pair} @ {current_reference_price:.6f} "
                    f"(change: {price_change_pct:.4%}), "
                    f"Adjusted mid-price: {reference_price:.6f} "
                    f"(adjusted change: {adjusted_change_pct:.4%})"
                )

            # Store the processed data
            self.processed_data = {
                "reference_price": reference_price,
                "spread_multiplier": Decimal("1"),  # Can be enhanced later with volatility-based spread
                "reference_token_price": current_reference_price if current_reference_price else None,
                "price_change_pct": price_change_pct if self._initial_reference_price else Decimal("0"),
            }

        except Exception as e:
            self.logger().error(
                f"Error updating processed data for price follower: {e}. "
                f"Reference: {self.config.reference_trading_pair} on {self.config.reference_connector}. "
                f"Check that: 1) Trading pair format is correct (e.g., 'BTC-USDT' with hyphen), "
                f"2) The connector supports the trading pair, "
                f"3) The connector is properly connected. "
                f"Using starting price as fallback."
            )
            # Fallback to starting price in case of errors
            self.processed_data = {
                "reference_price": self.config.starting_price,
                "spread_multiplier": Decimal("1"),
                "reference_token_price": None,
                "price_change_pct": Decimal("0"),
            }

    def get_executor_config(self, level_id: str, price: Decimal, amount: Decimal):
        """
        Create the position executor configuration for a given level.

        Args:
            level_id: The level identifier (e.g., "buy_0", "sell_1")
            price: The order price
            amount: The order amount in base asset

        Returns:
            PositionExecutorConfig with the order parameters
        """
        trade_type = self.get_trade_type_from_level_id(level_id)
        return PositionExecutorConfig(
            timestamp=self.market_data_provider.time(),
            level_id=level_id,
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            entry_price=price,
            amount=amount,
            triple_barrier_config=self.config.triple_barrier_config,
            leverage=self.config.leverage,
            side=trade_type,
        )

    def to_format_status(self) -> list:
        """
        Format the controller status for display.

        Returns:
            List of status information lines
        """
        status = []

        # Add reference token information
        if self.processed_data:
            ref_price = self.processed_data.get("reference_token_price")
            price_change = self.processed_data.get("price_change_pct", Decimal("0"))
            current_mid = self.processed_data.get("reference_price")

            status.append(f"Reference: {self.config.reference_trading_pair} on {self.config.reference_connector}")
            if ref_price:
                status.append(f"  Current Reference Price: {ref_price:.6f}")
            if self._initial_reference_price:
                status.append(f"  Initial Reference Price: {self._initial_reference_price:.6f}")
            status.append(f"  Price Change: {price_change:.2%}")
            status.append(f"  Follow Coefficient: {self.config.follow_coefficient}")
            if current_mid:
                status.append(f"  Current Mid-Price: {current_mid:.6f}")
            status.append(f"  Starting Price: {self.config.starting_price}")

        return status
