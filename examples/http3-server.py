import argparse
import asyncio
import logging
import os
import re
from typing import Dict, List, Optional, Text, Tuple, Union, cast

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization

import aioquic.events
from aioquic.buffer import Buffer
from aioquic.configuration import QuicConfiguration
from aioquic.connection import NetworkAddress, QuicConnection
from aioquic.h3.connection import H3Connection
from aioquic.h3.events import RequestReceived
from aioquic.packet import (
    PACKET_TYPE_INITIAL,
    encode_quic_retry,
    encode_quic_version_negotiation,
    pull_quic_header,
)
from aioquic.retry import QuicRetryTokenHandler
from aioquic.tls import SessionTicket, SessionTicketFetcher, SessionTicketHandler

TEMPLATE = """<!DOCTYPE html>
<html>
    <head>
        <meta charset="utf-8"/>
        <title>aioquic</title>
    </head>
    <body>
        <h1>Welcome to aioquic</h1>
        <p>{content}</p>
    </body>
</html>
"""


class H0Connection:
    """
    An HTTP/0.9 connection object.
    """

    def __init__(self, quic: QuicConnection):
        self._quic = quic

    def handle_event(self, event: aioquic.events.Event):
        http_events = []

        if (
            isinstance(event, aioquic.events.StreamDataReceived)
            and (event.stream_id % 4) == 0
        ):
            method, path = event.data.rstrip().split(b" ", 1)
            http_events.append(
                RequestReceived(
                    headers=[(b":method", method), (b":path", path)],
                    stream_ended=event.end_stream,
                    stream_id=event.stream_id,
                )
            )

        return http_events

    def send_data(self, stream_id: int, data: bytes, end_stream: bool) -> None:
        self._quic.send_stream_data(stream_id, data, end_stream)

    def send_headers(self, stream_id: int, headers: List[Tuple[bytes, bytes]]) -> None:
        # HTTP/0.9 has no concept of headers.
        pass


HttpConnection = Union[H0Connection, H3Connection]


class HttpServer(asyncio.DatagramProtocol):
    def __init__(
        self,
        *,
        configuration: QuicConfiguration,
        session_ticket_fetcher: Optional[SessionTicketFetcher] = None,
        session_ticket_handler: Optional[SessionTicketHandler] = None,
        stateless_retry: bool = False,
    ) -> None:
        self._connections: Dict[bytes, QuicConnection] = {}
        self._configuration = configuration
        self._http: Dict[QuicConnection, HttpConnection] = {}
        self._loop = asyncio.get_event_loop()
        self._session_ticket_fetcher = session_ticket_fetcher
        self._session_ticket_handler = session_ticket_handler
        self._transport: Optional[asyncio.DatagramTransport] = None

        if stateless_retry:
            self._retry = QuicRetryTokenHandler()
        else:
            self._retry = None

    def close(self):
        for connection in set(self._connections.values()):
            connection.close()
        self._connections.clear()
        self._transport.close()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = cast(asyncio.DatagramTransport, transport)

    def datagram_received(self, data: Union[bytes, Text], addr: NetworkAddress) -> None:
        data = cast(bytes, data)
        buf = Buffer(data=data)
        header = pull_quic_header(buf, host_cid_length=8)

        # version negotiation
        if (
            header.version is not None
            and header.version not in self._configuration.supported_versions
        ):
            self._transport.sendto(
                encode_quic_version_negotiation(
                    source_cid=header.destination_cid,
                    destination_cid=header.source_cid,
                    supported_versions=self._configuration.supported_versions,
                ),
                addr,
            )
            return

        connection = self._connections.get(header.destination_cid, None)
        original_connection_id: Optional[bytes] = None
        if connection is None and header.packet_type == PACKET_TYPE_INITIAL:
            # stateless retry
            if self._retry is not None:
                if not header.token:
                    # create a retry token
                    self._transport.sendto(
                        encode_quic_retry(
                            version=header.version,
                            source_cid=os.urandom(8),
                            destination_cid=header.source_cid,
                            original_destination_cid=header.destination_cid,
                            retry_token=self._retry.create_token(
                                addr, header.destination_cid
                            ),
                        ),
                        addr,
                    )
                    return
                else:
                    # validate retry token
                    try:
                        original_connection_id = self._retry.validate_token(
                            addr, header.token
                        )
                    except ValueError:
                        return

            # create new connection
            connection = QuicConnection(
                configuration=self._configuration,
                original_connection_id=original_connection_id,
                session_ticket_fetcher=self._session_ticket_fetcher,
                session_ticket_handler=self._session_ticket_handler,
            )

            self._connections[header.destination_cid] = connection
            self._connections[connection.host_cid] = connection

        if connection is not None:
            connection.receive_datagram(cast(bytes, data), addr, now=self._loop.time())
            self._consume_events(connection)

    def _consume_events(self, connection: QuicConnection) -> None:
        # process events
        event = connection.next_event()
        while event is not None:
            if isinstance(event, aioquic.events.HandshakeCompleted):
                if event.alpn_protocol == "h3-20":
                    self._http[connection] = H3Connection(connection)
                elif event.alpn_protocol == "hq-20":
                    self._http[connection] = H0Connection(connection)
            elif isinstance(event, aioquic.events.ConnectionIdIssued):
                self._connections[event.connection_id] = connection
            elif isinstance(event, aioquic.events.ConnectionIdRetired):
                assert self._connections[event.connection_id] == connection
                del self._connections[event.connection_id]

            #  pass event to the HTTP layer
            http = self._http.get(connection)
            if http is not None:
                for http_event in http.handle_event(event):
                    handle_http_event(http, http_event)

            event = connection.next_event()

        # send datagrams
        for data, addr in connection.datagrams_to_send(now=self._loop.time()):
            self._transport.sendto(data, addr)

        # re-arm timer
        """
        timer_at = connection.get_timer()
        if self._timer is not None and self._timer_at != timer_at:
            self._timer.cancel()
            self._timer = None
        if self._timer is None and timer_at is not None:
            self._timer = self._loop.call_at(timer_at, self._handle_timer)
        self._timer_at = timer_at
        """


