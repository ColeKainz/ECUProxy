import struct
import unittest
from pathlib import Path
from unittest.mock import Mock

from main_controller_proxy import MainControllerProxy
from main_controller_proxy_pages import Page8Response
from tcp_ecu.ini.config import IniProtocolConfig, split_values
from tcp_ecu.ini.generator import generate_proxy_source
from tcp_ecu.interfaces.base import ECUResponse
from tcp_ecu.protocol import CommandDispatcher


ROOT = Path(__file__).resolve().parents[1]


class ProjectIniTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = IniProtocolConfig(ROOT / "mainController.ini")

    def test_eighth_page_has_two_unsigned_integer_properties(self):
        self.assertEqual(list(self.config.pages), list(range(1, 9)))
        self.assertTrue(all(self.config.pages[number].size == 1024 for number in range(1, 8)))
        page = self.config.pages[8]
        self.assertEqual(page.size, 8)
        self.assertEqual([(field.name, field.storage, field.offset, field.annotation) for field in page.fields], [
            ("output_shaft_pulse_rate", "U32", 0, "int | None"),
            ("cruise_control_pulse_rate", "U32", 4, "int | None"),
        ])
        self.assertEqual(Page8Response._page_fields, page.fields)

    def test_all_page_command_lists_include_the_new_page(self):
        names = {"pageSize", "pageIdentifier", "burnCommand", "pageReadCommand",
                 "pageValueWrite", "pageChunkWrite", "crc32CheckCommand"}
        entries = {entry.name: split_values(entry.value) for entry in self.config.entries
                   if entry.section == "Constants" and entry.name in names}
        self.assertEqual(set(entries), names)
        for name, values in entries.items():
            with self.subTest(name=name):
                self.assertEqual(len(values), 8)
                if name != "pageSize":
                    self.assertIn(r"\x0d", values[-1])
        self.assertEqual(entries["pageSize"][-1], "8")

    def test_output_channels_alias_page_values_without_extending_telemetry(self):
        for defines in ({}, {"CAN_COMMANDS": False}):
            with self.subTest(defines=defines):
                config = IniProtocolConfig(ROOT / "mainController.ini", defines=defines)
                channels = {entry.name: entry.value for entry in config.entries if entry.section == "OutputChannels"}
                self.assertEqual(channels["outputShaftPulseRate"], '{ output_shaft_pulse_rate }, "Hz"')
                self.assertEqual(channels["cruiseControlPulseRate"], '{ cruise_control_pulse_rate }, "Hz"')
                self.assertEqual(config.och_block_size, 212)

    def test_generated_proxy_matches_the_local_ini(self):
        self.assertEqual((ROOT / "main_controller_proxy.py").read_text(),
                         generate_proxy_source(self.config, "MainControllerProxy",
                                               pages_package="main_controller_proxy_pages"))
        self.assertIs(MainControllerProxy.page_response_types[8], Page8Response)

    def test_full_and_partial_page_reads(self):
        data = struct.pack(">II", 12345, 4294967295)
        page = Page8Response.from_bytes(data)
        self.assertEqual(page.output_shaft_pulse_rate, 12345)
        self.assertEqual(page.cruise_control_pulse_rate, 4294967295)
        for start in range(9):
            for end in range(start, 9):
                with self.subTest(start=start, end=end):
                    page = Page8Response.from_bytes(data[start:end], offset=start)
                    self.assertEqual(page.to_bytes(), data[start:end])
                    self.assertEqual(page.output_shaft_pulse_rate, 12345 if start == 0 and end >= 4 else None)
                    self.assertEqual(page.cruise_control_pulse_rate, 4294967295 if start <= 4 and end == 8 else None)
        page = Page8Response.from_bytes(data[4:], offset=4)
        page.cruise_control_pulse_rate = 250
        self.assertEqual(page.to_bytes(), struct.pack(">I", 250))

    def test_new_page_commands_dispatch_to_interface(self):
        interface = Mock()
        dispatcher = CommandDispatcher(MainControllerProxy(interface))
        data = struct.pack(">II", 100, 200)
        commands = [
            (b"r\x03\x0d\x00\x00\x00\x08", 8, data),
            (b"r\x03\x0d\x00\x04\x00\x04", 4, data[4:]),
            (b"w\x03\x0d\x00\x00\x00\x08" + data, 0, b""),
            (b"b\x03\x0d", 0, b""),
            (b"k\x03\x0d\x00\x00\x00\x04", 4, b"\x01\x02\x03\x04"),
        ]
        for wire, length, payload in commands:
            with self.subTest(wire=wire):
                interface.exchange.return_value = ECUResponse(payload, status=0)
                response = dispatcher.dispatch(wire)
                self.assertEqual(response, payload)
                self.assertEqual(response.status, 0)
                interface.exchange.assert_called_with(wire, length)


if __name__ == "__main__":
    unittest.main()
