import asyncio
import time
from decimal import ROUND_DOWN, Decimal
from typing import Any, Dict, List, Optional, Tuple

from bidict import bidict

import hummingbot.connector.exchange.ourbit.ourbit_constants as CONSTANTS
import hummingbot.connector.exchange.ourbit.ourbit_utils as ourbit_utils
import hummingbot.connector.exchange.ourbit.ourbit_web_utils as web_utils
from hummingbot.connector.exchange.ourbit.ourbit_api_order_book_data_source import OurbitAPIOrderBookDataSource
from hummingbot.connector.exchange.ourbit.ourbit_api_user_stream_data_source import OurbitAPIUserStreamDataSource
from hummingbot.connector.exchange.ourbit.ourbit_auth import OurbitAuth
from hummingbot.connector.exchange_py_base import ExchangePyBase
from hummingbot.connector.trading_rule import TradingRule
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.data_type.in_flight_order import InFlightOrder, OrderState, OrderUpdate, TradeUpdate
from hummingbot.core.data_type.order_book_tracker_data_source import OrderBookTrackerDataSource
from hummingbot.core.data_type.trade_fee import TokenAmount, TradeFeeBase
from hummingbot.core.data_type.user_stream_tracker_data_source import UserStreamTrackerDataSource
from hummingbot.core.utils.estimate_fee import build_trade_fee
from hummingbot.core.web_assistant.connections.data_types import RESTMethod
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory

s_logger = None
s_decimal_NaN = Decimal("nan")


