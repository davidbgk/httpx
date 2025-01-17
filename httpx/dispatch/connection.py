import functools
import ssl
import typing

import h11

from ..backends.base import BaseSocketStream, ConcurrencyBackend, lookup_backend
from ..config import CertTypes, SSLConfig, Timeout, VerifyTypes
from ..models import URL, Origin, Request, Response
from ..utils import get_logger
from .base import Dispatcher, OpenConnection
from .http2 import HTTP2Connection
from .http11 import HTTP11Connection

# Callback signature: async def callback(conn: HTTPConnection) -> None
ReleaseCallback = typing.Callable[["HTTPConnection"], typing.Awaitable[None]]


logger = get_logger(__name__)


class HTTPConnection(Dispatcher):
    def __init__(
        self,
        origin: typing.Union[str, Origin],
        verify: VerifyTypes = True,
        cert: CertTypes = None,
        trust_env: bool = None,
        http2: bool = False,
        backend: typing.Union[str, ConcurrencyBackend] = "auto",
        release_func: typing.Optional[ReleaseCallback] = None,
        uds: typing.Optional[str] = None,
    ):
        self.origin = Origin(origin) if isinstance(origin, str) else origin
        self.ssl = SSLConfig(cert=cert, verify=verify, trust_env=trust_env)
        self.http2 = http2
        self.backend = lookup_backend(backend)
        self.release_func = release_func
        self.uds = uds
        self.open_connection: typing.Optional[OpenConnection] = None
        self.expires_at: typing.Optional[float] = None

    async def send(self, request: Request, timeout: Timeout = None) -> Response:
        timeout = Timeout() if timeout is None else timeout

        if self.open_connection is None:
            await self.connect(timeout=timeout)

        assert self.open_connection is not None
        response = await self.open_connection.send(request, timeout=timeout)

        return response

    async def connect(self, timeout: Timeout) -> None:
        host = self.origin.host
        port = self.origin.port
        ssl_context = await self.get_ssl_context(self.ssl)

        if self.release_func is None:
            on_release = None
        else:
            on_release = functools.partial(self.release_func, self)

        if self.uds is None:
            logger.trace(
                f"start_connect tcp host={host!r} port={port!r} timeout={timeout!r}"
            )
            stream = await self.backend.open_tcp_stream(
                host, port, ssl_context, timeout
            )
        else:
            logger.trace(
                f"start_connect uds path={self.uds!r} host={host!r} timeout={timeout!r}"
            )
            stream = await self.backend.open_uds_stream(
                self.uds, host, ssl_context, timeout
            )

        http_version = stream.get_http_version()
        logger.trace(f"connected http_version={http_version!r}")

        self.set_open_connection(http_version, socket=stream, on_release=on_release)

    async def tunnel_start_tls(
        self, origin: Origin, proxy_url: URL, timeout: Timeout = None,
    ) -> None:
        """
        Upgrade this connection to use TLS, assuming it represents a TCP tunnel.
        """
        timeout = Timeout() if timeout is None else timeout

        # First, check that we are in the correct state to start TLS, i.e. we've
        # just agreed to switch protocols with the server via HTTP/1.1.
        assert isinstance(self.open_connection, HTTP11Connection)
        h11_connection = self.open_connection
        assert h11_connection is not None
        assert h11_connection.h11_state.our_state == h11.SWITCHED_PROTOCOL

        # Store this information here so that we can transfer
        # it to the new internal connection object after
        # the old one goes to 'SWITCHED_PROTOCOL'.
        # Note that the negotiated 'http_version' may change after the TLS upgrade.
        http_version = "HTTP/1.1"
        socket = h11_connection.socket
        on_release = h11_connection.on_release

        if origin.is_ssl:
            # Pull the socket stream off the internal HTTP connection object,
            # and run start_tls().
            ssl_context = await self.get_ssl_context(self.ssl)
            assert ssl_context is not None

            logger.trace(f"tunnel_start_tls proxy_url={proxy_url!r} origin={origin!r}")
            socket = await socket.start_tls(
                hostname=origin.host, ssl_context=ssl_context, timeout=timeout
            )
            http_version = socket.get_http_version()
            logger.trace(
                f"tunnel_tls_complete "
                f"proxy_url={proxy_url!r} "
                f"origin={origin!r} "
                f"http_version={http_version!r}"
            )
        else:
            # User requested the use of a tunnel, but they're performing a plain-text
            # HTTP request. Don't try to upgrade to TLS in this case.
            pass

        self.set_open_connection(http_version, socket=socket, on_release=on_release)

    def set_open_connection(
        self,
        http_version: str,
        socket: BaseSocketStream,
        on_release: typing.Optional[typing.Callable],
    ) -> None:
        if http_version == "HTTP/2":
            self.open_connection = HTTP2Connection(
                socket, self.backend, on_release=on_release
            )
        else:
            assert http_version == "HTTP/1.1"
            self.open_connection = HTTP11Connection(socket, on_release=on_release)

    async def get_ssl_context(self, ssl: SSLConfig) -> typing.Optional[ssl.SSLContext]:
        if not self.origin.is_ssl:
            return None

        # Run the SSL loading in a threadpool, since it may make disk accesses.
        return await self.backend.run_in_threadpool(ssl.load_ssl_context, self.http2)

    async def close(self) -> None:
        logger.trace("close_connection")
        if self.open_connection is not None:
            await self.open_connection.close()

    @property
    def is_http2(self) -> bool:
        assert self.open_connection is not None
        return self.open_connection.is_http2

    @property
    def is_closed(self) -> bool:
        assert self.open_connection is not None
        return self.open_connection.is_closed

    def is_connection_dropped(self) -> bool:
        assert self.open_connection is not None
        return self.open_connection.is_connection_dropped()

    def __repr__(self) -> str:
        class_name = self.__class__.__name__
        return f"{class_name}(origin={self.origin!r})"
