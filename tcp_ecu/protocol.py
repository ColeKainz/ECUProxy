"""Compile TunerStudio command templates and dispatch their binary requests."""

import inspect
import re
from dataclasses import dataclass
from typing import Annotated, get_args, get_origin, get_type_hints

from tcp_ecu.constants import STANDARD_ENVELOPE_MAX_PAYLOAD


class IncompleteCommand(ValueError):
    """The request is a valid prefix, but more TCP bytes are needed."""


class UnknownCommand(ValueError):
    pass


@dataclass(frozen=True)
class IniParameter:
    token: str


@dataclass(frozen=True)
class Variable:
    token: str
    width: int | None
    is_bytes: bool = False


@dataclass(frozen=True)
class CommandSpec:
    template: str
    response_length: int | str | None = None
    byteorder: str | None = None
    page_identifier: str | None = None
    value_length: int | None = None

    def compile(self, byteorder="big"):
        return CommandPattern(
            self.template, self.byteorder or byteorder,
            page_identifier=self.page_identifier, value_length=self.value_length,
        )


def ecu_command(template, *, response_length=None, byteorder=None,
                page_identifier=None, value_length=None):
    """Annotate a method with its INI request and response-length policy.

    A string response_length names a captured token (e.g. "c"). None means
    that the transport must obtain the length from framing or a read timeout.
    """
    spec = CommandSpec(template, response_length, byteorder, page_identifier, value_length)

    def decorate(method):
        method.__ecu_command__ = spec
        return method

    return decorate


