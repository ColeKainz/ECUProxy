import glob
import struct
import threading

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = None
    list_ports = None

from tcp_ecu.constants import (
    BAUD_RATE,
    READ_TIMEOUT_SECONDS,
    SERIAL_PROTOCOL_VERSION_COMMAND,
    SERIAL_PROTOCOL_VERSION_RESPONSE,
    SERIAL_RETRY_SECONDS,
    STANDARD_ENVELOPE_MAX_PAYLOAD,
)
from tcp_ecu.interfaces.base import ECUError, ECUInterface, ECUResponse
from tcp_ecu.utils import build_standard_envelope, unwrap_standard_response


MAX_RAW_RESPONSE_LENGTH = 65534


class SerialECUInterface(ECUInterface):
    def __init__(self, config, port=None, baud=BAUD_RATE):
        self.config = config
        self.requested_port = port
        self.baud = baud
        self.connection = None
        self.connected_port = None
        self.uses_protocol_002 = False
        self.last_error = "serial auto-detect has not completed"
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self._thread = None

    def start(self):
        with self.lock:
            if self.stop_event.is_set() or (self._thread is not None and self._thread.is_alive()):
                return
            self._thread = threading.Thread(target=self._connection_loop, daemon=True)
            self._thread.start()

    def close(self):
        self.stop_event.set()
        with self.lock:
            self._drop_connection("serial interface is closed")
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()

    def exchange(self, command: bytes, response_length: int | None) -> bytes:
        if not isinstance(command, bytes):
            raise TypeError("command must be bytes")
        if response_length is not None:
            if not isinstance(response_length, int):
                raise TypeError("response_length must be an integer or None")
            if response_length < 0:
                raise ValueError("response_length must not be negative")

        with self.lock:
            if self.stop_event.is_set():
                raise RuntimeError("serial interface is closed")
            if self.connection is None:
                raise RuntimeError(self.last_error)
            try:
                return self._exchange_on_connection(
                    self.connection, command, response_length, self.uses_protocol_002
                )
            except ECUError:
                raise
            except Exception as exc:
                self._drop_connection(f"serial exchange failed on {self.connected_port}: {exc}")
                raise RuntimeError(self.last_error) from exc

    @staticmethod
    def _close_serial(connection):
        try:
            connection.close()
        except Exception:
            # A broken port must not mask the exchange failure or prevent shutdown.
            pass

    def _drop_connection(self, error):
        connection = self.connection
        self.connection = None
        self.connected_port = None
        self.uses_protocol_002 = False
        self.last_error = error
        if connection is not None:
            self._close_serial(connection)

    def _connection_loop(self):
        while not self.stop_event.is_set():
            with self.lock:
                connected = self.connection is not None
            if not connected:
                if serial is None:
                    with self.lock:
                        if not self.stop_event.is_set():
                            self.last_error = "pyserial is not installed; install it with `python3 -m pip install pyserial`"
                else:
                    try:
                        for port in self._candidate_ports():
                            if self.stop_event.is_set():
                                return
                            if self._try_connect(port):
                                break
                    except Exception as exc:
                        with self.lock:
                            if not self.stop_event.is_set():
                                self.last_error = f"serial auto-detect failed: {exc}"
            self.stop_event.wait(SERIAL_RETRY_SECONDS)

    def _candidate_ports(self):
        if self.requested_port:
            return [self.requested_port]
        if list_ports is not None:
            ports = [port.device for port in list_ports.comports()]
            if ports:
                return ports
        return sorted(
            glob.glob("/dev/tty.usb*")
            + glob.glob("/dev/cu.usb*")
            + glob.glob("/dev/tty.*")
            + glob.glob("/dev/cu.*")
        )

    def _try_connect(self, port):
        with self.lock:
            if self.stop_event.is_set() or self.connection is not None:
                return False

        connection = None
        detected = False
        try:
            connection = serial.Serial(
                port=port,
                baudrate=self.baud,
                timeout=READ_TIMEOUT_SECONDS,
                write_timeout=READ_TIMEOUT_SECONDS,
            )
            response, uses_protocol_002 = self._autodetect(connection)
            if response.rstrip(b"\x00") != self.config.signature:
                raise ValueError(
                    f"signature mismatch: expected={self.config.signature!r} received={response!r}"
                )
            with self.lock:
                if self.stop_event.is_set() or self.connection is not None:
                    return False
                self.connected_port = port
                self.uses_protocol_002 = uses_protocol_002
                self.last_error = ""
                self.connection = connection
                detected = True
            return True
        except Exception as exc:
            with self.lock:
                if not self.stop_event.is_set() and self.connection is None:
                    self.last_error = f"serial auto-detect failed on {port}: {exc}"
            return False
        finally:
            if connection is not None and not detected:
                self._close_serial(connection)

    def _autodetect(self, connection):
        try:
            protocol_response = self._exchange_on_connection(
                connection,
                SERIAL_PROTOCOL_VERSION_COMMAND,
                len(SERIAL_PROTOCOL_VERSION_RESPONSE),
                False,
            )
        except TimeoutError:
            # Legacy ECUs may ignore the protocol-version probe entirely.
            protocol_response = b""
        if self.stop_event.is_set():
            raise RuntimeError("serial interface is closed")

        uses_protocol_002 = protocol_response == SERIAL_PROTOCOL_VERSION_RESPONSE
        response = self._exchange_on_connection(
            connection,
            self.config.command_for_autodetect(),
            self.config.autodetect_response_length,
            uses_protocol_002,
        )
        return response, uses_protocol_002

    def _exchange_on_connection(self, connection, command, response_length, uses_protocol_002):
        framed = uses_protocol_002 and command != SERIAL_PROTOCOL_VERSION_COMMAND
        if framed:
            if not 1 <= len(command) <= STANDARD_ENVELOPE_MAX_PAYLOAD:
                raise ValueError(f"invalid framed command length {len(command)}")
            request = build_standard_envelope(command)
        else:
            request = command

        connection.reset_input_buffer()
        if connection.write(request) != len(request):
            raise TimeoutError("short serial command write")
        connection.flush()

        if framed:
            return self._read_framed_response(connection, response_length)
        if response_length is None:
            # pyserial read(size) waits for size bytes or timeout, including newlines.
            return ECUResponse(connection.read(MAX_RAW_RESPONSE_LENGTH))
        return ECUResponse(self._read_exact(connection, response_length, "raw response"))

    @staticmethod
    def _read_exact(connection, length, description):
        data = bytearray()
        while len(data) < length:
            chunk = connection.read(length - len(data))
            if not chunk:
                raise TimeoutError(
                    f"timed out waiting for {description}; have={len(data)} need={length}"
                )
            data.extend(chunk)
        return bytes(data)

    def _read_framed_response(self, connection, response_length):
        header = self._read_exact(connection, 2, "framed response length")
        payload_length = struct.unpack(">H", header)[0]
        if not 1 <= payload_length <= STANDARD_ENVELOPE_MAX_PAYLOAD:
            raise ValueError(f"invalid framed response length {payload_length}")
        rest = self._read_exact(connection, payload_length + 4, "framed response body")
        response = unwrap_standard_response(header + rest)
        # Configuration-error status carries variable-length diagnostic text.
        if response.status != 0x03 and response_length is not None and len(response) != response_length:
            raise ValueError(
                f"unexpected framed response data length; have={len(response)} need={response_length}"
            )
        return response
