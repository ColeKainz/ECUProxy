import dataclasses
import inspect
import math
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tcp_ecu.ini.config import IniProtocolConfig, split_values
from tcp_ecu.ini.generator import generate_proxy_class, generate_proxy_source
from tcp_ecu.interfaces.base import ECUResponse
from tcp_ecu.protocol import CommandDispatcher


FIXTURE = Path(__file__).parent / "fixtures" / "pages.ini"


class PageResponseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = IniProtocolConfig(FIXTURE)
        cls.proxy_type = generate_proxy_class(cls.config, "PagesProxy")
        cls.Page1 = cls.proxy_type.page_response_types[1]
        cls.Page2 = cls.proxy_type.page_response_types[2]

    def data(self):
        data = bytearray(range(32))
        data[0] = 0xD3
        data[2:4] = struct.pack(">h", -123)
        data[4:6] = struct.pack(">h", 20)
        data[12:16] = struct.pack(">f", 1.25)
        data[16:20] = b"\x04\x03\x00\x05"
        return bytes(data)

    def test_all_properties_are_optional_with_default_none(self):
        for response_type in self.proxy_type.page_response_types.values():
            instance = response_type()
            self.assertTrue(dataclasses.is_dataclass(instance))
            for field in dataclasses.fields(instance):
                self.assertIsNone(field.default)
                self.assertIsNone(getattr(instance, field.name))
                self.assertIn(type(None), field.type.__args__)

    def test_full_page_decodes_types_scaling_and_shapes(self):
        page = self.Page1.from_bytes(self.data())
        self.assertEqual(page.low, 3)
        self.assertEqual(page.high, 5)
        self.assertEqual(page.signed, -123)
        self.assertEqual(page.scaled, 4.0)
        self.assertEqual(page.table, [[3.0, 3.5, 4.0], [4.5, 5.0, 5.5]])
        self.assertEqual(page.floating, 1.25)
        self.assertEqual(page.ratio, 8.0)
        self.assertIsNone(page.context_value)
        self.assertEqual(page.last, 28)
        self.assertEqual(page.last_alias, 12)
        self.assertEqual(page.to_bytes(), self.data())

    def test_every_partial_segment_round_trips_exactly(self):
        data = self.data()
        for start in range(33):
            for end in range(start, 33):
                with self.subTest(start=start, end=end):
                    page = self.Page1.from_bytes(data[start:end], offset=start)
                    self.assertEqual(page.to_bytes(), data[start:end])

    def test_partial_scalars_and_arrays_are_none(self):
        page = self.Page1.from_bytes(self.data()[3:7], offset=3)
        self.assertIsNone(page.signed)
        self.assertEqual(page.scaled, 4.0)
        self.assertIsNone(page.table)
        self.assertIsNone(page.byte)
        array = self.Page1.from_bytes(self.data()[6:12], offset=6)
        self.assertEqual(array.table, [[3.0, 3.5, 4.0], [4.5, 5.0, 5.5]])
        self.assertIsNone(array.signed)

    def test_missing_scale_dependency_leaves_field_none(self):
        page = self.Page1.from_bytes(self.data()[16:17], offset=16)
        self.assertIsNone(page.ratio)
        self.assertEqual(page.to_bytes(), b"\x04")
        with_context = self.Page1.from_bytes(b"\x04", offset=16, context={"gain": 3})
        self.assertEqual(with_context.ratio, 8)

    def test_same_page_dependency_overrides_stale_external_context(self):
        page = self.Page1.from_bytes(self.data(), context={"gain": 99})
        self.assertEqual(page.ratio, 8)

    def test_external_context_supports_decoding_and_editing(self):
        page = self.Page1.from_bytes(self.data()[18:20], offset=18, context={"external": 3})
        self.assertEqual(page.context_value, 20)
        page.context_value = 28
        self.assertEqual(page.to_bytes(), b"\x00\x07")

    def test_scalar_edits_preserve_unknown_bytes_and_segment_length(self):
        page = self.Page1.from_bytes(self.data()[2:6], offset=2)
        page.signed = -100
        page.scaled = 2.0
        self.assertEqual(page.to_bytes(), struct.pack(">hh", -100, 12))
        full = self.Page1.from_bytes(self.data())
        full.byte = 99
        expected = bytearray(self.data())
        expected[1] = 99
        self.assertEqual(full.to_bytes(), bytes(expected))

    def test_bit_edits_preserve_neighboring_bits_and_unmodified_aliases(self):
        page = self.Page1.from_bytes(b"\xd3")
        page.low = 1
        page.high = 2
        self.assertEqual(page.to_bytes(), b"\xa1")
        self.assertEqual(page.low_alias, 3)

    def test_conflicting_alias_edits_fail_and_consistent_edits_work(self):
        page = self.Page1.from_bytes(b"\xd3")
        page.low = 1
        page.low_alias = 2
        with self.assertRaisesRegex(ValueError, "Conflicting edits"):
            page.to_bytes()
        page.low_alias = 1
        self.assertEqual(page.to_bytes(), b"\xd1")

    def test_in_place_array_edits_patch_only_changed_elements(self):
        page = self.Page1.from_bytes(self.data()[6:12], offset=6)
        page.table[0][0] = 10
        page.table_alias[5] = 99
        self.assertEqual(page.to_bytes(), bytes([20, 7, 8, 9, 10, 99]))
        page.table_alias[0] = 9
        with self.assertRaisesRegex(ValueError, "Conflicting edits"):
            page.to_bytes()

    def test_nan_signed_zero_and_unknown_bytes_preserved(self):
        data = bytearray(24)
        data[6:8] = b"\xab\xcd"
        data[12:16] = bytes.fromhex("7fa12345")
        data[16:20] = bytes.fromhex("80000000")
        page = self.Page2.from_bytes(bytes(data))
        self.assertTrue(math.isnan(page.float_array[0]))
        self.assertEqual(math.copysign(1, page.float_array[1]), -1)
        page.number = 42
        data[8:12] = struct.pack(">I", 42)
        self.assertEqual(page.to_bytes(), bytes(data))

    def test_large_integer_does_not_lose_precision(self):
        value = 2**64 - 1
        page = self.Page1.from_bytes(value.to_bytes(8, "big"), offset=20)
        self.assertEqual(page.wide, value)
        page.wide = value - 1
        self.assertEqual(page.to_bytes(), (value - 1).to_bytes(8, "big"))

    def test_none_discards_an_edit_without_erasing_data(self):
        page = self.Page1.from_bytes(b"\xd3\x01")
        page.byte = 5
        page.byte = None
        self.assertEqual(page.to_bytes(), b"\xd3\x01")

    def test_fields_outside_segment_cannot_be_serialized(self):
        page = self.Page1.from_bytes(self.data()[3:4], offset=3)
        page.signed = 12
        with self.assertRaisesRegex(ValueError, "outside the received"):
            page.to_bytes()

    def test_missing_conversion_context_cannot_be_guessed(self):
        page = self.Page1.from_bytes(b"\x00\x05", offset=18)
        page.context_value = 12
        with self.assertRaisesRegex(ValueError, "Missing conversion context"):
            page.to_bytes()

    def test_invalid_values_shapes_and_ranges_are_rejected(self):
        for name, value in (("byte", 256), ("low", 8), ("signed", -32769),
                            ("table", [[1, 2], [3, 4]]), ("table", [[1, 2, None], [3, 4, 5]])):
            with self.subTest(name=name, value=value):
                page = self.Page1.from_bytes(self.data())
                setattr(page, name, value)
                with self.assertRaises(ValueError):
                    page.to_bytes()
        with self.assertRaises(ValueError):
            self.Page1.from_bytes(b"xx", offset=31)
        with self.assertRaises(ValueError):
            self.Page1.from_bytes(b"", offset=-1)
        with self.assertRaises(ValueError):
            self.Page1().to_bytes()

    def test_ecu_status_survives_and_diagnostics_are_not_page_fields(self):
        page = self.Page1.from_bytes(ECUResponse(self.data(), status=2))
        page.byte = 20
        response = page.to_bytes()
        self.assertIsInstance(response, ECUResponse)
        self.assertEqual(response.status, 2)
        diagnostic = ECUResponse(b"Configuration error" * 3, status=3)
        page = self.Page1.from_bytes(diagnostic)
        self.assertTrue(all(getattr(page, field.name) is None for field in dataclasses.fields(page)))
        self.assertEqual(page.to_bytes(), diagnostic)
        self.assertEqual(page.to_bytes().status, 3)

    def test_no_fields_still_preserves_partial_data(self):
        page = self.proxy_type.page_response_types[3].from_bytes(b"\xff\x12", offset=3)
        self.assertEqual(page.to_bytes(), b"\xff\x12")

    def test_generated_page_read_uses_codecs_and_can_modify_response(self):
        interface = Mock()
        interface.exchange.return_value = ECUResponse(b"\x01", status=2)
        original = self.Page1.from_bytes

        def customize(data, **kwargs):
            page = original(data, **kwargs)
            page.byte = 9
            return page

        with patch.object(self.Page1, "from_bytes", side_effect=customize) as decode:
            response = CommandDispatcher(self.proxy_type(interface)).dispatch(b"R\x01\x00\x01\x00\x01")
        decode.assert_called_once_with(interface.exchange.return_value, offset=1)
        self.assertEqual(response, b"\x09")
        self.assertEqual(response.status, 2)
        self.assertEqual(inspect.signature(self.proxy_type.page_read_command_1).return_annotation, bytes)


