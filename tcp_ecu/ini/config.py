"""Read the command-bearing portion of a TunerStudio ECU definition."""

import ast
import keyword
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from tcp_ecu.protocol import CommandPattern
from tcp_ecu.pages import PageField, PageResponse
from tcp_ecu.utils import decode_ini_string, strip_inline_comment


@dataclass(frozen=True)
class IniEntry:
    section: str
    name: str
    value: str
    line: int
    page: int | None


@dataclass(frozen=True)
class IniCommand:
    name: str
    template: str
    response_length: int | str | None
    page_identifier: str | None = None
    page: int | None = None
    read_offset: int = 0


@dataclass(frozen=True)
class IniPage:
    number: int
    size: int | None
    fields: tuple[PageField, ...]


def split_values(value):
    """Split top-level INI attributes, retaining strings and expressions intact."""
    values = []
    start = 0
    quoted = False
    escaped = False
    stack = []
    closing = {"}": "{", "]": "[", ")": "("}
    for position, char in enumerate(value):
        if escaped:
            escaped = False
        elif quoted and char == "\\":
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif not quoted:
            if char in "{[(":
                stack.append(char)
            elif char in closing:
                if not stack or stack.pop() != closing[char]:
                    raise ValueError(f"Unbalanced INI attribute: {value!r}")
            elif char == "," and not stack:
                values.append(value[start:position].strip())
                start = position + 1
    if quoted or stack:
        raise ValueError(f"Unterminated INI attribute: {value!r}")
    if value[start:].strip():
        values.append(value[start:].strip())
    return values


def snake_case(name):
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    name = re.sub(r"[^A-Za-z0-9_]", "_", name).lower().strip("_")
    return name if name and not name[0].isdigit() else "command_" + name


def _condition(expression, flags):
    expression = expression.replace("&&", " and ").replace("||", " or ")
    expression = re.sub(r"!(?!=)", " not ", expression).strip()
    node = ast.parse(expression, mode="eval").body

    def evaluate(item):
        if isinstance(item, ast.Name):
            return flags.get(item.id, False)
        if isinstance(item, ast.Constant) and item.value in (0, 1):
            return bool(item.value)
        if isinstance(item, ast.UnaryOp) and isinstance(item.op, ast.Not):
            return not evaluate(item.operand)
        if isinstance(item, ast.BoolOp):
            values = [evaluate(value) for value in item.values]
            return all(values) if isinstance(item.op, ast.And) else any(values)
        raise ValueError(f"Unsupported INI condition: {expression!r}")

    return evaluate(node)