class OurbitExchange(ExchangePyBase):
    web_utils = web_utils

    def __init__(
        self,
        ourbit_api_key: str,
        ourbit_api_secret: str,
        balance_asset_limit: Optional[Dict[str, Dict[str, Decimal]]] = None,
        rate_limits_share_pct: Decimal = Decimal("100"),
        trading_pairs: Optional[List[str]] = None,
        trading_required: bool = True,
        domain: str = CONSTANTS.DEFAULT_DOMAIN,
    ):
        self.api_key = ourbit_api_key
        self.secret_key = ourbit_api_secret
        self._domain = domain
        self._trading_required = trading_required
        self._trading_pairs = trading_pairs
        self._last_trades_poll_ourbit_timestamp = 1.0
        super().__init__(balance_asset_limit, rate_limits_share_pct)

    @staticmethod
    def ourbit_order_type(order_type: OrderType) -> str:
        return order_type.name.upper()

    @staticmethod
    def to_hb_order_type(ourbit_type: str) -> OrderType:
        return OrderType[ourbit_type]

    @property
    def authenticator(self):
        return OurbitAuth(api_key=self.api_key, secret_key=self.secret_key)

    @property
    def name(self) -> str:
        return "ourbit"

    @property
    def rate_limits_rules(self):
        return CONSTANTS.RATE_LIMITS

    @property
    def domain(self):
        return self._domain

    @property
    def client_order_id_max_length(self):
        return CONSTANTS.MAX_ORDER_ID_LEN

    @property
    def client_order_id_prefix(self):
        return CONSTANTS.HBOT_ORDER_ID_PREFIX

    @property
    def trading_rules_request_path(self):
        return CONSTANTS.EXCHANGE_INFO_PATH_URL

    @property
    def trading_pairs_request_path(self):
        return CONSTANTS.EXCHANGE_INFO_PATH_URL

    @property
    def check_network_request_path(self):
        return CONSTANTS.SERVER_TIME_PATH_URL

    @property
    def trading_pairs(self):
        return self._trading_pairs

    @property
    def is_cancel_request_in_exchange_synchronous(self) -> bool:
        return True

    @property
    def is_trading_required(self) -> bool:
        return self._trading_required

    def supported_order_types(self):
        return [OrderType.MARKET, OrderType.LIMIT, OrderType.LIMIT_MAKER]

    def _is_request_exception_related_to_time_synchronizer(self, request_exception: Exception):
        return False

    def _is_order_not_found_during_status_update_error(self, status_update_exception: Exception) -> bool:
        return "Order does not exist" in str(status_update_exception)

    def _is_order_not_found_during_cancelation_error(self, cancelation_exception: Exception) -> bool:
        return "Order does not exist" in str(cancelation_exception)

    def _create_web_assistants_factory(self) -> WebAssistantsFactory:
        return web_utils.build_api_factory(
            throttler=self._throttler, time_synchronizer=self._time_synchronizer, domain=self._domain, auth=self._auth
        )

    def _create_order_book_data_source(self) -> OrderBookTrackerDataSource:
        return OurbitAPIOrderBookDataSource(
            trading_pairs=self._trading_pairs,
            connector=self,
            domain=self.domain,
            api_factory=self._web_assistants_factory,
            throttler=self._throttler,
        )

    def _create_user_stream_data_source(self) -> UserStreamTrackerDataSource:
        return OurbitAPIUserStreamDataSource(
            auth=self._auth,
            throttler=self._throttler,
            api_factory=self._web_assistants_factory,
            domain=self.domain,
        )

    def _get_fee(
        self,
        base_currency: str,
        quote_currency: str,
        order_type: OrderType,
        order_side: TradeType,
        amount: Decimal,
        price: Decimal = s_decimal_NaN,
        is_maker: Optional[bool] = None,
    ) -> TradeFeeBase:
        is_maker = order_type is OrderType.LIMIT_MAKER
        trade_base_fee = build_trade_fee(
            exchange="ourbit",
            is_maker=is_maker,
            order_side=order_side,
            order_type=order_type,
            amount=amount,
            price=price,
            base_currency=base_currency,
            quote_currency=quote_currency,
        )
        return trade_base_fee

    def quantize_order_amount(self, trading_pair: str, amount: Decimal) -> Decimal:
        """
        Ensures the order amount adheres to the exchange's step size constraints.
        """
        step_size = self._trading_rules[trading_pair].min_base_amount_increment
        return amount.quantize(step_size, rounding=ROUND_DOWN)

    async def _place_order(
        self,
        order_id: str,
        trading_pair: str,
        amount: Decimal,
        trade_type: TradeType,
        order_type: OrderType,
        price: Decimal,
        **kwargs,
    ) -> Tuple[str, float]:
        amount_str = f"{amount:f}"
        type_str = self.ourbit_order_type(order_type)

        side_str = CONSTANTS.SIDE_BUY if trade_type is TradeType.BUY else CONSTANTS.SIDE_SELL
        # Convert trading pair format from "BTC-USDT" to "BTCUSDT" for Ourbit API
        symbol = trading_pair.replace("-", "")
        api_params = {
            "symbol": symbol,
            "side": side_str,
            "quantity": amount_str,
            "type": type_str,
            "newClientOrderId": order_id,
        }
        if order_type != OrderType.MARKET:
            api_params["price"] = f"{price:f}"
        if order_type == OrderType.LIMIT:
            api_params["timeInForce"] = CONSTANTS.TIME_IN_FORCE_GTC

        order_result = await self._api_request(
            path_url=CONSTANTS.ORDER_PATH_URL,
            method=RESTMethod.POST,
            params=api_params,
            is_auth_required=True,
        )

        o_id = str(order_result["orderId"])
        transact_time = int(order_result["transactTime"]) * 1e-3
        return (o_id, transact_time)

    async def _place_cancel(self, order_id: str, tracked_order: InFlightOrder):
        # Convert trading pair format from "BTC-USDT" to "BTCUSDT" for Ourbit API
        symbol = tracked_order.trading_pair.replace("-", "")
        api_params = {"symbol": symbol}
        if tracked_order.exchange_order_id:
            api_params["orderId"] = tracked_order.exchange_order_id
        else:
            api_params["origClientOrderId"] = tracked_order.client_order_id

        cancel_result = await self._api_request(
            path_url=CONSTANTS.CANCEL_ORDER_PATH_URL, method=RESTMethod.DELETE, params=api_params, is_auth_required=True
        )

        if isinstance(cancel_result, dict) and cancel_result.get("orderId"):
            self._order_tracker.process_order_update(
                OrderUpdate(
                    client_order_id=tracked_order.client_order_id,
                    exchange_order_id=tracked_order.exchange_order_id,
                    trading_pair=tracked_order.trading_pair,
                    update_timestamp=time.time(),
                    new_state=OrderState.CANCELED,
                )
            )
            return True
        else:
            await self._order_tracker.process_order_not_found(tracked_order.client_order_id)
            return False

    async def _format_trading_rules(self, exchange_info_dict: Dict[str, Any]) -> List[TradingRule]:
        """
        Example:
        {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "status": "TRADING",
                    "baseAsset": "BTC",
                    "baseAssetPrecision": 8,
                    "quoteAsset": "USDT",
                    "quotePrecision": 8,
                    "quoteAssetPrecision": 8,
                    "filters": [
                        {
                            "filterType": "PRICE_FILTER",
                            "minPrice": "0.00000100",
                            "maxPrice": "1000000.00000000",
                            "tickSize": "0.00000100"
                        },
                        {
                            "filterType": "PERCENT_PRICE",
                            "multiplierUp": "5",
                            "multiplierDown": "0.2",
                            "avgPriceMins": 5
                        },
                        {
                            "filterType": "LOT_SIZE",
                            "minQty": "0.00001000",
                            "maxQty": "9000000.00000000",
                            "stepSize": "0.00001000"
                        },
                        {
                            "filterType": "MIN_NOTIONAL",
                            "minNotional": "10.00000000",
                            "applyToMarket": true,
                            "avgPriceMins": 5
                        }
                    ]
                }
            ]
        }
        """
        trading_pair_rules = exchange_info_dict.get("symbols", [])
        trading_pair_rules = [item for item in trading_pair_rules if (item.get("symbol") in self.trading_pairs)]
        retval = []
        for rule in trading_pair_rules:
            try:
                trading_pair = rule.get("symbol")

                # Get filters
                filters = {f["filterType"]: f for f in rule.get("filters", [])}

                # Price filter
                price_filter = filters.get("PRICE_FILTER", {})
                min_price_increment = Decimal(str(price_filter.get("tickSize", "0.00000001")))

                # Lot size filter
                lot_size_filter = filters.get("LOT_SIZE", {})
                min_base_amount_increment = Decimal(str(lot_size_filter.get("stepSize", "0.00000001")))
                min_order_size = Decimal(str(lot_size_filter.get("minQty", "0.00000001")))
                max_order_size = Decimal(str(lot_size_filter.get("maxQty", "9000000000")))

                # Min notional filter
                min_notional_filter = filters.get("MIN_NOTIONAL", {})
                min_notional_size = Decimal(str(min_notional_filter.get("minNotional", "10")))

                retval.append(
                    TradingRule(
                        trading_pair,
                        min_order_size=min_order_size,
                        max_order_size=max_order_size,
                        min_price_increment=min_price_increment,
                        min_base_amount_increment=min_base_amount_increment,
                        min_notional_size=min_notional_size,
                    )
                )
            except Exception as exception:
                self.logger().exception(
                    f"Error parsing the trading pair rule {rule.get('symbol')}. Skipping. Error: {exception}"
                )
        return retval

    async def _update_trading_fees(self):
        """
        Update fees information from the exchange
        """
        pass

    async def _user_stream_event_listener(self):
        """
        This functions runs in background continuously processing the events received from the exchange by the user
        stream data source. It keeps reading events from the queue until the task is interrupted.
        The events received are balance updates, order updates and trade events.
        """
        async for event_message in self._iter_user_event_queue():
            try:
                if event_message.get("e") == "executionReport":
                    execution_type = event_message.get("X")
                    client_order_id = event_message.get("C")

                    tracked_order = self._order_tracker.all_fillable_orders.get(client_order_id)
                    if tracked_order is not None:
                        if execution_type in ["PARTIALLY_FILLED", "FILLED"]:
                            new_state = CONSTANTS.ORDER_STATE[event_message["X"]]
                            if (
                                new_state == OrderState.FILLED
                                and tracked_order.current_state == OrderState.PENDING_CREATE
                            ):
                                order_update = OrderUpdate(
                                    trading_pair=tracked_order.trading_pair,
                                    update_timestamp=int(event_message["E"]) * 1e-3,
                                    new_state=OrderState.OPEN,
                                    client_order_id=tracked_order.client_order_id,
                                    exchange_order_id=str(event_message["i"]),
                                )
                                await self._order_tracker._process_order_update(order_update)

                            fee = TradeFeeBase.new_spot_fee(
                                fee_schema=self.trade_fee_schema(),
                                trade_type=tracked_order.trade_type,
                                flat_fees=[
                                    TokenAmount(amount=Decimal(str(event_message["n"])), token=event_message["N"])
                                ],
                            )
                            trade_update = TradeUpdate(
                                trade_id=str(event_message["t"]),
                                client_order_id=tracked_order.client_order_id,
                                exchange_order_id=str(event_message["i"]),
                                trading_pair=tracked_order.trading_pair,
                                fee=fee,
                                fill_base_amount=Decimal(str(event_message["l"])),
                                fill_quote_amount=Decimal(str(event_message["l"])) * Decimal(str(event_message["L"])),
                                fill_price=Decimal(str(event_message["L"])),
                                fill_timestamp=int(event_message["E"]) * 1e-3,
                            )
                            self._order_tracker.process_trade_update(trade_update)

                    tracked_order = self._order_tracker.all_updatable_orders.get(client_order_id)
                    if tracked_order is not None:
                        new_state = CONSTANTS.ORDER_STATE[event_message["X"]]
                        if new_state == OrderState.PENDING_CREATE:
                            continue

                        order_update = OrderUpdate(
                            trading_pair=tracked_order.trading_pair,
                            update_timestamp=int(event_message["E"]) * 1e-3,
                            new_state=new_state,
                            client_order_id=tracked_order.client_order_id,
                            exchange_order_id=str(event_message["i"]),
                        )
                        self._order_tracker.process_order_update(order_update=order_update)
                        continue

                elif event_message.get("e") == "outboundAccountPosition":
                    balances = event_message.get("B", [])
                    for balance_entry in balances:
                        asset_name = balance_entry["a"]
                        free_balance = Decimal(str(balance_entry["f"]))
                        total_balance = Decimal(str(balance_entry["f"])) + Decimal(str(balance_entry["l"]))
                        self._account_available_balances[asset_name] = free_balance
                        self._account_balances[asset_name] = total_balance
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().error("Unexpected error in user stream listener loop.", exc_info=True)
                await self._sleep(5.0)

    async def _all_trade_updates_for_order(self, order: InFlightOrder) -> List[TradeUpdate]:
        trade_updates = []

        if order.exchange_order_id is not None:
            exchange_order_id = int(order.exchange_order_id)
            trading_pair = order.trading_pair
            all_fills_response = await self._api_request(
                path_url=CONSTANTS.MY_TRADES_PATH_URL,
                method=RESTMethod.GET,
                params={"symbol": trading_pair.replace("-", ""), "orderId": exchange_order_id},
                is_auth_required=True,
                limit_id=CONSTANTS.MY_TRADES_PATH_URL,
            )

            fills_data = all_fills_response if isinstance(all_fills_response, list) else [all_fills_response]
            for trade in fills_data:
                exchange_order_id = str(trade["orderId"])
                fee = TradeFeeBase.new_spot_fee(
                    fee_schema=self.trade_fee_schema(),
                    trade_type=order.trade_type,
                    percent_token=trade["commissionAsset"],
                    flat_fees=[TokenAmount(amount=Decimal(str(trade["commission"])), token=trade["commissionAsset"])],
                )
                trade_update = TradeUpdate(
                    trade_id=str(trade["id"]),
                    client_order_id=order.client_order_id,
                    exchange_order_id=exchange_order_id,
                    trading_pair=trading_pair,
                    fee=fee,
                    fill_base_amount=Decimal(str(trade["qty"])),
                    fill_quote_amount=Decimal(str(trade["price"])) * Decimal(str(trade["qty"])),
                    fill_price=Decimal(str(trade["price"])),
                    fill_timestamp=int(trade["time"]) * 1e-3,
                )
                trade_updates.append(trade_update)

        return trade_updates

    async def _request_order_status(self, tracked_order: InFlightOrder) -> OrderUpdate:
        updated_order_data = await self._api_request(
            path_url=CONSTANTS.ORDER_PATH_URL,
            method=RESTMethod.GET,
            params={"symbol": tracked_order.trading_pair.replace("-", ""), "orderId": tracked_order.exchange_order_id},
            is_auth_required=True,
        )

        new_state = CONSTANTS.ORDER_STATE[updated_order_data["status"]]
        if new_state == OrderState.PENDING_CREATE:
            new_state = OrderState.OPEN

        if new_state == OrderState.FILLED and tracked_order.current_state == OrderState.PENDING_CREATE:
            order_update = OrderUpdate(
                client_order_id=tracked_order.client_order_id,
                exchange_order_id=str(updated_order_data["orderId"]),
                trading_pair=tracked_order.trading_pair,
                update_timestamp=int(updated_order_data["updateTime"]) * 1e-3,
                new_state=OrderState.OPEN,
            )
            await self._order_tracker._process_order_update(order_update)

        order_update = OrderUpdate(
            client_order_id=tracked_order.client_order_id,
            exchange_order_id=str(updated_order_data["orderId"]),
            trading_pair=tracked_order.trading_pair,
            update_timestamp=int(updated_order_data["updateTime"]) * 1e-3,
            new_state=new_state,
        )

        return order_update

    async def _update_balances(self):
        local_asset_names = set(self._account_balances.keys())
        remote_asset_names = set()

        account_info = await self._api_request(
            method=RESTMethod.GET, path_url=CONSTANTS.ACCOUNTS_PATH_URL, is_auth_required=True
        )
        balances = account_info["balances"]
        for balance_entry in balances:
            asset_name = balance_entry["asset"]
            free_balance = Decimal(str(balance_entry["free"]))
            total_balance = Decimal(str(balance_entry["free"])) + Decimal(str(balance_entry["locked"]))
            self._account_available_balances[asset_name] = free_balance
            self._account_balances[asset_name] = total_balance
            remote_asset_names.add(asset_name)

        asset_names_to_remove = local_asset_names.difference(remote_asset_names)
        for asset_name in asset_names_to_remove:
            del self._account_available_balances[asset_name]
            del self._account_balances[asset_name]

    def _initialize_trading_pair_symbols_from_exchange_info(self, exchange_info: Dict[str, Any]):
        mapping = bidict()
        for symbol_data in filter(ourbit_utils.is_exchange_information_valid, exchange_info["symbols"]):
            # Convert from Ourbit symbol format (BTCUSDT) to Hummingbot format (BTC-USDT)
            symbol = symbol_data["symbol"]
            base_asset = symbol_data["baseAsset"]
            quote_asset = symbol_data["quoteAsset"]
            trading_pair = f"{base_asset}-{quote_asset}"
            mapping[trading_pair] = symbol
        self._set_trading_pair_symbol_map(mapping)

    async def _get_last_traded_price(self, trading_pair: str) -> float:
        # Convert trading pair format from "BTC-USDT" to "BTCUSDT" for Ourbit API
        symbol = trading_pair.replace("-", "")
        params = {"symbol": symbol}
        resp_json = await self._api_request(
            method=RESTMethod.GET, path_url=CONSTANTS.LAST_TRADED_PRICE_PATH, params=params, is_auth_required=False
        )
        # Handle both single symbol and list responses
        if isinstance(resp_json, list):
            for ticker in resp_json:
                if ticker.get("symbol") == symbol:
                    return float(ticker["lastPrice"])
        else:
            return float(resp_json["lastPrice"])
        return 0.0

    async def _api_request(
        self,
        path_url,
        method: RESTMethod = RESTMethod.GET,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        is_auth_required: bool = False,
        return_err: bool = False,
        limit_id: Optional[str] = None,
        trading_pair: Optional[str] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        rest_assistant = await self._web_assistants_factory.get_rest_assistant()
        url = web_utils.rest_url(path_url, domain=self.domain)

        local_headers = {"Content-Type": "application/json", "Accept": "application/json"}

        request_result = await rest_assistant.execute_request(
            url=url,
            params=params,
            data=data,
            method=method,
            is_auth_required=is_auth_required,
            return_err=return_err,
            headers=local_headers,
            throttler_limit_id=limit_id if limit_id else path_url,
        )
        return request_result
