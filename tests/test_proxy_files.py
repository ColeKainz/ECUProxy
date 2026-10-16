import ast
import dataclasses
import importlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tcp_ecu.ini.config import IniProtocolConfig
from tcp_ecu.ini.generator import write_proxy_files
from tcp_ecu.interfaces.base import ECUResponse
from tcp_ecu.protocol import CommandDispatcher


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "pages.ini"


class ProxyFileTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.config = IniProtocolConfig(FIXTURE)
        self.output = self.directory / "vehicle_proxy.py"

    def load_proxy(self, name="vehicle_proxy"):
        with patch.dict(sys.modules), patch.object(sys, "path", [str(self.directory), *sys.path]):
            importlib.invalidate_caches()
            return importlib.import_module(name)

    def test_writes_one_page_class_per_file_and_proxy_imports(self):
        write_proxy_files(self.config, self.output, "DemoProxy")
        package = self.directory / "vehicle_proxy_pages"
        self.assertEqual({path.name for path in package.iterdir()},
                         {"__init__.py", "page_1_response.py", "page_2_response.py", "page_3_response.py"})
        source = self.output.read_text()
        classes = [node.name for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ClassDef)]
        self.assertEqual(classes, ["DemoProxy"])
        self.assertIn("from vehicle_proxy_pages import (", source)
        self.assertNotIn("PageField(", source)
        for page in self.config.pages:
            tree = ast.parse((package / f"page_{page}_response.py").read_text())
            classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
            self.assertEqual([node.name for node in classes], [f"Page{page}Response"])
            self.assertEqual({node.name for node in classes[0].body if isinstance(node, ast.FunctionDef)},
                             {"from_bytes", "to_bytes"})

    def test_imported_models_preserve_defaults_partial_reads_and_editing(self):
        write_proxy_files(self.config, self.output, "DemoProxy")
        module = self.load_proxy()
        response_type = module.Page1Response
        self.assertEqual(response_type.__module__, "vehicle_proxy_pages.page_1_response")
        self.assertIs(module.DemoProxy.page_response_types[1], response_type)
        self.assertTrue(all(field.default is None for field in dataclasses.fields(response_type)))
        page = response_type.from_bytes(b"\x01", offset=1)
        self.assertEqual(page.byte, 1)
        self.assertIsNone(page.signed)
        self.assertIsNone(page.table)
        self.assertEqual(page.to_bytes(), b"\x01")
        page.byte = 9
        self.assertEqual(page.to_bytes(), b"\x09")

        interface = Mock(exchange=Mock(return_value=ECUResponse(b"\x01", status=2)))
        with patch.object(response_type, "from_bytes", wraps=response_type.from_bytes) as decode:
            response = CommandDispatcher(module.DemoProxy(interface)).dispatch(b"R\x01\x00\x01\x00\x01")
        decode.assert_called_once_with(interface.exchange.return_value, offset=1)
        self.assertEqual(response, b"\x01")
        self.assertEqual(response.status, 2)

    def test_proxy_can_be_imported_inside_a_python_package(self):
        package = self.directory / "generated_vehicles"
        package.mkdir()
        (package / "__init__.py").write_text("")
        write_proxy_files(self.config, package / self.output.name, "DemoProxy")
        module = self.load_proxy("generated_vehicles.vehicle_proxy")
        self.assertEqual(module.Page1Response.__module__,
                         "generated_vehicles.vehicle_proxy_pages.page_1_response")
        self.assertEqual(module.Page1Response.from_bytes(b"\xd3").to_bytes(), b"\xd3")

    def test_multiple_proxies_get_distinct_page_packages(self):
        write_proxy_files(self.config, self.output, "DemoProxy")
        write_proxy_files(self.config, self.directory / "second_proxy.py", "DemoProxy")
        with patch.dict(sys.modules), patch.object(sys, "path", [str(self.directory), *sys.path]):
            importlib.invalidate_caches()
            first = importlib.import_module("vehicle_proxy")
            second = importlib.import_module("second_proxy")
        self.assertIsNot(first.Page1Response, second.Page1Response)
        self.assertEqual(second.Page1Response.__module__, "second_proxy_pages.page_1_response")

    def test_cli_output_writes_importable_package(self):
        result = subprocess.run(
            [sys.executable, "-B", str(ROOT / "generate_proxy.py"), str(FIXTURE),
             "--class-name", "DemoProxy", "--output", str(self.output)],
            cwd=ROOT, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(len(self.load_proxy().DemoProxy.page_response_types), 3)

    def test_cli_stdout_remains_standalone(self):
        result = subprocess.run(
            [sys.executable, "-B", str(ROOT / "generate_proxy.py"), str(FIXTURE), "--class-name", "DemoProxy"],
            cwd=ROOT, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        namespace = {}
        exec(compile(result.stdout, "generated.py", "exec"), namespace)
        self.assertEqual(len(namespace["DemoProxy"].page_response_types), 3)

    def test_invalid_output_package_name_does_not_write_files(self):
        with self.assertRaisesRegex(ValueError, "package name"):
            write_proxy_files(self.config, self.directory / "invalid-name.py", "DemoProxy")
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_no_pages_does_not_create_a_page_package(self):
        ini = self.directory / "no_pages.ini"
        ini.write_text('[MegaTune]\nqueryCommand = "Q"\nsignature = "ECU"\n')
        write_proxy_files(IniProtocolConfig(ini), self.output, "DemoProxy")
        self.assertFalse((self.directory / "vehicle_proxy_pages").exists())
        self.assertEqual(self.load_proxy().DemoProxy.page_response_types, {})

    def test_regeneration_preserves_unrelated_files(self):
        write_proxy_files(self.config, self.output, "DemoProxy")
        notes = self.directory / "vehicle_proxy_pages" / "notes.txt"
        notes.write_text("Keep developer notes")
        original = self.output.read_text()
        write_proxy_files(self.config, self.output, "DemoProxy")
        self.assertEqual(self.output.read_text(), original)
        self.assertEqual(notes.read_text(), "Keep developer notes")


if __name__ == "__main__":
    unittest.main()