class CommandPattern:
    def __init__(self, template, byteorder="big", *, page_identifier=None, value_length=None):
        if byteorder not in {"big", "little"}:
            raise ValueError(f"Invalid endianness: {byteorder!r}")
        self.template = template
        self.byteorder = byteorder
        if value_length is not None and (not isinstance(value_length, int) or value_length < 0):
            raise ValueError("value_length must be a nonnegative integer")
        self.value_length = value_length
        self.parts = self._tokenize(template)
        if not self.parts:
            raise ValueError("An empty string is not a wire command")
        self.variables = {}
        for part in self.parts:
            if isinstance(part, Variable):
                if part.token in self.variables and part != self.variables[part.token]:
                    raise ValueError(f"Inconsistent widths for {part.token!r}")
                self.variables[part.token] = part
        self.identifier = CommandPattern(page_identifier, byteorder) if page_identifier else None
        if self.identifier and "i" not in self.variables:
            raise ValueError("page_identifier requires an %i token")
        self.specificity = sum(len(part) for part in self.parts if isinstance(part, bytes))
        if self.identifier:
            self.specificity += self.identifier.specificity

    @staticmethod
    def _tokenize(template):
        parts = []
        literal = bytearray()
        index = 0

        def flush():
            if literal:
                parts.append(bytes(literal))
                literal.clear()

        while index < len(template):
            char = template[index]
            if template.startswith(r"\$", index):
                match = re.match(r"\\\$([A-Za-z_]\w*)", template[index:])
                if not match:
                    raise ValueError(f"Invalid variable in {template!r}")
                flush()
                parts.append(Variable(match[1], 1))
                index += len(match[0])
            elif char == "%":
                if template.startswith("%%", index):
                    literal.append(ord("%"))
                    index += 2
                    continue
                match = re.match(r"%(\d*)([A-Za-z])", template[index:])
                if not match:
                    raise ValueError(f"Invalid placeholder in {template!r}")
                flush()
                width = int(match[1]) if match[1] else None
                if width == 0 or (width is not None and width > 8 and match[2] != "v"):
                    raise ValueError(f"Invalid placeholder width in {match[0]!r}")
                parts.append(Variable(match[2], width if match[2] == "v" else width or 1,
                                      match[2] == "v"))
                index += len(match[0])
            elif char == "\\":
                if index + 1 >= len(template):
                    raise ValueError(f"Trailing escape in {template!r}")
                escape = template[index + 1]
                if escape == "x":
                    digits = template[index + 2:index + 4]
                    if len(digits) != 2 or not re.fullmatch(r"[0-9a-fA-F]{2}", digits):
                        raise ValueError(f"Invalid hex escape in {template!r}")
                    literal.append(int(digits, 16))
                    index += 4
                elif escape in "01234567":
                    match = re.match(r"[0-7]{1,3}", template[index + 1:])
                    literal.append(int(match[0], 8))
                    index += len(match[0]) + 1
                else:
                    escapes = {"n": 10, "r": 13, "t": 9, "\\": 92, '"': 34, "%": 37}
                    if escape not in escapes:
                        raise ValueError(f"Unsupported escape \\{escape} in {template!r}")
                    literal.append(escapes[escape])
                    index += 2
            else:
                literal.extend(char.encode("latin1"))
                index += 1
        flush()
        return tuple(parts)

    def _value_size(self, variable, values, data=None, position=0, part_index=0, complete=False):
        if "c" in values:
            return values["c"]
        if self.value_length is not None:
            return self.value_length
        if variable.width is not None:
            return variable.width
        if data is None:
            return len(values[variable.token])
        if complete and part_index == len(self.parts) - 1:
            return len(data) - position
        raise IncompleteCommand("An unbounded %v requires a framed request or value_length")

    def match(self, data, *, complete=False):
        """Return (captured values, consumed bytes), None, or IncompleteCommand."""
        values = {}
        position = 0
        for part_index, part in enumerate(self.parts):
            if isinstance(part, bytes):
                available = data[position:position + len(part)]
                if not part.startswith(available):
                    return None
                size = len(part)
            else:
                size = (self._value_size(part, values, data, position, part_index, complete)
                        if part.is_bytes else part.width)
            if size < 0 or position + size > STANDARD_ENVELOPE_MAX_PAYLOAD:
                raise ValueError("Command exceeds maximum payload size")
            if len(data) < position + size:
                raise IncompleteCommand(self.template)
            if isinstance(part, Variable):
                raw = data[position:position + size]
                value = raw if part.is_bytes else int.from_bytes(raw, self.byteorder)
                if part.token in values and values[part.token] != value:
                    return None
                if part.token == "i" and self.identifier:
                    try:
                        identifier_match = self.identifier.match(raw, complete=True)
                    except IncompleteCommand:
                        return None
                    if identifier_match is None:
                        return None
                values[part.token] = value
            position += size
        if complete and position != len(data):
            return None
        return values, position

    def render(self, values):
        output = bytearray()
        for part in self.parts:
            if isinstance(part, bytes):
                output.extend(part)
                continue
            value = values[part.token]
            if part.is_bytes:
                if not isinstance(value, bytes):
                    raise ValueError(f"{part.token} must be bytes")
                if len(value) != self._value_size(part, values):
                    raise ValueError(f"{part.token} length does not match the command count")
                output.extend(value)
            else:
                if not isinstance(value, int) or not 0 <= value < 1 << (8 * part.width):
                    raise ValueError(f"{part.token} must fit in {part.width} unsigned bytes")
                output.extend(value.to_bytes(part.width, self.byteorder))
        if len(output) > STANDARD_ENVELOPE_MAX_PAYLOAD:
            raise ValueError("Command exceeds maximum payload size")
        if self.identifier and self.match(bytes(output), complete=True) is None:
            raise ValueError("Identifier does not match this page")
        return bytes(output)


@dataclass
class CommandMatch:
    method: object
    arguments: dict
    consumed: int

    def invoke(self):
        response = self.method(**self.arguments)
        if not isinstance(response, bytes):
            raise TypeError(f"{self.method.__name__} must return bytes")
        return response


