import struct
import threading
import unittest
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from tcp_ecu.interfaces import serial as serial_ecu_interface
from tcp_ecu.interfaces.base import ECUError, ECUInterface, ECUResponse
from tcp_ecu.interfaces.serial import MAX_RAW_RESPONSE_LENGTH, SerialECUInterface
from tcp_ecu.interfaces.serial_proxy import SerialEcuProxy
from tcp_ecu.utils import build_standard_envelope


def mock_port(*responses, chunk_size=None):
    """Mock pyserial: each write selects its response, and reads consume bytes."""
    pending = deque(responses)
    incoming = bytearray()
    port = Mock()

    def write(data):
        incoming.extend(pending.popleft() if pending else b"")
        return len(data)

    def read(size):
        if chunk_size is not None:
            size = min(size, chunk_size)
        result = bytes(incoming[:size])
        del incoming[:size]
        return result

    port.write.side_effect = write
    port.read.side_effect = read
    port.reset_input_buffer.side_effect = incoming.clear
    return port


class ECUInterfaceTests(unittest.TestCase):
    def test_interface_is_abstract_and_lifecycle_is_optional(self):
        with self.assertRaises(TypeError):
            ECUInterface()

        class MemoryECU(ECUInterface):
            def exchange(self, command, response_length):
                return command

        ecu = MemoryECU()
        self.assertIsNone(ecu.start())
        self.assertEqual(ecu.exchange(b"test", 4), b"test")
        self.assertIsNone(ecu.close())

    def test_response_is_bytes_with_status(self):
        response = ECUResponse(b"data", status=0x7F)
        self.assertIsInstance(response, bytes)
        self.assertEqual(response, b"data")
        self.assertEqual(response.status, 0x7F)
        self.assertEqual(ECUResponse(b"raw").status, 0)

    def test_error_preserves_status_and_optional_message(self):
        error = ECUError(0x83)
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(error.status, 0x83)
        self.assertIn("0x83", str(error))
        self.assertEqual(str(ECUError(0x80, "ECU busy")), "ECU busy")

    def test_compatibility_alias(self):
        self.assertIs(SerialEcuProxy, SerialECUInterface)
        self.assertTrue(issubclass(SerialECUInterface, ECUInterface))


class SerialECUInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.config = SimpleNamespace(
            signature=b"test ECU",
            command_for_autodetect=Mock(return_value=b"custom query"),
            autodetect_response_length=8,
        )
        self.serial = Mock()
        self.serial_patch = patch.object(serial_ecu_interface, "serial", self.serial)
        self.serial_patch.start()
        self.addCleanup(self.serial_patch.stop)
        self.ports_patch = patch.object(serial_ecu_interface, "list_ports", Mock())
        self.ports_patch.start()
        self.addCleanup(self.ports_patch.stop)
        self.ecu = SerialECUInterface(self.config, port="mock-port", baud=57600)
        self.addCleanup(self.ecu.close)

    def connect(self, *responses, framed=False, chunk_size=None):
        port = mock_port(*responses, chunk_size=chunk_size)
        self.ecu.connection = port
        self.ecu.connected_port = "mock-port"
        self.ecu.uses_protocol_002 = framed
        return port

    def test_unavailable_exchange(self):
        with self.assertRaisesRegex(RuntimeError, "auto-detect has not completed"):
            self.ecu.exchange(b"Q", 8)
        self.serial.Serial.assert_not_called()

    def test_explicit_length_is_used_without_parsing_commands(self):
        for command in (b"Q", b"S", b"F", b"r\x00\x00\x00\x00\x00\x05", b"custom"):
            with self.subTest(command=command):
                port = self.connect(b"ab")
                response = self.ecu.exchange(command, 2)
                self.assertEqual(response, b"ab")
                self.assertEqual(response.status, 0)
                port.write.assert_called_once_with(command)
                port.read.assert_called_once_with(2)
                port.reset_input_buffer.assert_called_once_with()
                port.flush.assert_called_once_with()

    def test_unknown_raw_length_preserves_binary_and_newlines(self):
        data = b"\x00first\nsecond\r\n\xff"
        port = self.connect(data)
        self.assertEqual(self.ecu.exchange(b"Q", None), data)
        port.read.assert_called_once_with(65534)
        port.read_until.assert_not_called()

    def test_unknown_raw_length_is_bounded(self):
        port = self.connect(b"x" * 65535)
        self.assertEqual(len(self.ecu.exchange(b"custom", None)), 65534)
        port.read.assert_called_once_with(MAX_RAW_RESPONSE_LENGTH)

    def test_unknown_raw_timeout_can_return_empty(self):
        self.connect(b"")
        self.assertEqual(self.ecu.exchange(b"custom", None), b"")
        self.assertIsNotNone(self.ecu.connection)

    def test_exact_read_handles_fragmented_data(self):
        port = self.connect(b"abcde", chunk_size=2)
        self.assertEqual(self.ecu.exchange(b"custom", 5), b"abcde")
        self.assertEqual(port.read.call_args_list, [call(5), call(3), call(1)])

    def test_short_known_raw_response_drops_connection(self):
        port = self.connect(b"ab")
        with self.assertRaisesRegex(RuntimeError, "have=2 need=3") as caught:
            self.ecu.exchange(b"custom", 3)
        self.assertIsInstance(caught.exception.__cause__, TimeoutError)
        self.assertIsNone(self.ecu.connection)
        self.assertIsNone(self.ecu.connected_port)
        port.close.assert_called_once_with()

    def test_zero_length_raw_response_does_not_read(self):
        port = self.connect()
        self.assertEqual(self.ecu.exchange(b"write", 0), b"")
        port.read.assert_not_called()

    def test_invalid_arguments_do_not_disconnect(self):
        port = self.connect()
        for length, error in ((-1, ValueError), ("3", TypeError), (1.5, TypeError)):
            with self.subTest(length=length), self.assertRaises(error):
                self.ecu.exchange(b"Q", length)
        with self.assertRaises(TypeError):
            self.ecu.exchange("Q", 1)
        self.assertIs(self.ecu.connection, port)
        port.write.assert_not_called()

    def test_framed_command_is_wrapped_once_and_data_length_excludes_status(self):
        port = self.connect(build_standard_envelope(b"\x00abc"), framed=True)
        self.assertEqual(self.ecu.exchange(b"custom", 3), b"abc")
        port.write.assert_called_once_with(build_standard_envelope(b"custom"))

    def test_protocol_probe_is_always_raw(self):
        for length in (3, None):
            with self.subTest(length=length):
                port = self.connect(b"002", framed=True)
                self.assertEqual(self.ecu.exchange(b"F", length), b"002")
                port.write.assert_called_once_with(b"F")
                self.assertTrue(self.ecu.uses_protocol_002)

    def test_all_non_error_statuses_are_preserved(self):
        for status in range(0x80):
            with self.subTest(status=status):
                self.connect(build_standard_envelope(bytes([status]) + b"data"), framed=True)
                response = self.ecu.exchange(b"Q", 4)
                self.assertEqual(response, b"data")
                self.assertEqual(response.status, status)

    def test_all_ecu_error_statuses_survive_and_do_not_disconnect(self):
        for status in range(0x80, 0x100):
            with self.subTest(status=status):
                port = self.connect(build_standard_envelope(bytes([status])), framed=True)
                with self.assertRaises(ECUError) as caught:
                    self.ecu.exchange(b"Q", 100)
                self.assertEqual(caught.exception.status, status)
                self.assertIs(self.ecu.connection, port)
                self.assertTrue(self.ecu.uses_protocol_002)
                port.close.assert_not_called()

    def test_configuration_status_preserves_variable_length_diagnostic(self):
        diagnostic = b"Configuration error\nCheck engine settings\x00"
        port = self.connect(build_standard_envelope(b"\x03" + diagnostic), framed=True)
        response = self.ecu.exchange(b"A", 200)
        self.assertEqual(response, diagnostic)
        self.assertEqual(response.status, 0x03)
        self.assertIs(self.ecu.connection, port)

    def test_exchange_can_succeed_after_ecu_error(self):
        self.connect(
            build_standard_envelope(b"\x80"),
            build_standard_envelope(b"\x01ok"),
            framed=True,
        )
        with self.assertRaises(ECUError):
            self.ecu.exchange(b"Q", 2)
        response = self.ecu.exchange(b"Q", 2)
        self.assertEqual(response, b"ok")
        self.assertEqual(response.status, 1)

    def test_unknown_framed_length_uses_envelope(self):
        self.connect(build_standard_envelope(b"\x7f\x00\n\xff"), framed=True)
        response = self.ecu.exchange(b"custom", None)
        self.assertEqual(response, b"\x00\n\xff")
        self.assertEqual(response.status, 0x7F)

    def test_framed_zero_length_data_is_valid(self):
        self.connect(build_standard_envelope(b"\x01"), framed=True)
        response = self.ecu.exchange(b"write", 0)
        self.assertEqual(response, b"")
        self.assertEqual(response.status, 1)

    def test_fragmented_frame_is_reassembled(self):
        self.connect(build_standard_envelope(b"\x00abc"), framed=True, chunk_size=1)
        self.assertEqual(self.ecu.exchange(b"custom", 3), b"abc")

    def test_framed_expected_length_must_match(self):
        for expected in (0, 2, 4):
            with self.subTest(expected=expected):
                port = self.connect(build_standard_envelope(b"\x00abc"), framed=True)
                with self.assertRaisesRegex(RuntimeError, "unexpected framed response data length"):
                    self.ecu.exchange(b"Q", expected)
                port.close.assert_called_once_with()

    def test_malformed_frames_drop_connection(self):
        frame = build_standard_envelope(b"\x00abc")
        bad_crc = frame[:-1] + bytes([frame[-1] ^ 1])
        cases = (
            (b"", "framed response length"),
            (b"\x00", "framed response length"),
            (b"\x00\x00", "invalid framed response length"),
            (frame[:-1], "framed response body"),
            (bad_crc, "bad framed response CRC"),
        )
        for response, message in cases:
            with self.subTest(message=message, response=response):
                port = self.connect(response, framed=True)
                with self.assertRaisesRegex(RuntimeError, message):
                    self.ecu.exchange(b"Q", None)
                self.assertIsNone(self.ecu.connection)
                self.assertFalse(self.ecu.uses_protocol_002)
                port.close.assert_called_once_with()

    def test_crc_is_checked_before_interpreting_error_status(self):
        frame = build_standard_envelope(b"\xff")
        self.connect(frame[:-1] + bytes([frame[-1] ^ 1]), framed=True)
        with self.assertRaisesRegex(RuntimeError, "bad framed response CRC") as caught:
            self.ecu.exchange(b"Q", None)
        self.assertNotIsInstance(caught.exception, ECUError)

    def test_maximum_framed_payload(self):
        data = b"x" * 65534
        port = self.connect(build_standard_envelope(b"\x7f" + data), framed=True)
        response = self.ecu.exchange(b"Q", len(data))
        self.assertEqual(response, data)
        self.assertEqual(response.status, 0x7F)
        self.assertEqual(port.read.call_args_list, [call(2), call(65539)])

    def test_configured_frame_limit_is_enforced(self):
        self.connect(struct.pack(">H", 17), framed=True)
        with patch.object(serial_ecu_interface, "STANDARD_ENVELOPE_MAX_PAYLOAD", 16):
            with self.assertRaisesRegex(RuntimeError, "invalid framed response length 17"):
                self.ecu.exchange(b"Q", None)

    def test_short_write_drops_connection(self):
        port = self.connect()
        port.write.side_effect = None
        port.write.return_value = 1
        with self.assertRaisesRegex(RuntimeError, "short serial command write"):
            self.ecu.exchange(b"custom", 0)
        self.assertIsNone(self.ecu.connection)
        port.read.assert_not_called()

    def test_io_error_and_close_error_do_not_mask_original_failure(self):
        port = self.connect()
        port.read.side_effect = OSError("unplugged")
        port.close.side_effect = OSError("already gone")
        with self.assertRaisesRegex(RuntimeError, "unplugged"):
            self.ecu.exchange(b"custom", 1)
        self.assertIsNone(self.ecu.connection)

    def test_legacy_autodetect_uses_config_command_and_length(self):
        port = mock_port(b"001", self.config.signature)
        self.serial.Serial.return_value = port
        self.assertTrue(self.ecu._try_connect("mock-port"))
        self.assertIs(self.ecu.connection, port)
        self.assertEqual(self.ecu.connected_port, "mock-port")
        self.assertFalse(self.ecu.uses_protocol_002)
        self.assertEqual(self.ecu.last_error, "")
        self.assertEqual(port.write.call_args_list, [call(b"F"), call(b"custom query")])
        self.assertEqual(port.read.call_args_list, [call(3), call(8)])
        self.serial.Serial.assert_called_once_with(
            port="mock-port", baudrate=57600,
            timeout=serial_ecu_interface.READ_TIMEOUT_SECONDS,
            write_timeout=serial_ecu_interface.READ_TIMEOUT_SECONDS,
        )

    def test_protocol_002_autodetect_wraps_only_signature_query(self):
        port = mock_port(b"002", build_standard_envelope(b"\x7f" + self.config.signature))
        self.serial.Serial.return_value = port
        self.assertTrue(self.ecu._try_connect("mock-port"))
        self.assertTrue(self.ecu.uses_protocol_002)
        self.assertEqual(
            port.write.call_args_list,
            [call(b"F"), call(build_standard_envelope(b"custom query"))],
        )

    def test_autodetect_accepts_fixed_length_nul_padded_signature(self):
        self.config.signature = b"test ECU signature"
        self.config.autodetect_response_length = 20
        self.config.command_for_autodetect.return_value = b"r\x00\x0f\x00\x00\x00\x14"
        for framed in (False, True):
            with self.subTest(framed=framed):
                response = self.config.signature + b"\x00\x00"
                if framed:
                    response = build_standard_envelope(b"\x00" + response)
                port = mock_port(b"002" if framed else b"001", response)
                self.serial.Serial.return_value = port
                self.assertTrue(self.ecu._try_connect("mock-port"))
                self.assertIs(self.ecu.connection, port)
                with self.ecu.lock:
                    self.ecu._drop_connection("next test")

    def test_padded_signature_still_requires_complete_configured_length(self):
        self.config.autodetect_response_length = len(self.config.signature) + 2
        port = mock_port(b"001", self.config.signature)
        self.serial.Serial.return_value = port
        self.assertFalse(self.ecu._try_connect("mock-port"))
        self.assertIn("timed out", self.ecu.last_error)
        port.close.assert_called_once_with()

    def test_legacy_autodetect_can_follow_silent_protocol_probe(self):
        port = mock_port(b"", self.config.signature)
        self.serial.Serial.return_value = port
        self.assertTrue(self.ecu._try_connect("mock-port"))
        self.assertFalse(self.ecu.uses_protocol_002)

    def test_autodetect_accepts_unknown_configured_length(self):
        self.config.autodetect_response_length = None
        self.config.signature = b"binary\nECU\x00data"
        port = mock_port(b"001", self.config.signature)
        self.serial.Serial.return_value = port
        self.assertTrue(self.ecu._try_connect("mock-port"))
        self.assertEqual(port.read.call_args_list, [call(3), call(65534)])

    def test_unvalidated_connection_is_not_published(self):
        port = mock_port(b"002", build_standard_envelope(b"\x00" + self.config.signature))
        self.serial.Serial.return_value = port
        original_read = port.read.side_effect

        def read(size):
            self.assertIsNone(self.ecu.connection)
            self.assertIsNone(self.ecu.connected_port)
            self.assertFalse(self.ecu.uses_protocol_002)
            with self.assertRaises(RuntimeError):
                self.ecu.exchange(b"other", 0)
            return original_read(size)

        port.read.side_effect = read
        self.assertTrue(self.ecu._try_connect("mock-port"))
        self.assertIs(self.ecu.connection, port)

    def test_failed_autodetect_closes_candidate_and_preserves_disconnected_state(self):
        cases = (
            (b"001", b"bad ECU!"),
            (b"001", b"short"),
            (b"002", build_standard_envelope(b"\x80")),
            (b"002", build_standard_envelope(b"\x00bad ECU!")),
            (b"002", b"\x00\x00"),
        )
        for probe, signature in cases:
            with self.subTest(probe=probe, signature=signature):
                port = mock_port(probe, signature)
                self.serial.Serial.return_value = port
                self.assertFalse(self.ecu._try_connect("mock-port"))
                self.assertIsNone(self.ecu.connection)
                self.assertIsNone(self.ecu.connected_port)
                self.assertFalse(self.ecu.uses_protocol_002)
                self.assertIn("auto-detect failed", self.ecu.last_error)
                port.close.assert_called_once_with()

    def test_open_failure_is_reported(self):
        self.serial.Serial.side_effect = OSError("busy")
        self.assertFalse(self.ecu._try_connect("mock-port"))
        self.assertIn("busy", self.ecu.last_error)
        self.assertIsNone(self.ecu.connection)

    def test_close_during_autodetect_does_not_publish_candidate(self):
        port = mock_port(b"002", build_standard_envelope(b"\x00" + self.config.signature))
        self.serial.Serial.return_value = port
        self.config.command_for_autodetect.side_effect = lambda: (self.ecu.close() or b"custom query")
        self.assertFalse(self.ecu._try_connect("mock-port"))
        self.assertIsNone(self.ecu.connection)
        self.assertFalse(self.ecu.uses_protocol_002)
        self.assertEqual(self.ecu.last_error, "serial interface is closed")
        port.close.assert_called_once_with()

    def test_close_is_idempotent_and_prevents_restart(self):
        port = self.connect()
        self.ecu.close()
        self.ecu.close()
        self.ecu.start()
        self.assertTrue(self.ecu.stop_event.is_set())
        self.assertIsNone(self.ecu._thread)
        self.assertFalse(self.ecu._try_connect("mock-port"))
        port.close.assert_called_once_with()
        self.serial.Serial.assert_not_called()

    def test_stop_signal_prevents_new_exchanges(self):
        port = self.connect()
        self.ecu.stop_event.set()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            self.ecu.exchange(b"Q", 1)
        port.write.assert_not_called()

    def test_exchanges_hold_lock_through_complete_response(self):
        reading = threading.Event()
        release_read = threading.Event()
        second_waiting = threading.Event()
        second_thread = None
        real_lock = self.ecu.lock
        port = self.connect(b"a", b"b")
        original_read = port.read.side_effect

        class ObservedLock:
            def __enter__(self):
                if threading.current_thread() is second_thread:
                    second_waiting.set()
                real_lock.acquire()

            def __exit__(self, *args):
                real_lock.release()

        def read(size):
            if not reading.is_set():
                reading.set()
                if not release_read.wait(2):
                    raise TimeoutError("test did not release the reader")
            return original_read(size)

        def second_exchange():
            nonlocal second_thread
            second_thread = threading.current_thread()
            return self.ecu.exchange(b"second", 1)

        port.read.side_effect = read
        with patch.object(self.ecu, "lock", ObservedLock()), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.ecu.exchange, b"first", 1)
            try:
                self.assertTrue(reading.wait(2))
                second = pool.submit(second_exchange)
                self.assertTrue(second_waiting.wait(2))
                port.write.assert_called_once_with(b"first")
            finally:
                release_read.set()
            self.assertEqual(first.result(timeout=2), b"a")
            self.assertEqual(second.result(timeout=2), b"b")
        self.assertEqual(port.write.call_args_list, [call(b"first"), call(b"second")])

    def test_close_joins_autodetect_and_closes_unpublished_candidate(self):
        reading = threading.Event()
        release_read = threading.Event()
        stopped = threading.Event()
        port = mock_port(b"002", build_standard_envelope(b"\x00" + self.config.signature))
        self.serial.Serial.return_value = port
        original_read = port.read.side_effect
        original_set = self.ecu.stop_event.set

        def read(size):
            reading.set()
            if not release_read.wait(2):
                raise TimeoutError("test did not release autodetect")
            return original_read(size)

        def stop():
            original_set()
            stopped.set()

        port.read.side_effect = read
        with patch.object(self.ecu.stop_event, "set", side_effect=stop), \
                ThreadPoolExecutor(max_workers=1) as pool:
            self.ecu.start()
            try:
                self.assertTrue(reading.wait(2))
                closing = pool.submit(self.ecu.close)
                self.assertTrue(stopped.wait(2))
                self.assertIsNone(self.ecu.connection)
                self.assertFalse(closing.done())
            finally:
                release_read.set()
            closing.result(timeout=2)
        self.assertFalse(self.ecu._thread.is_alive())
        self.assertIsNone(self.ecu.connection)
        port.write.assert_called_once_with(b"F")
        port.close.assert_called_once_with()

    def test_start_is_idempotent_and_close_interrupts_retry_wait(self):
        waiting = threading.Event()
        original_wait = self.ecu.stop_event.wait

        def wait(timeout):
            waiting.set()
            return original_wait(timeout)

        with patch.object(self.ecu, "_candidate_ports", return_value=[]), \
                patch.object(self.ecu.stop_event, "wait", side_effect=wait), \
                patch.object(serial_ecu_interface, "SERIAL_RETRY_SECONDS", 60):
            self.ecu.start()
            self.assertTrue(waiting.wait(2))
            thread = self.ecu._thread
            self.ecu.start()
            self.assertIs(self.ecu._thread, thread)
            closer = threading.Thread(target=self.ecu.close, daemon=True)
            closer.start()
            closer.join(2)
            self.assertFalse(closer.is_alive())
            self.assertFalse(thread.is_alive())

    def test_connection_loop_reconnects_after_failed_exchange(self):
        first = mock_port(b"001", self.config.signature, b"short")
        second = mock_port(b"002", build_standard_envelope(b"\x00" + self.config.signature))
        self.serial.Serial.side_effect = [first, second]
        waits = 0

        def wait(timeout):
            nonlocal waits
            waits += 1
            if waits == 1:
                self.assertIs(self.ecu.connection, first)
                with self.assertRaises(RuntimeError):
                    self.ecu.exchange(b"custom", 10)
            else:
                self.assertIs(self.ecu.connection, second)
                self.ecu.stop_event.set()
            return self.ecu.stop_event.is_set()

        with patch.object(self.ecu.stop_event, "wait", side_effect=wait):
            self.ecu._connection_loop()
        self.assertEqual(self.serial.Serial.call_count, 2)
        self.assertTrue(self.ecu.uses_protocol_002)
        first.close.assert_called_once_with()

    def test_missing_pyserial_waits_until_stopped(self):
        def wait(timeout):
            self.assertIn("pyserial is not installed", self.ecu.last_error)
            self.ecu.stop_event.set()
            return True

        with patch.object(serial_ecu_interface, "serial", None), \
                patch.object(self.ecu.stop_event, "wait", side_effect=wait) as waiting:
            self.ecu._connection_loop()
        waiting.assert_called_once_with(serial_ecu_interface.SERIAL_RETRY_SECONDS)


if __name__ == "__main__":
    unittest.main()
