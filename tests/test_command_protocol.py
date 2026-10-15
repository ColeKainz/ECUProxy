import unittest
from typing import Annotated

from tcp_ecu.protocol import (
    CommandDispatcher, CommandPattern, IncompleteCommand, IniParameter,
    UnknownCommand, Variable, ecu_command,
)


class CommandPatternTests(unittest.TestCase):
    def test_ini_example_has_literal_bytes_and_typed_delimiters(self):
        pattern = CommandPattern(r"r\$tsCanId\x04%2o%2c")
        self.assertEqual(pattern.parts, (b"r", Variable("tsCanId", 1), b"\x04",
                                         Variable("o", 2), Variable("c", 2)))
        data = bytes.fromhex("72 03 04 01 02 00 10")
        self.assertEqual(pattern.match(data), ({"tsCanId": 3, "o": 258, "c": 16}, 7))
        self.assertEqual(pattern.render({"tsCanId": 3, "o": 258, "c": 16}), data)

    def test_little_endian_and_embedded_literal(self):
        pattern = CommandPattern(r"R%2o\xff%4c", "little")
        self.assertEqual(pattern.match(bytes.fromhex("52 02 01 ff 04 03 02 01")),
                         ({"o": 258, "c": 0x01020304}, 8))

    def test_every_fragment_waits_and_wrong_literal_is_not_a_match(self):
        pattern = CommandPattern(r"r\$tsCanId\x04%2o%2c")
        wire = bytes.fromhex("72 00 04 00 08 00 10")
        for size in range(len(wire)):
            with self.subTest(size=size), self.assertRaises(IncompleteCommand):
                pattern.match(wire[:size])
        self.assertIsNone(pattern.match(b"r\x00\x05"))
        self.assertIsNone(pattern.match(wire + b"F", complete=True))

    def test_count_delimits_binary_write_data(self):
        pattern = CommandPattern(r"w%2c%v!")
        wire = b"w\x00\x04\n\x00!\xff!F"
        self.assertEqual(pattern.match(wire), ({"c": 4, "v": b"\n\x00!\xff"}, 8))
        with self.assertRaises(IncompleteCommand):
            pattern.match(wire[:6])
        with self.assertRaises(ValueError):
            pattern.render({"c": 2, "v": b"three"})

    def test_unbounded_data_requires_a_complete_frame(self):
        pattern = CommandPattern("W%v")
        with self.assertRaises(IncompleteCommand):
            pattern.match(b"Wabc")
        self.assertEqual(pattern.match(b"Wabc", complete=True), ({"v": b"abc"}, 4))
        self.assertEqual(CommandPattern("W%v", value_length=2).match(b"WabF"), ({"v": b"ab"}, 3))

    def test_escaped_literals_and_repeated_variables(self):
        pattern = CommandPattern(r"\x72\004%%\n%o%o")
        self.assertEqual(pattern.match(b"r\x04%\n\x03\x03"), ({"o": 3}, 6))
        self.assertIsNone(pattern.match(b"r\x04%\n\x03\x04"))

    def test_bad_templates_and_out_of_range_values(self):
        for template in ("", r"r\x0g", r"r\q", "r%0c", "r%", "r%2c%c"):
            with self.subTest(template=template), self.assertRaises(ValueError):
                CommandPattern(template)
        with self.assertRaises(ValueError):
            CommandPattern("r%c").render({"c": 256})


class DispatcherTests(unittest.TestCase):
    def test_explicit_parameter_annotations_and_response_length(self):
        class Proxy:
            @ecu_command(r"r\$tsCanId\x04%2o%2c", response_length="c")
            def read(self, can: Annotated[int, IniParameter("tsCanId")],
                     address: Annotated[int, IniParameter("o")],
                     size: Annotated[int, IniParameter("c")], response_length):
                self.received = can, address, size, response_length
                return b"data"

        proxy = Proxy()
        self.assertEqual(CommandDispatcher(proxy).dispatch(bytes.fromhex("72 02 04 01 00 00 04")), b"data")
        self.assertEqual(proxy.received, (2, 256, 4, 4))

    def test_response_length_can_itself_capture_count(self):
        class Proxy:
            @ecu_command("R%2c")
            def read(self, response_length: Annotated[int, IniParameter("c")]):
                return b"x" * response_length

        self.assertEqual(CommandDispatcher(Proxy()).dispatch(b"R\x00\x03"), b"xxx")

    def test_reject_missing_method_parameters_at_load(self):
        class MissingLength:
            @ecu_command("F")
            def query(self):
                return b"002"

        class MissingVariable:
            @ecu_command("R%2o")
            def read(self, response_length):
                return b""

        for proxy in (MissingLength(), MissingVariable()):
            with self.assertRaises(ValueError):
                CommandDispatcher(proxy)

    def test_specific_command_wins_over_generic_and_aliases_are_allowed(self):
        class Proxy:
            @ecu_command("R%c", response_length=0)
            def generic(self, c, response_length):
                return b"generic"

            @ecu_command(r"R\x02", response_length=0)
            def specific(self, response_length):
                return b"specific"

            @ecu_command(r"R\x02", response_length=0)
            def specific_alias(self, response_length):
                return b"specific"

        dispatcher = CommandDispatcher(Proxy())
        self.assertEqual(dispatcher.dispatch(b"R\x02"), b"specific")
        self.assertEqual(dispatcher.dispatch(b"R\x03"), b"generic")
        with self.assertRaises(UnknownCommand):
            dispatcher.dispatch(b"X")


if __name__ == "__main__":
    unittest.main()
