import logging
import socketserver
import struct

from tcp_ecu.protocol import CommandDispatcher, IncompleteCommand, UnknownCommand
from tcp_ecu.constants import (
    PROTOCOL_ERROR_BAD_CRC,
    PROTOCOL_ERROR_OUT_OF_RANGE,
    PROTOCOL_ERROR_UNAVAILABLE,
    PROTOCOL_ERROR_UNKNOWN_COMMAND,
    PROTOCOL_OK,
    RECV_TIMEOUT_SECONDS,
    STANDARD_ENVELOPE_MAX_PAYLOAD,
)
from tcp_ecu.interfaces.base import ECUError
from tcp_ecu.utils import build_standard_envelope, crc32


logger = logging.getLogger(__name__)


class TcpProxyHandler(socketserver.BaseRequestHandler):
    def handle(self):
        logger.info("Connected: %s", self.client_address)
        self.request.settimeout(RECV_TIMEOUT_SECONDS)
        buffer = b""
        try:
            while True:
                try:
                    data = self.request.recv(4096)
                except TimeoutError:
                    continue
                if not data:
                    break
                buffer = self.process_buffer(buffer + data)
                if len(buffer) > STANDARD_ENVELOPE_MAX_PAYLOAD + 6:
                    raise ValueError("TCP receive buffer exceeds maximum frame size")
        except (ConnectionError, OSError, ValueError) as exc:
            logger.info("Connection %s closed: %s", self.client_address, exc)
        finally:
            logger.info("Disconnected: %s", self.client_address)

    def process_buffer(self, buffer):
        while buffer:
            if self.server.tcp_mode != "framed":
                try:
                    match = self.server.dispatcher.match(buffer)
                except IncompleteCommand:
                    return buffer
                except UnknownCommand:
                    if self.server.tcp_mode == "raw":
                        self.request.sendall(b"?")
                        return b""
                except ValueError as exc:
                    logger.warning("Invalid raw command: %s", exc)
                    self.request.sendall(b"?")
                    return b""
                else:
                    self._invoke(match, framed=False)
                    buffer = buffer[match.consumed:]
                    continue

            if len(buffer) < 2:
                return buffer
            payload_length = struct.unpack(">H", buffer[:2])[0]
            if not 0 < payload_length <= STANDARD_ENVELOPE_MAX_PAYLOAD:
                raise ValueError(f"Invalid frame payload length: {payload_length}")
            frame_length = payload_length + 6
            if len(buffer) < frame_length:
                return buffer
            payload = buffer[2:2 + payload_length]
            received_crc = struct.unpack(">I", buffer[2 + payload_length:frame_length])[0]
            buffer = buffer[frame_length:]
            if received_crc != crc32(payload):
                self._send_error(PROTOCOL_ERROR_BAD_CRC, framed=True)
                continue
            try:
                match = self.server.dispatcher.match(payload, complete=True)
            except UnknownCommand:
                self._send_error(PROTOCOL_ERROR_UNKNOWN_COMMAND, framed=True)
            except ValueError as exc:
                logger.warning("Invalid framed command: %s", exc)
                self._send_error(PROTOCOL_ERROR_OUT_OF_RANGE, framed=True)
            else:
                self._invoke(match, framed=True)
        return buffer

    def _invoke(self, match, *, framed):
        try:
            response = match.invoke()
            if framed:
                status = getattr(response, "status", PROTOCOL_OK)
                response = build_standard_envelope(bytes([status]) + response)
        except ECUError as exc:
            self._send_error(exc.status, framed=framed)
            return
        except (RuntimeError, OSError) as exc:
            logger.warning("ECU exchange failed: %s", exc)
            self._send_error(PROTOCOL_ERROR_UNAVAILABLE, framed=framed)
            return
        except (ValueError, TypeError) as exc:
            logger.warning("Command failed: %s", exc)
            self._send_error(PROTOCOL_ERROR_OUT_OF_RANGE, framed=framed)
            return
        self.request.sendall(response)

    def _send_error(self, code, *, framed):
        self.request.sendall(build_standard_envelope(bytes([code])) if framed else b"?")


class ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address, handler_class, ecu_proxy, *, tcp_mode="auto"):
        if tcp_mode not in {"auto", "raw", "framed"}:
            raise ValueError(f"Invalid TCP mode: {tcp_mode}")
        self.ecu_proxy = ecu_proxy
        self.dispatcher = CommandDispatcher(ecu_proxy)
        self.tcp_mode = tcp_mode
        super().__init__(server_address, handler_class)