class SessionTicketStore:
    """
    Simple in-memory store for session tickets.
    """

    def __init__(self) -> None:
        self.tickets: Dict[bytes, SessionTicket] = {}

    def add(self, ticket: SessionTicket) -> None:
        self.tickets[ticket.ticket] = ticket

    def pop(self, label: bytes) -> Optional[SessionTicket]:
        return self.tickets.pop(label, None)


def handle_http_event(
    connection: HttpConnection, event: aioquic.h3.events.Event
) -> None:
    """
    Serve HTTP requests.
    """

    if isinstance(event, RequestReceived):
        headers = dict(event.headers)
        try:
            path = headers[b":path"].decode("utf8")
        except (UnicodeDecodeError, ValueError):
            send_response(
                connection=connection,
                data=render_html("Bad Request"),
                status_code=400,
                stream_id=event.stream_id,
            )

        size_match = re.match(r"^/(\d+)$", path)
        if size_match:
            # we accept a maximum of 50MB
            size = min(50000000, int(size_match.group(1)))
            send_response(
                connection=connection,
                content_type="text/plain",
                data=b"Z" * size,
                status_code=200,
                stream_id=event.stream_id,
            )
        elif path in ["/", "/index.html"]:
            send_response(
                connection=connection,
                data=render_html("It works!"),
                status_code=200,
                stream_id=event.stream_id,
            )
        else:
            send_response(
                connection=connection,
                data=render_html("The document could not be found."),
                status_code=404,
                stream_id=event.stream_id,
            )


def render_html(content: str) -> bytes:
    return TEMPLATE.format(content=content).encode("utf8")


def send_response(
    connection: HttpConnection,
    stream_id: int,
    data: bytes = b"",
    content_type: str = "text/html",
    status_code: int = 200,
) -> None:
    """
    Send an HTTP response on a connection and stream.
    """
    connection.send_headers(
        stream_id=stream_id,
        headers=[
            (b":status", str(status_code).encode("ascii")),
            (b"content-type", content_type.encode("ascii")),
        ],
    )
    connection.send_data(stream_id=stream_id, data=data, end_stream=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="QUIC server")
    parser.add_argument(
        "-c",
        "--certificate",
        type=str,
        required=True,
        help="load the TLS certificate from the specified file",
    )
    parser.add_argument(
        "--host",
        type=str,
        default="::",
        help="listen on the specified address (defaults to ::)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=4433,
        help="listen on the specified port (defaults to 4433)",
    )
    parser.add_argument(
        "-k",
        "--private-key",
        type=str,
        required=True,
        help="load the TLS private key from the specified file",
    )
    parser.add_argument(
        "-l",
        "--secrets-log",
        type=str,
        help="log secrets to a file, for use with Wireshark",
    )
    parser.add_argument(
        "-r",
        "--stateless-retry",
        action="store_true",
        help="send a stateless retry for new connections",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="increase logging verbosity"
    )
    args = parser.parse_args()

    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        level=logging.DEBUG if args.verbose else logging.INFO,
    )

    with open(args.certificate, "rb") as fp:
        certificate = x509.load_pem_x509_certificate(
            fp.read(), backend=default_backend()
        )
    with open(args.private_key, "rb") as fp:
        private_key = serialization.load_pem_private_key(
            fp.read(), password=None, backend=default_backend()
        )

    if args.secrets_log:
        secrets_log_file = open(args.secrets_log, "a")
    else:
        secrets_log_file = None

    # session tickets
    ticket_store = SessionTicketStore()

    configuration = QuicConfiguration(
        alpn_protocols=["h3-20", "hq-20"],
        certificate=certificate,
        is_client=False,
        private_key=private_key,
        secrets_log_file=secrets_log_file,
    )

    loop = asyncio.get_event_loop()
    loop.run_until_complete(
        loop.create_datagram_endpoint(
            lambda: HttpServer(
                configuration=configuration,
                session_ticket_fetcher=ticket_store.pop,
                session_ticket_handler=ticket_store.add,
                stateless_retry=args.stateless_retry,
            ),
            local_addr=(args.host, args.port),
        )
    )
    loop.run_forever()