class CommandDispatcher:
    def __init__(self, proxy):
        self.proxy = proxy
        self.commands = []
        for name, method in inspect.getmembers(proxy, predicate=inspect.ismethod):
            spec = getattr(method, "__ecu_command__", None)
            if spec is None:
                continue
            pattern = spec.compile(getattr(proxy, "byteorder", "big"))
            signature = inspect.signature(method)
            parameters = signature.parameters
            if "response_length" not in parameters:
                raise ValueError(f"{name} must declare response_length")
            hints = get_type_hints(method, include_extras=True)
            bindings = {}
            for parameter in parameters.values():
                if parameter.kind not in {parameter.POSITIONAL_OR_KEYWORD, parameter.KEYWORD_ONLY}:
                    raise ValueError(f"{name}: parameters must accept keyword arguments")
                hint = hints.get(parameter.name)
                markers = ([item for item in get_args(hint)[1:] if isinstance(item, IniParameter)]
                           if get_origin(hint) is Annotated else [])
                token = markers[0].token if markers else parameter.name
                if token in pattern.variables:
                    if token in bindings:
                        raise ValueError(f"{name}: duplicate binding for {token}")
                    bindings[token] = parameter.name
                elif markers or (parameter.name != "response_length" and
                                 parameter.default is inspect.Parameter.empty):
                    raise ValueError(f"{name}: {parameter.name} has no command variable")
            missing = pattern.variables.keys() - bindings.keys()
            if missing:
                raise ValueError(f"{name}: missing parameters for {sorted(missing)}")
            if isinstance(spec.response_length, str) and spec.response_length not in bindings:
                raise ValueError(f"{name}: response length token is not in the template")
            if (spec.response_length is not None and not isinstance(spec.response_length, str)
                    and (not isinstance(spec.response_length, int)
                         or not 0 <= spec.response_length < STANDARD_ENVELOPE_MAX_PAYLOAD)):
                raise ValueError(f"{name}: invalid response length")
            self.commands.append((method, spec, pattern, bindings))
        if not self.commands:
            raise ValueError("Proxy has no annotated commands")
        self.commands.sort(key=lambda item: item[2].specificity, reverse=True)

    def match(self, data, *, complete=False):
        pending_score = -1
        candidates = []
        invalid = []
        for method, spec, pattern, bindings in self.commands:
            if candidates and pattern.specificity < candidates[0][0].specificity:
                break
            try:
                result = pattern.match(data, complete=complete)
            except IncompleteCommand:
                pending_score = max(pending_score, pattern.specificity)
                continue
            except ValueError as exc:
                # A competing layout may decode these bytes as an oversized count.
                # It must not reject a valid (possibly fragmented) specific command.
                invalid.append(exc)
                continue
            if result is None:
                continue
            values, consumed = result
            arguments = {bindings[token]: value for token, value in values.items()}
            if "response_length" not in arguments:
                arguments["response_length"] = (values[spec.response_length]
                                                if isinstance(spec.response_length, str)
                                                else spec.response_length)
            length = arguments["response_length"]
            if length is not None and (not isinstance(length, int) or not 0 <= length < STANDARD_ENVELOPE_MAX_PAYLOAD):
                raise ValueError("Invalid response length")
            candidates.append((pattern, spec, CommandMatch(method, arguments, consumed)))
        if candidates:
            pattern, spec, match = candidates[0]
            if not complete and pending_score >= pattern.specificity:
                raise IncompleteCommand("A more specific command needs more bytes")
            for other_pattern, other_spec, other_match in candidates[1:]:
                if other_pattern.specificity != pattern.specificity:
                    break
                # INIs often give pageValueWrite and pageChunkWrite identical bytes.
                if (other_pattern.parts != pattern.parts or other_match.consumed != match.consumed
                        or other_spec.response_length != spec.response_length):
                    raise ValueError("Ambiguous command templates")
            return match
        if pending_score >= 0 and not complete:
            raise IncompleteCommand("Waiting for the rest of the command")
        if invalid:
            raise invalid[0]
        raise UnknownCommand(f"No command matches {data.hex(' ')}")

    def dispatch(self, data):
        return self.match(data, complete=True).invoke()
