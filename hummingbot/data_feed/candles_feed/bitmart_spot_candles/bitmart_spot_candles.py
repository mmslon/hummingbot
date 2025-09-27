import logging
from typing import Any, Dict, List, Optional

from hummingbot.core.network_iterator import NetworkStatus
from hummingbot.data_feed.candles_feed.bitmart_spot_candles import constants as CONSTANTS
from hummingbot.data_feed.candles_feed.candles_base import CandlesBase
from hummingbot.logger import HummingbotLogger


class BitmartSpotCandles(CandlesBase):
    _logger: Optional[HummingbotLogger] = None

    @classmethod
    def logger(cls) -> HummingbotLogger:
        if cls._logger is None:
            cls._logger = logging.getLogger(__name__)
        return cls._logger

    def __init__(self, trading_pair: str, interval: str = "1m", max_records: int = 150):
        super().__init__(trading_pair, interval, max_records)
        self.contract_size = None
        self.ws_interval = {
            "1m": "1m",
            "5m": "5m",
            "15m": "15m",
            "30m": "30m",
            "1h": "1H",
            "2h": "2H",
            "4h": "4H",
            "12h": "12H",
            "1d": "1D",
            "1w": "1W",
        }

    @property
    def name(self):
        return f"bitmart_{self._trading_pair}"

    @property
    def rest_url(self):
        return CONSTANTS.REST_URL

    @property
    def wss_url(self):
        return CONSTANTS.WSS_URL

    @property
    def health_check_url(self):
        return self.rest_url + CONSTANTS.HEALTH_CHECK_ENDPOINT

    @property
    def candles_url(self):
        return self.rest_url + CONSTANTS.CANDLES_ENDPOINT

    @property
    def candles_endpoint(self):
        return CONSTANTS.CANDLES_ENDPOINT

    @property
    def candles_max_result_per_rest_request(self):
        return CONSTANTS.MAX_RESULTS_PER_CANDLESTICK_REST_REQUEST

    @property
    def rate_limits(self):
        return CONSTANTS.RATE_LIMITS

    @property
    def intervals(self):
        return CONSTANTS.INTERVALS

    @property
    def is_linear(self):
        return "USDT" in self._trading_pair

    async def check_network(self) -> NetworkStatus:
        rest_assistant = await self._api_factory.get_rest_assistant()
        await rest_assistant.execute_request(
            url=self.health_check_url, throttler_limit_id=CONSTANTS.HEALTH_CHECK_ENDPOINT
        )
        return NetworkStatus.CONNECTED

    def get_exchange_trading_pair(self, trading_pair):
        return trading_pair.replace("-", "")

    @property
    def _is_first_candle_not_included_in_rest_request(self):
        return False

    @property
    def _is_last_candle_not_included_in_rest_request(self):
        return False

    def _get_rest_candles_params(
        self,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        limit: Optional[int] = CONSTANTS.MAX_RESULTS_PER_CANDLESTICK_REST_REQUEST,
    ) -> dict:
        """
        start_time and end_time must be used at the same time.
        """
        params = {
            "symbol": self._ex_trading_pair,
            "step": CONSTANTS.INTERVALS[self.interval],
        }
        if start_time:
            params["after"] = start_time
        if end_time:
            params["before"] = end_time

        return params

    def _parse_rest_candles(self, data: dict, end_time: Optional[int] = None) -> List[List[float]]:
        if data is not None and data.get("result") is not None:
            candles = data.get("result").get("list")
            if len(candles) > 0:
                return [
                    [
                        self.ensure_timestamp_in_seconds(row[0]),
                        row[1],
                        row[2],
                        row[3],
                        row[4],
                        float(row[5]),
                        0.0,
                        0.0,
                        0.0,
                        0.0,
                    ]
                    for row in candles
                ]
        return []

    def ws_subscription_payload(self):
        interval = self.ws_interval[self.interval]
        channel = f"spot/kline{interval}"
        args = [f"{channel}:{self._ex_trading_pair.replace("USDT", "_USDT")}"]
        payload = {
            "op": "subscribe",
            "args": args,
        }

        return payload

    def _parse_websocket_message(self, data):
        candles_row_dict: Dict[str, Any] = {}
        if data is not None and data.get("data") is not None:
            candle = data["data"][0]
            candles_row_dict["timestamp"] = self.ensure_timestamp_in_seconds(data["ts"])
            candles_row_dict["open"] = candle["open"]
            candles_row_dict["low"] = candle["low"]
            candles_row_dict["high"] = candle["high"]
            candles_row_dict["close"] = candle["close"]
            candles_row_dict["volume"] = float(candle["volume"])
            candles_row_dict["quote_asset_volume"] = 0.0
            candles_row_dict["n_trades"] = 0.0
            candles_row_dict["taker_buy_base_volume"] = 0.0
            candles_row_dict["taker_buy_quote_volume"] = 0.0
            return candles_row_dict
