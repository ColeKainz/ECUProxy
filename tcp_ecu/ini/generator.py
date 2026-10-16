"""Generate an importable ECUProxy subclass or load one directly from an INI."""

import argparse
import keyword
import re
from pathlib import Path

from tcp_ecu.protocol import CommandPattern
from tcp_ecu.ini.config import IniProtocolConfig, snake_case


def proxy_class_name(config):
    words = re.findall(r"[A-Za-z0-9]+", config.path.stem)
    name = "".join(word[:1].upper() + word[1:] for word in words) or "ECU"
    if name[0].isdigit():
        name = "ECU" + name
    return name + "Proxy"


def _page_response_lines(page, byteorder):
    response_class = f"Page{page.number}Response"
    lines = ["@dataclass", f"class {response_class}(PageResponse):",
             f"    _page_size = {page.size!r}", f"    _byteorder = {byteorder!r}",
             "    _page_fields = ("]
    for field in page.fields:
        lines.append(f"        {field!r},")
    lines.append("    )")
    for field in page.fields:
        lines.append(f"    {field.name}: {field.annotation} = None")
    lines.extend([
        "", "    @classmethod",
        "    def from_bytes(", "        cls, data: bytes, *, offset: int = 0,",
        "        context: dict[str, int | float] | None = None,",
        f"    ) -> {response_class!r}:",
        "        return super().from_bytes(data, offset=offset, context=context)",
        "", "    def to_bytes(self) -> bytes:", "        return super().to_bytes()",
    ])
    return lines


def generate_proxy_source(config, class_name=None, *, pages_package=None):
    """Render a proxy, importing page models when a sibling package is supplied."""
    class_name = class_name or proxy_class_name(config)
    if not class_name.isidentifier() or keyword.iskeyword(class_name):
        raise ValueError(f"Invalid proxy class name: {class_name!r}")
    if class_name in {f"Page{page}Response" for page in config.pages}:
        raise ValueError("Proxy class name conflicts with a generated page response")
    if pages_package is not None and (not pages_package.isidentifier() or keyword.iskeyword(pages_package)):
        raise ValueError(f"Invalid page response package name: {pages_package!r}")
    lines = [
        '"""Generated ECU command methods; subclass to customize their behavior."""',
        "", "from typing import Annotated", "",
        "from tcp_ecu.protocol import IniParameter, ecu_command",
        "from tcp_ecu.interfaces.base import ECUInterface",
        "from tcp_ecu.proxy import ECUProxy",
    ]
    if pages_package is None:
        lines.extend(["from dataclasses import dataclass", "from tcp_ecu.pages import PageField, PageResponse"])
        for page in config.pages.values():
            lines.extend(["", "", *_page_response_lines(page, config.byteorder)])
    elif config.pages:
        # Relative imports also allow the output module to live inside a package.
        lines.append("")
        for branch, prefix in [("if __package__:", "."), ("else:", "")]:
            lines.extend([branch, f"    from {prefix}{pages_package} import ("])
            lines.extend(f"        Page{page}Response," for page in config.pages)
            lines.append("    )")
    lines.extend([
        "", "",
        f"class {class_name}(ECUProxy):",
        f"    byteorder = {config.byteorder!r}",
        f"    signature = {config.signature!r}",
        "    page_response_types = {",
    ])
    for page in config.pages:
        lines.append(f"        {page}: Page{page}Response,")
    lines.extend([
        "    }", "",
        "    def __init__(self, interface: ECUInterface):",
        "        super().__init__(interface)",
    ])
    field_names = {"o": "offset", "c": "count", "i": "identifier", "v": "value", "p": "page"}
    reserved = {"interface", "_exchange", "protocol_version", "byteorder", "signature", "page_response_types"}
    for command in config.commands:
        if command.name in reserved or keyword.iskeyword(command.name):
            raise ValueError(f"Reserved proxy method name: {command.name}")
        pattern = CommandPattern(command.template, config.byteorder, page_identifier=command.page_identifier)
        options = f"response_length={command.response_length!r}"
        if command.page_identifier:
            options += f", page_identifier={command.page_identifier!r}"
        lines.extend(["", f"    @ecu_command({command.template!r}, {options})", f"    def {command.name}(", "        self,"])
        bindings = []
        names = {"self", "response_length"}
        for token, field in pattern.variables.items():
            name = field_names.get(token, snake_case(token))
            if keyword.iskeyword(name):
                name += "_"
            if name in names:
                raise ValueError(f"Conflicting parameter name {name!r} in {command.name}")
            names.add(name)
            annotation = "bytes" if field.is_bytes else "int"
            lines.append(f"        {name}: Annotated[{annotation}, IniParameter({token!r})],")
            bindings.append((token, name))
        exchange = "data =" if command.page is not None else "return"
        lines.extend(["        *,", "        response_length: int | None,", "    ) -> bytes:",
                      f"        {exchange} self._exchange(", f"            {command.name!r}, response_length,"])
        if bindings:
            mapping = ", ".join(f"{token!r}: {name}" for token, name in bindings)
            lines.append(f"            **{{{mapping}}},")
        lines.append("        )")
        if command.page is not None:
            offset = "offset" if "o" in pattern.variables else str(command.read_offset)
            if "o" in pattern.variables and config.parameter_start_offset:
                offset += f" - {config.parameter_start_offset}"
            lines.extend([
                f"        page = Page{command.page}Response.from_bytes(data, offset={offset})",
                "        # Customize populated page properties here before serialization.",
                "        return page.to_bytes()",
            ])
    return "\n".join(lines) + "\n"