class IniProtocolConfig:
    COMMAND_SECTIONS = {"MegaTune", "TunerStudio", "Constants", "OutputChannels",
                        "BurstMode", "ReferenceTables", "LoggerDefinition"}
    EXTRA_COMMANDS = {"versionInfo", "pageActivate", "pageValueWrite", "pageChunkWrite",
                      "retrieveConfigError"}
    PAGE_COMMANDS = {"burnCommand", "pageReadCommand", "pageActivate", "pageValueWrite",
                     "pageChunkWrite", "crc32CheckCommand"}

    def __init__(self, path, *, defines=None, ts_can_id=0):
        self.path = Path(path)
        self.ts_can_id = ts_can_id
        self.defines = {"INI_VERSION_2": True, **(defines or {})}
        self.entries = self._read_entries()
        self.byteorder = self._value("Constants", "endianness", "big").strip().lower()
        if self.byteorder not in {"big", "little"}:
            raise ValueError(f"Unsupported endianness: {self.byteorder}")
        self.signature = decode_ini_string(self._value("MegaTune", "signature", '""'))
        self.och_block_size = self._integer("OutputChannels", "ochBlockSize")
        self.parameter_start_offset = self._integer("Constants", "parameterStartOffset", 0)
        self.commands = self._read_commands()
        self.pages = self._read_pages()
        if not self.commands:
            raise ValueError(f"No commands found in {self.path}")
        query = next((command for command in self.commands if command.name == "query_command"), None)
        if query is None:
            raise ValueError("INI must define [MegaTune] queryCommand")
        self.query_command = query
        self.autodetect_response_length = query.response_length
        if isinstance(self.autodetect_response_length, str):
            raise ValueError("Autodetect queryCommand cannot have a variable response length")

    def _read_entries(self):
        entries = []
        flags = dict(self.defines)
        stack = []
        active = True
        section = ""
        page = None
        data = self.path.read_bytes()
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            # Legacy INIs use single-byte text; preserve bytes in command literals.
            text = data.decode("latin1")
        for line_number, raw in enumerate(text.splitlines(), 1):
            line = strip_inline_comment(raw).strip()
            if not line:
                continue
            if line.startswith("#"):
                parts = line[1:].split(None, 1)
                directive = parts[0]
                argument = parts[1].strip() if len(parts) > 1 else ""
                if directive == "if":
                    condition = _condition(argument, flags)
                    stack.append([active, condition, False])
                    active = active and condition
                elif directive == "elif":
                    if not stack or stack[-1][2]:
                        raise ValueError(f"Unexpected #elif on line {line_number}")
                    parent, taken, _ = stack[-1]
                    condition = _condition(argument, flags)
                    active = parent and not taken and condition
                    stack[-1][1] = taken or condition
                elif directive == "else":
                    if not stack or stack[-1][2]:
                        raise ValueError(f"Unexpected #else on line {line_number}")
                    parent, taken, _ = stack[-1]
                    active = parent and not taken
                    stack[-1][1:] = [True, True]
                elif directive == "endif":
                    if not stack:
                        raise ValueError(f"Unexpected #endif on line {line_number}")
                    active = stack.pop()[0]
                elif active and directive in {"set", "unset"}:
                    if argument not in self.defines:
                        flags[argument] = directive == "set"
                elif active and directive == "error":
                    raise ValueError(f"INI line {line_number}: {argument}")
                elif active and directive not in {"set", "unset"}:
                    raise ValueError(f"Unsupported INI directive #{directive} on line {line_number}")
                continue
            if not active:
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1]
                page = None
                continue
            if "=" not in line:
                continue
            name, value = (part.strip() for part in line.split("=", 1))
            if section == "Constants" and name == "page":
                page = int(value)
            entries.append(IniEntry(section, name, value, line_number, page))
        if stack:
            raise ValueError("Unterminated #if in INI")
        return entries

    def _value(self, section, name, default=None):
        return next((entry.value for entry in reversed(self.entries)
                     if entry.section == section and entry.name == name), default)

    def _integer(self, section, name, default=None):
        value = self._value(section, name)
        return int(value, 0) if value is not None else default

    def _page_value(self, name, page):
        candidates = [entry for entry in self.entries if entry.section == "Constants" and entry.name == name]
        local = next((entry for entry in reversed(candidates) if entry.page == page), None)
        if local:
            return split_values(local.value)[0]
        global_entry = next((entry for entry in reversed(candidates) if entry.page is None), None)
        if global_entry:
            values = split_values(global_entry.value)
            if 0 < page <= len(values):
                return values[page - 1]
        return None

    def _read_commands(self):
        pending = []
        controllers = {entry.name: entry for entry in self.entries if entry.section == "ControllerCommands"}

        def expand(entry, seen=()):
            if entry.name in seen:
                raise ValueError(f"Circular ControllerCommands reference: {entry.name}")
            for value in split_values(entry.value):
                if value.startswith('"') and value.endswith('"'):
                    yield value[1:-1]
                elif entry.section == "ControllerCommands" and value in controllers:
                    yield from expand(controllers[value], (*seen, entry.name))
                else:
                    raise ValueError(f"Expected command string on INI line {entry.line}: {value!r}")

        for entry in self.entries:
            if entry.section != "ControllerCommands" and not (
                entry.section in self.COMMAND_SECTIONS and
                (entry.name.endswith("Command") or entry.name in self.EXTRA_COMMANDS)
            ):
                continue
            for index, template in enumerate(expand(entry), 1):
                if not template:
                    continue
                if entry.section == "LoggerDefinition":
                    template = template.replace("\\\\", "\\")
                page = (entry.page or index) if entry.name in self.PAGE_COMMANDS else None
                identifier = None
                if page and re.search(r"%\d*i", template):
                    identifier = self._page_value("pageIdentifier", page)
                    if identifier:
                        identifier = identifier[1:-1]
                pattern = CommandPattern(template, self.byteorder, page_identifier=identifier)
                length = self._response_length(entry, pattern, page)
                pending.append((entry, page, template, identifier, length))

        counts = Counter(snake_case(entry.name) for entry, *_ in pending)
        occurrences = Counter()
        commands = []
        used_names = set()
        for entry, page, template, identifier, length in pending:
            name = snake_case(entry.name)
            occurrences[name] += 1
            if counts[name] > 1:
                name += f"_{occurrences[name]}"
            if name in used_names:
                raise ValueError(f"Conflicting generated method name: {name}")
            used_names.add(name)
            read_page = page if entry.name == "pageReadCommand" else None
            read_offset = 0
            if read_page and "o" not in CommandPattern(template).variables:
                pattern = CommandPattern(template, self.byteorder)
                probe = pattern.render({token: 0 for token in pattern.variables})
                if len(probe) == 7 and probe[:1] == b"r":
                    read_offset = int.from_bytes(probe[3:5], self.byteorder) - self.parameter_start_offset
            commands.append(IniCommand(name, template, length, identifier, read_page, read_offset))
        return commands

    def _read_pages(self):
        pages = {number: [] for number in range(1, self._integer("Constants", "nPages", 0) + 1)}
        for command in self.commands:
            if command.page is not None:
                pages.setdefault(command.page, [])
        offsets = {}
        names = {}

        def integer(value):
            return int(value, 16) if value.lower().startswith("0x") else int(value)

        def conversion(value):
            if value.startswith("{") and value.endswith("}"):
                return value[1:-1].strip()
            return float(value)

        for entry in self.entries:
            if entry.section != "Constants":
                continue
            kind = entry.value.partition(",")[0].strip()
            if kind not in {"scalar", "array", "bits", "string", "oddArray"}:
                continue
            try:
                if kind not in {"scalar", "array", "bits"}:
                    raise ValueError(f"Unsupported page field kind {kind!r}")
                parts = split_values(entry.value)
                page = entry.page or 1
                fields = pages.setdefault(page, [])
                previous, following = offsets.get(page, (0, 0))
                offset = {"lastOffset": previous, "nextOffset": following}.get(parts[2])
                if offset is None:
                    offset = integer(parts[2])
                name = entry.name + "_" if keyword.iskeyword(entry.name) else entry.name
                if not name.isidentifier() or name.startswith("_") or hasattr(PageResponse, name):
                    raise ValueError(f"Unsupported page property name {entry.name!r}")
                if name in names.setdefault(page, set()):
                    raise ValueError(f"Duplicate page property {name!r}")
                names[page].add(name)
                shape = ()
                bits = None
                scale, translate = 1.0, 0.0
                if kind == "bits":
                    match = re.fullmatch(r"\[\s*(\d+)\s*:\s*(\d+)\s*\]", parts[3])
                    if not match:
                        raise ValueError(f"Unsupported bit shape {parts[3]!r}")
                    bits = int(match[1]), int(match[2])
                else:
                    if kind == "array":
                        match = re.fullmatch(r"\[\s*(\d+)\s*(?:[xX]\s*(\d+)\s*)?\]", parts[3])
                        if not match:
                            raise ValueError(f"Unsupported array shape {parts[3]!r}")
                        shape = tuple(int(value) for value in match.groups() if value is not None)
                    index = 5 if kind == "array" else 4
                    scale, translate = conversion(parts[index]), conversion(parts[index + 1])
                field = PageField(name, parts[1].upper(), offset, shape, scale, translate, bits, entry.name)
                fields.append(field)
                offsets[page] = offset, offset + field.size
            except (ValueError, IndexError, SyntaxError) as exc:
                raise ValueError(f"INI line {entry.line}, field {entry.name}: {exc}") from exc

        result = {}
        for page, fields in sorted(pages.items()):
            value = self._page_value("pageSize", page)
            size = integer(value) if value is not None else None
            if size is not None and (size < 0 or any(field.offset + field.size > size for field in fields)):
                raise ValueError(f"Invalid layout or pageSize for page {page}")
            result[page] = IniPage(page, size, tuple(fields))
        return result

    def _response_length(self, entry, pattern, page):
        override = self._value(entry.section, entry.name + "ResponseLength")
        if override is not None:
            return int(override, 0)
        if "crc" in entry.name.lower():
            return 4
        if entry.name in {"pageValueWrite", "pageChunkWrite", "tableWriteCommand", "burnCommand", "pageActivate"}:
            return 0
        if "c" in pattern.variables and "v" not in pattern.variables:
            return "c"
        # MS2 standard reads encode a fixed response count in the last two bytes.
        values = {token: b"" if field.is_bytes else 0 for token, field in pattern.variables.items()}
        probe = CommandPattern(pattern.template, self.byteorder).render(values)
        if len(probe) == 7 and probe[:1] == b"r":
            return int.from_bytes(probe[5:7], self.byteorder)
        if entry.section == "ControllerCommands" and probe[:1] in {b"w", b"b"}:
            return 0
        if entry.name == "queryCommand":
            return len(self.signature) or None
        if entry.name in {"ochGetCommand", "getCommand"}:
            return self.och_block_size
        if entry.name == "pageReadCommand" and page:
            size = self._page_value("pageSize", page)
            return int(size, 0) if size else None
        return None

    def command_for_autodetect(self):
        pattern = CommandPattern(self.query_command.template, self.byteorder)
        values = {}
        for token in pattern.variables:
            if token != "tsCanId":
                raise ValueError(f"Cannot resolve autodetect variable {token!r}")
            values[token] = self.ts_can_id
        return pattern.render(values)
