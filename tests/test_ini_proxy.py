import inspect
import tempfile
import unittest
from pathlib import Path

from tcp_ecu.protocol import CommandDispatcher
from tcp_ecu.interfaces.base import ECUInterface
from tcp_ecu.ini.generator import generate_proxy_class, generate_proxy_source
from tcp_ecu.ini.config import IniProtocolConfig


FIXTURE = Path(__file__).parent / "fixtures" / "demo.ini"


class RecordingInterface(ECUInterface):
    def __init__(self):
        self.calls = []

    def exchange(self, command, response_length):
        self.calls.append((command, response_length))
        if command == b"F":
            return b"002"
        return b"x" * (response_length or 0)


class IniProxyTests(unittest.TestCase):
    def setUp(self):
        self.config = IniProtocolConfig(FIXTURE)
        self.interface = RecordingInterface()
        self.proxy_class = generate_proxy_class(self.config, "TestECUProxy")
        self.proxy = self.proxy_class(self.interface)
        self.dispatcher = CommandDispatcher(self.proxy)

    def config_from_text(self, text, *, encoding="utf-8"):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "test.ini"
        path.write_text(text, encoding=encoding)
        return IniProtocolConfig(path)

    def test_utf8_bom_and_legacy_latin1_preserve_ini_bytes(self):
        text = '''
; Temperature units: \u00b0C
[MegaTune]
queryCommand = "Q"
signature = "ECU \u00b0C"
[ControllerCommands]
cmdHighByte = "\u00ff"
'''
        for encoding in ("utf-8", "utf-8-sig", "latin1"):
            with self.subTest(encoding=encoding):
                config = self.config_from_text(text, encoding=encoding)
                self.assertEqual(config.signature, b"ECU \xb0C")
                self.assertEqual(config.command_for_autodetect(), b"Q")
                proxy = generate_proxy_class(config)(self.interface)
                CommandDispatcher(proxy).dispatch(b"\xff")
                self.assertEqual(self.interface.calls[-1], (b"\xff", None))

    def test_generation_and_dispatch_preserve_example_request(self):
        wire = bytes.fromhex("72 03 04 01 02 00 10")
        match = self.dispatcher.match(wire, complete=True)
        self.assertEqual(match.method.__name__, "page_read_command_1")
        self.assertEqual(match.arguments, {"ts_can_id": 3, "offset": 258, "count": 16, "response_length": 16})
        self.assertEqual(match.invoke(), b"x" * 16)
        self.assertEqual(self.interface.calls, [(wire, 16)])

    def test_each_method_has_real_signature_and_annotation(self):
        for command in self.config.commands:
            method = getattr(self.proxy, command.name)
            self.assertEqual(method.__ecu_command__.template, command.template)
            signature = inspect.signature(method)
            self.assertIn("response_length", signature.parameters)
            self.assertNotIn("kwargs", signature.parameters)
        source = generate_proxy_source(self.config, "TestECUProxy")
        namespace = {}
        exec(compile(source, "generated_test.py", "exec"), namespace)
        loaded = namespace["TestECUProxy"](self.interface)
        self.assertEqual(CommandDispatcher(loaded).dispatch(b"F"), b"002")

    def test_write_count_is_not_response_length(self):
        wire = bytes.fromhex("77 02 04 00 05 00 04 ff 0a 00 46")
        self.assertEqual(self.dispatcher.dispatch(wire), b"")
        self.assertEqual(self.interface.calls, [(wire, 0)])

    def test_less_specific_template_cannot_reject_a_valid_write(self):
        wire = bytes.fromhex("77 02 04 00 05 00 ff") + b"\xff" * 255
        self.assertEqual(self.dispatcher.dispatch(wire), b"")
        self.assertEqual(self.interface.calls, [(wire, 0)])

    def test_table_write_identifier_and_count(self):
        wire = bytes.fromhex("77 01 00 03 00 20 00 03 01 02 03")
        match = self.dispatcher.match(wire, complete=True)
        self.assertEqual(match.method.__name__, "table_write_command")
        self.assertEqual(match.arguments["identifier"], 3)
        self.assertEqual(match.arguments["value"], b"\x01\x02\x03")
        match.invoke()
        self.assertEqual(self.interface.calls[-1], (wire, 0))

    def test_fixed_length_logger_crc_burn_and_query(self):
        for wire, size in [(b"r\x00\xf4\x00\x00\x04\x00", 1024),
                           (b"k\x03\x04\x00\x00\x00\x04", 4),
                           (b"b\x02\x05", 0),
                           (b"r\x00\x0f\x00\x00\x00\x0a", 10),
                           (b"A", 16), (b"Z,;", None)]:
            with self.subTest(wire=wire):
                self.dispatcher.dispatch(wire)
                self.assertEqual(self.interface.calls[-1], (wire, size))

    def test_custom_commands_and_chain_components(self):
        wire = bytes.fromhex("77 03 07 02 08 00 03 30 39 01")
        self.dispatcher.dispatch(wire)
        self.assertEqual(self.interface.calls, [(wire, 0)])
        self.assertTrue(hasattr(self.proxy, "cmd_chain_1"))
        self.assertTrue(hasattr(self.proxy, "cmd_chain_2"))

    def test_conditional_flags_and_ini_signature(self):
        self.assertEqual(self.config.signature, b"Test ECU")
        self.assertEqual(self.config.autodetect_response_length, 10)
        other = IniProtocolConfig(FIXTURE, defines={"CAN_COMMANDS": False})
        self.assertEqual(other.command_for_autodetect(), b"Q")
        self.assertEqual(other.autodetect_response_length, 8)
        self.assertEqual(IniProtocolConfig(FIXTURE, ts_can_id=3).command_for_autodetect()[1], 3)

    def test_page_identifiers_constrain_identical_templates(self):
        config = self.config_from_text(r'''
[MegaTune]
queryCommand = "Q"
signature = "ECU"
[Constants]
endianness = little
pageSize = 2, 3
pageIdentifier = "\x00\x01", "\x01\x01"
pageReadCommand = "R%2i", "R%2i"
''')
        proxy = generate_proxy_class(config)(self.interface)
        dispatcher = CommandDispatcher(proxy)
        match = dispatcher.match(b"R\x01\x01", complete=True)
        self.assertEqual(match.method.__name__, "page_read_command_2")
        self.assertEqual(match.arguments, {"identifier": 257, "response_length": 3})
        self.assertEqual(match.invoke(), b"xxx")
        with self.assertRaises(ValueError):
            proxy.page_read_command_1(identifier=257, response_length=2)

    def test_per_page_definitions_and_explicit_length_override(self):
        config = self.config_from_text('''
[MegaTune]
queryCommand = "Q"
signature = "ECU"
versionInfo = "V"
versionInfoResponseLength = 17
[Constants]
page = 1
pageSize = 2
pageIdentifier = "a"
pageReadCommand = "R%i"
page = 2
pageSize = 3
pageIdentifier = "b"
pageReadCommand = "R%i"
''')
        dispatcher = CommandDispatcher(generate_proxy_class(config)(self.interface))
        dispatcher.dispatch(b"Rb")
        self.assertEqual(self.interface.calls[-1], (b"Rb", 3))
        dispatcher.dispatch(b"V")
        self.assertEqual(self.interface.calls[-1], (b"V", 17))

    def test_invalid_conditionals_and_cycles_fail_clearly(self):
        for text in ('#if FLAG\n', '#else\n', '#include "missing.ini"\n',
                     '[ControllerCommands]\na = b\nb = a\n'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.config_from_text(text)


if __name__ == "__main__":
    unittest.main()
