import gzip
import io
import json
from decimal import Decimal
from typing import Any, Dict

from pydantic import ConfigDict, Field, SecretStr

from hummingbot.client.config.config_data_types import BaseConnectorConfigMap
from hummingbot.core.data_type.trade_fee import TradeFeeSchema

CENTRALIZED = True
EXAMPLE_PAIR = "BTC-USDT"
DEFAULT_FEES = TradeFeeSchema(
    maker_percent_fee_decimal=Decimal("0.001"),
    taker_percent_fee_decimal=Decimal("0.001"),
    buy_percent_fee_deducted_from_returns=True,
)


def is_exchange_information_valid(exchange_info: Dict[str, Any]) -> bool:
    """
    Verifies if a trading pair is enabled to operate with based on its exchange information
    :param exchange_info: the exchange information for a trading pair
    :return: True if the trading pair is enabled, False otherwise
    """
    return exchange_info.get("status") == "TRADING"


def decompress_ws_message(message):
    if isinstance(message, bytes):
        try:
            compressed_data = gzip.GzipFile(fileobj=io.BytesIO(message), mode="rb")
            decompressed_data = compressed_data.read()
            utf8_data = decompressed_data.decode("utf-8")
            return json.loads(utf8_data)
        except Exception:
            # If decompression fails, try to decode as plain text
            return json.loads(message.decode("utf-8"))
    else:
        return message


class OurbitConfigMap(BaseConnectorConfigMap):
    connector: str = "ourbit"
    ourbit_api_key: SecretStr = Field(
        default=...,
        json_schema_extra={
            "prompt": "Enter your Ourbit API key",
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        },
    )
    ourbit_api_secret: SecretStr = Field(
        default=...,
        json_schema_extra={
            "prompt": "Enter your Ourbit API secret",
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        },
    )
    model_config = ConfigDict(title="ourbit")


KEYS = OurbitConfigMap.model_construct()