def generate_proxy_class(config, class_name=None):
    class_name = class_name or proxy_class_name(config)
    namespace = {"__name__": "generated_ecu_proxy"}
    source = generate_proxy_source(config, class_name)
    # Only validated identifiers and repr-escaped literals from the INI enter source.
    exec(compile(source, str(config.path) + "::<proxy>", "exec"), namespace)
    return namespace[class_name]


def write_proxy_files(config, output, class_name=None):
    """Write the proxy and a sibling package containing one module per page."""
    output = Path(output)
    pages_package = output.stem + "_pages"
    source = generate_proxy_source(config, class_name, pages_package=pages_package)
    page_sources = {}
    imports = ['"""Generated ECU page response classes."""', ""]
    for page in config.pages.values():
        module = f"page_{page.number}_response"
        lines = [f'"""Generated response for ECU page {page.number}."""', "",
                 "from dataclasses import dataclass", "",
                 "from tcp_ecu.pages import PageField, PageResponse", "", "",
                 *_page_response_lines(page, config.byteorder)]
        page_sources[module + ".py"] = "\n".join(lines) + "\n"
        imports.append(f"from .{module} import Page{page.number}Response")
    if page_sources:
        page_sources["__init__.py"] = "\n".join(imports) + "\n"
        directory = output.parent / pages_package
        directory.mkdir(exist_ok=True)
        for filename, page_source in page_sources.items():
            (directory / filename).write_text(page_source, encoding="utf-8")
    output.write_text(source, encoding="utf-8")


def add_ini_arguments(parser):
    parser.add_argument("ini", help="Path to the TunerStudio ECU .ini file")
    parser.add_argument("--class-name", help="Name for the generated ECUProxy subclass")
    parser.add_argument("--define", action="append", default=[], metavar="NAME", help="Enable an INI conditional flag")
    parser.add_argument("--undefine", action="append", default=[], metavar="NAME", help="Disable an INI conditional flag")
    parser.add_argument("--can-id", type=int, default=0, help="CAN ID for ECU autodetection (default 0)")


def config_from_args(args):
    flags = {name: True for name in args.define}
    flags.update({name: False for name in args.undefine})
    return IniProtocolConfig(args.ini, defines=flags, ts_can_id=args.can_id)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_ini_arguments(parser)
    parser.add_argument("--output", type=Path,
                        help="Write a proxy module and sibling <output_stem>_pages package (otherwise print standalone source)")
    args = parser.parse_args()
    try:
        config = config_from_args(args)
        if args.output:
            write_proxy_files(config, args.output, args.class_name)
        else:
            print(generate_proxy_source(config, args.class_name), end="")
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
