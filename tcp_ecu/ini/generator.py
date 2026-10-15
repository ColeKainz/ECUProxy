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


def generate_proxy_source(config, class_name=None):
    class_name = class_name or proxy_class_name(config)
    if not class_name.isidentifier() or keyword.iskeyword(class_name):
        raise ValueError(f"Invalid proxy class name: {class_name!r}")
    lines = [
        '"""Generated ECU command methods; subclass to customize their behavior."""',
        "", "from typing import Annotated", "",
        "from tcp_ecu.protocol import IniParameter, ecu_command",
        "from tcp_ecu.interfaces.base import ECUInterface",
        "from tcp_ecu.proxy import ECUProxy", "", "",
        f"class {class_name}(ECUProxy):",
        f"    byteorder = {config.byteorder!r}",
        f"    signature = {config.signature!r}", "",
        "    def __init__(self, interface: ECUInterface):",
        "        super().__init__(interface)",
    ]
    field_names = {"o": "offset", "c": "count", "i": "identifier", "v": "value", "p": "page"}
    reserved = {"interface", "_exchange", "protocol_version", "byteorder", "signature"}
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
        lines.extend(["        *,", "        response_length: int | None,", "    ) -> bytes:",
                      "        return self._exchange(", f"            {command.name!r}, response_length,"])
        if bindings:
            mapping = ", ".join(f"{token!r}: {name}" for token, name in bindings)
            lines.append(f"            **{{{mapping}}},")
        lines.append("        )")
    return "\n".join(lines) + "\n"


def generate_proxy_class(config, class_name=None):
    class_name = class_name or proxy_class_name(config)
    namespace = {"__name__": "generated_ecu_proxy"}
    source = generate_proxy_source(config, class_name)
    # Only validated identifiers and repr-escaped literals from the INI enter source.
    exec(compile(source, str(config.path) + "::<proxy>", "exec"), namespace)
    return namespace[class_name]


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
    parser.add_argument("--output", type=Path, help="Write a Python module (otherwise print to stdout)")
    args = parser.parse_args()
    try:
        source = generate_proxy_source(config_from_args(args), args.class_name)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    if args.output:
        args.output.write_text(source, encoding="utf-8")
    else:
        print(source, end="")


if __name__ == "__main__":
    main()