class PageDefinitionTests(unittest.TestCase):
    def config(self, text):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "test.ini"
        path.write_text('[MegaTune]\nqueryCommand="Q"\nsignature="ECU"\n' + text)
        return IniProtocolConfig(path)

    def test_little_endian_and_per_page_command_generation(self):
        config = self.config('''
[Constants]
endianness = little
page = 2
pageSize = 8
pageReadCommand = "R%2o%2c"
value = scalar, S16, 2, "", 0.5, -2, -100, 100, 1
''')
        proxy = generate_proxy_class(config)
        response_type = proxy.page_response_types[2]
        page = response_type.from_bytes(struct.pack("<h", -10), offset=2)
        self.assertEqual(page.value, -6)
        page.value = 4
        self.assertEqual(page.to_bytes(), struct.pack("<h", 10))
        self.assertIn("page = Page2Response.from_bytes", generate_proxy_source(config))

    def test_fixed_read_offset_is_preserved(self):
        config = self.config(r'''
[Constants]
pageSize = 10
pageReadCommand = "r\x00\x04\x00\x02\x00\x02"
page = 1
value = scalar, U16, 2, "", 1, 0, 0, 100, 0
''')
        proxy = generate_proxy_class(config)
        response_type = proxy.page_response_types[1]
        interface = Mock(exchange=Mock(return_value=b"\x00\x05"))
        with patch.object(response_type, "from_bytes", wraps=response_type.from_bytes) as decode:
            proxy(interface).page_read_command(response_length=2)
        decode.assert_called_once_with(b"\x00\x05", offset=2)

    def test_parameter_start_offset_is_subtracted_from_request_offset(self):
        config = self.config('''
[Constants]
parameterStartOffset = 256
pageSize = 4
pageReadCommand = "R%2o%2c"
page = 1
value = scalar, U16, 0, "", 1, 0, 0, 100, 0
''')
        proxy = generate_proxy_class(config)
        interface = Mock(exchange=Mock(return_value=b"\x00\x05"))
        with patch.object(proxy.page_response_types[1], "from_bytes", wraps=proxy.page_response_types[1].from_bytes) as decode:
            proxy(interface).page_read_command(offset=256, count=2, response_length=2)
        decode.assert_called_once_with(b"\x00\x05", offset=0)

    def test_conditional_fields_and_python_keyword_names(self):
        config = self.config('''
[Constants]
pageSize = 4
page = 1
#if CELSIUS
temperature = scalar, S16, 0, "", 0.5, -10, 0, 100, 0
#else
temperature = scalar, S16, 0, "", 1, 0, 0, 100, 0
#endif
class = scalar, U08, 2, "", 1, 0, 0, 255, 0
''')
        page = generate_proxy_class(config).page_response_types[1].from_bytes(b"\x00\x04\x02")
        self.assertEqual(page.temperature, 4)
        self.assertEqual(page.class_, 2)

    def test_top_level_splitting_preserves_nested_expressions_and_strings(self):
        self.assertEqual(split_values('array, U08, 0, [2x3], {label(1, "a,b")}, 0.5, 0'),
                         ['array', 'U08', '0', '[2x3]', '{label(1, "a,b")}', '0.5', '0'])

    def test_invalid_field_definitions_fail_with_line_and_name(self):
        for definition in ('scalar, U24, 0, "", 1, 0', 'bits, U08, 0, [0:8]',
                           'array, U16, 0, [{size}], "", 1, 0',
                           'scalar, U08, 0, "", {danger()}, 0',
                           'scalar, U08, 0, "", 0, 0', 'scalar, U16, 3, "", 1, 0'):
            with self.subTest(definition=definition), self.assertRaisesRegex(ValueError, "INI line|pageSize"):
                self.config('[Constants]\npageSize=4\npage=1\nvalue = ' + definition)

    def test_generated_source_is_importable_and_contains_explicit_codecs(self):
        source = generate_proxy_source(IniProtocolConfig(FIXTURE), "DemoProxy")
        namespace = {}
        exec(compile(source, "generated.py", "exec"), namespace)
        self.assertIn("Page1Response", namespace)
        self.assertIn("from_bytes", namespace["Page1Response"].__dict__)
        self.assertIn("to_bytes", namespace["Page1Response"].__dict__)
        self.assertEqual(namespace["Page1Response"].from_bytes(b"\xd3").to_bytes(), b"\xd3")


if __name__ == "__main__":
    unittest.main()
