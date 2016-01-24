from __future__ import (absolute_import, print_function, division)

import inspect
import socket
from io import BytesIO

import logging
logging.getLogger("hyper.packages.hpack.hpack").setLevel(logging.WARNING)

import netlib
from netlib import tservers as netlib_tservers

import h2

from libmproxy import utils
import tservers

class SimpleHttp2Handler(netlib.tcp.BaseHandler):
    sni = None

    def handle_sni(self, connection):
        self.sni = connection.get_servername()

    def handle(self):
        h2_conn = h2.connection.H2Connection(client_side=False)

        preamble = self.rfile.read(24)
        h2_conn.initiate_connection()
        h2_conn.receive_data(preamble)
        self.wfile.write(h2_conn.data_to_send())
        self.wfile.flush()

        while True:
            events = h2_conn.receive_data(utils.http2_read_frame(self.rfile))
            self.wfile.write(h2_conn.data_to_send())
            self.wfile.flush()

            for event in events:
                if isinstance(event, h2.events.RequestReceived):
                    h2_conn.send_headers(1, [
                        (':status', '200'),
                        ('foo', 'bar'),
                    ])
                    h2_conn.send_data(1, b'foobar')
                    h2_conn.end_stream(1)
                    self.wfile.write(h2_conn.data_to_send())
                    self.wfile.flush()
                elif isinstance(event, h2.events.ConnectionTerminated):
                    return

class SimpleHttp2Server(netlib_tservers.ServerTestBase):
    ssl = dict(
        alpn_select=b'h2',
    )
    handler = SimpleHttp2Handler

class TestHttp2(tservers.ProxTestBase):
    ssl = True

    def test_simple(self):
        self.config.http2 = True

        server = SimpleHttp2Server()
        server.setup_class()

        client = netlib.tcp.TCPClient(("127.0.0.1", self.proxy.port))
        client.connect()
        client.wfile.write(
            b"CONNECT localhost:%d HTTP/1.1\r\n"
            b"Host: localhost:%d\r\n"
            b"\r\n" % (server.port, server.port)
        )
        client.wfile.flush()

        # read CONNECT response
        while client.rfile.readline() != "\r\n":
            pass

        client.convert_to_ssl(alpn_protos=[b'h2'])

        h2_conn = h2.connection.H2Connection(client_side=True)
        h2_conn.initiate_connection()
        client.wfile.write(h2_conn.data_to_send())
        client.wfile.flush()

        h2_conn.send_headers(stream_id=1, end_stream=True, headers=[
            (':authority', "127.0.0.1:%s" % server.port),
            (':method', 'GET'),
            (':scheme', 'https'),
            (':path', '/'),
        ])
        client.wfile.write(h2_conn.data_to_send())
        client.wfile.flush()

        done = False
        while not done:
            events = h2_conn.receive_data(utils.http2_read_frame(client.rfile))
            client.wfile.write(h2_conn.data_to_send())
            client.wfile.flush()

            for event in events:
                if isinstance(event, h2.events.StreamEnded):
                    done = True

        h2_conn.close_connection()
        client.wfile.write(h2_conn.data_to_send())
        client.wfile.flush()

        server.teardown_class()

        assert len(self.master.state.flows) == 1
        assert self.master.state.flows[0].response.status_code == 200
        assert self.master.state.flows[0].response.headers['foo'] == 'bar'
        assert self.master.state.flows[0].response.body == b'foobar'
