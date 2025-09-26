import ssl
from typing import Callable, Optional

import aiohttp

import hummingbot.connector.exchange.ourbit.ourbit_constants as CONSTANTS
from hummingbot.connector.time_synchronizer import TimeSynchronizer
from hummingbot.connector.utils import TimeSynchronizerRESTPreProcessor
from hummingbot.core.api_throttler.async_throttler import AsyncThrottler
from hummingbot.core.web_assistant.auth import AuthBase
from hummingbot.core.web_assistant.connections.connections_factory import ConnectionsFactory
from hummingbot.core.web_assistant.connections.data_types import RESTMethod
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory


def public_rest_url(path_url: str, domain: str = CONSTANTS.DEFAULT_DOMAIN) -> str:
    """
    Creates a full URL for provided public REST endpoint

    :param path_url: a public REST endpoint
    :param domain: the domain to connect to ("main"). The default value is "main"

    :return: the full URL to the endpoint
    """
    return CONSTANTS.REST_URLS[domain] + path_url


def rest_url(path_url: str, domain: str = CONSTANTS.DEFAULT_DOMAIN) -> str:
    """
    Creates a full URL for provided REST endpoint (alias for public_rest_url for compatibility)

    :param path_url: a REST endpoint
    :param domain: the domain to connect to ("main"). The default value is "main"

    :return: the full URL to the endpoint
    """
    return public_rest_url(path_url, domain)


def private_rest_url(path_url: str, domain: str = CONSTANTS.DEFAULT_DOMAIN) -> str:
    """
    Creates a full URL for provided private REST endpoint

    :param path_url: a private REST endpoint
    :param domain: the domain to connect to ("main"). The default value is "main"

    :return: the full URL to the endpoint
    """
    return public_rest_url(path_url=path_url, domain=domain)


def wss_url(path_url: str, domain: str = CONSTANTS.DEFAULT_DOMAIN) -> str:
    """
    Creates a full URL for provided websocket endpoint
    :param path_url: a websocket endpoint
    :param domain: the Ourbit domain to connect to ("main"). The default value is "main"
    :return: the full URL to the endpoint
    """
    return CONSTANTS.WSS_PUBLIC_URL[domain] + path_url


class OurbitConnectionsFactory(ConnectionsFactory):
    """Custom ConnectionsFactory that handles SSL verification issues"""

    def __init__(self):
        super().__init__()
        # Create SSL context that can handle certificate verification issues
        self._ssl_context = ssl.create_default_context()
        self._ssl_context.check_hostname = False
        self._ssl_context.verify_mode = ssl.CERT_NONE

    async def _get_shared_client(self) -> aiohttp.ClientSession:
        """
        Lazily create a shared aiohttp.ClientSession with SSL handling.
        """
        if self._shared_client is None:
            connector = aiohttp.TCPConnector(ssl=self._ssl_context)
            self._shared_client = aiohttp.ClientSession(connector=connector)
        return self._shared_client


def build_api_factory(
    throttler: Optional[AsyncThrottler] = None,
    time_synchronizer: Optional[TimeSynchronizer] = None,
    domain: str = CONSTANTS.DEFAULT_DOMAIN,
    time_provider: Optional[Callable] = None,
    auth: Optional[AuthBase] = None,
) -> WebAssistantsFactory:
    throttler = throttler or create_throttler()
    time_synchronizer = time_synchronizer or TimeSynchronizer()
    time_provider = time_provider or (
        lambda: get_current_server_time(
            throttler=throttler,
            domain=domain,
        )
    )

    # Use custom connections factory to handle SSL issues
    connections_factory = OurbitConnectionsFactory()

    api_factory = WebAssistantsFactory(
        throttler=throttler,
        auth=auth,
        connections_factory=connections_factory,
        rest_pre_processors=[
            TimeSynchronizerRESTPreProcessor(synchronizer=time_synchronizer, time_provider=time_provider),
        ],
    )
    return api_factory


def build_api_factory_without_time_synchronizer_pre_processor(throttler: AsyncThrottler) -> WebAssistantsFactory:
    # Use custom connections factory to handle SSL issues
    connections_factory = OurbitConnectionsFactory()
    api_factory = WebAssistantsFactory(throttler=throttler, connections_factory=connections_factory)
    return api_factory


def create_throttler() -> AsyncThrottler:
    return AsyncThrottler(CONSTANTS.RATE_LIMITS)


async def get_current_server_time(
    throttler: Optional[AsyncThrottler] = None,
    domain: str = CONSTANTS.DEFAULT_DOMAIN,
) -> float:
    throttler = throttler or create_throttler()
    api_factory = build_api_factory_without_time_synchronizer_pre_processor(throttler=throttler)
    rest_assistant = await api_factory.get_rest_assistant()
    response = await rest_assistant.execute_request(
        url=public_rest_url(path_url=CONSTANTS.SERVER_TIME_PATH_URL, domain=domain),
        method=RESTMethod.GET,
        throttler_limit_id=CONSTANTS.SERVER_TIME_PATH_URL,
    )
    server_time = response["serverTime"]
    return server_time
