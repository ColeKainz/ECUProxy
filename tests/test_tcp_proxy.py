import socket
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from tcp_ecu.protocol import CommandDispatcher
from tcp_ecu.constants import PROTOCOL_ERROR_BAD_CRC, PROTOCOL_ERROR_UNKNOWN_COMMAND
from tcp_ecu.interfaces.base import ECUError, ECUResponse
from tcp_ecu.ini.generator import generate_proxy_class
from tcp_ecu.ini.config import IniProtocolConfig
from tcp_ecu.tcp.handler import TcpProxyHandler, ThreadingTCPServer
from tests.test_ini_proxy import FIXTURE, RecordingInterface
from tcp_ecu.utils import build_standard_envelope, unwrap_standard_response


class TcpBufferTests(unittest.TestCase):
    def setUp(self):
        self.interface = RecordingInterface()
        self.proxy = generate_proxy_class(IniProtocolConfig(FIXTURE))(self.interface)
        self.handler = object.__new__(TcpProxyHandler)
        self.handler.request = Mock()
        self.handler.server = SimpleNamespace(dispatcher=CommandDispatcher(self.proxy), tcp_mode="auto")

    def sent(self):
        return [call.args[0] for call in self.handler.request.sendall.call_args_list]

    def test_fragmented_raw_read_and_coalesced_commands(self):
        wire = bytes.fromhex("72 03 04 01 02 00 04")
        pending = b""
        for byte in wire[:-1]:
            pending = self.handler.process_buffer(pending + bytes([byte]))
            self.assertEqual(self.interface.calls, [])
        self.assertEqual(self.handler.process_buffer(pending + wire[-1:] + b"FA"), b"")
        self.assertEqual(self.interface.calls, [(wire, 4), (b"F", 3), (b"A", 16)])
        self.assertEqual(self.sent(), [b"xxxx", b"002", b"x" * 16])

    def test_framed_read_split_at_every_byte_then_second_frame(self):
        wire = bytes.fromhex("72 01 05 00 00 00 03")
        frame = build_standard_envelope(wire)
        pending = b""
        for byte in frame[:-1]:
            pending = self.handler.process_buffer(pending + bytes([byte]))
            self.assertEqual(self.interface.calls, [])
        self.assertEqual(self.handler.process_buffer(pending + frame[-1:] + build_standard_envelope(b"F")), b"")
        self.assertEqual(self.interface.calls, [(wire, 3), (b"F", 3)])
        self.assertEqual([bytes(unwrap_standard_response(frame)) for frame in self.sent()], [b"xxx", b"002"])

    def test_bad_crc_and_unknown_commands_never_reach_interface(self):
        frame = bytearray(build_standard_envelope(b"F"))
        frame[-1] ^= 1
        unknown = build_standard_envelope(b"X")
        self.assertEqual(self.handler.process_buffer(bytes(frame) + unknown + build_standard_envelope(b"F")), b"")
        self.assertEqual(self.interface.calls, [(b"F", 3)])
        self.assertEqual(self.sent()[:2], [build_standard_envelope(bytes([PROTOCOL_ERROR_BAD_CRC])),
                                          build_standard_envelope(bytes([PROTOCOL_ERROR_UNKNOWN_COMMAND]))])

    def test_fragmented_write_has_no_early_side_effect(self):
        wire = bytes.fromhex("77 02 04 00 05 00 04 ff 0a 00 46")
        pending = b""
        for byte in wire[:-1]:
            pending = self.handler.process_buffer(pending + bytes([byte]))
            self.assertEqual(self.interface.calls, [])
        self.assertEqual(self.handler.process_buffer(pending + wire[-1:] + b"F"), b"")
        self.assertEqual(self.interface.calls, [(wire, 0), (b"F", 3)])

    def test_fragmented_large_write_is_not_rejected_by_overlapping_table_template(self):
        wire = bytes.fromhex("77 02 04 00 05 00 ff") + b"\xff" * 255
        pending = b""
        for byte in wire[:-1]:
            pending = self.handler.process_buffer(pending + bytes([byte]))
            self.assertEqual(self.interface.calls, [])
            self.assertEqual(self.sent(), [])
        self.assertEqual(self.handler.process_buffer(pending + wire[-1:]), b"")
        self.assertEqual(self.interface.calls, [(wire, 0)])

    def test_malformed_framed_write_is_not_forwarded(self):
        wire = bytes.fromhex("77 02 04 00 05 00 04 ff 0a")
        self.handler.process_buffer(build_standard_envelope(wire))
        self.assertEqual(self.interface.calls, [])

    def test_ecu_status_and_error_preserved(self):
        self.interface.exchange = Mock(return_value=ECUResponse(b"data", status=2))
        self.handler.process_buffer(build_standard_envelope(b"F"))
        self.assertEqual(self.sent()[-1], build_standard_envelope(b"\x02data"))
        self.interface.exchange.side_effect = ECUError(0x84)
        self.handler.process_buffer(build_standard_envelope(b"F"))
        self.assertEqual(self.sent()[-1], build_standard_envelope(b"\x84"))

    def test_logger_response_can_exceed_old_1024_byte_frame_limit(self):
        self.handler.process_buffer(build_standard_envelope(b"r\x00\xf4\x00\x00\x04\x00"))
        self.assertEqual(len(unwrap_standard_response(self.sent()[0])), 1024)

    def test_raw_unknown_command_returns_error(self):
        self.handler.server.tcp_mode = "raw"
        self.handler.process_buffer(b"X")
        self.assertEqual(self.sent(), [b"?"])


class TcpSocketTests(unittest.TestCase):
    def test_local_tcp_connection_dispatches_raw_probe_and_framed_read(self):
        interface = RecordingInterface()
        proxy = generate_proxy_class(IniProtocolConfig(FIXTURE))(interface)
        with ThreadingTCPServer(("127.0.0.1", 0), TcpProxyHandler, proxy) as server:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
            thread.start()
            try:
                with socket.create_connection(server.server_address, timeout=2) as client:
                    client.sendall(b"F")
                    self.assertEqual(client.recv(3), b"002")
                    wire = bytes.fromhex("72 02 04 00 08 00 04")
                    frame = build_standard_envelope(wire)
                    client.sendall(frame[:1])
                    client.sendall(frame[1:])
                    response = bytearray()
                    while len(response) < 11:
                        data = client.recv(11 - len(response))
                        self.assertTrue(data)
                        response.extend(data)
                    self.assertEqual(bytes(unwrap_standard_response(bytes(response))), b"xxxx")
                self.assertEqual(interface.calls, [(b"F", 3), (wire, 4)])
            finally:
                server.shutdown()
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
