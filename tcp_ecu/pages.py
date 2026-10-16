"""Byte-preserving codecs for generated, optionally populated page responses."""

import ast
import copy
import math
import operator
import struct
from dataclasses import dataclass
from functools import lru_cache

from tcp_ecu.interfaces.base import ECUResponse


STORAGE_FORMATS = {
    "U08": "B", "S08": "b", "U16": "H", "S16": "h",
    "U32": "I", "S32": "i", "U64": "Q", "S64": "q",
    "F32": "f", "F64": "d",
}
_OPERATORS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
}


@lru_cache(maxsize=256)
def _expression_tree(expression):
    tree = ast.parse(expression, mode="eval").body
    for node in ast.walk(tree):
        if isinstance(node, (ast.Expression, ast.Load, ast.Name)):
            continue
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            continue
        if isinstance(node, ast.BinOp) and type(node.op) in _OPERATORS:
            continue
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            continue
        if type(node) in (*_OPERATORS, ast.UAdd, ast.USub):
            continue
        raise ValueError(f"Unsupported page-field expression: {expression!r}")
    return tree


def conversion_value(value, context):
    if not isinstance(value, str):
        return value

    def evaluate(node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            result = context.get(node.id)
            if not isinstance(result, (int, float)):
                raise KeyError(node.id)
            return result
        if isinstance(node, ast.UnaryOp):
            result = evaluate(node.operand)
            return -result if isinstance(node.op, ast.USub) else result
        return _OPERATORS[type(node.op)](evaluate(node.left), evaluate(node.right))

    try:
        return evaluate(_expression_tree(value))
    except KeyError:
        return None


def _equal(left, right):
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_equal(a, b) for a, b in zip(left, right))
    if isinstance(left, float) and isinstance(right, float):
        return left.hex() == right.hex()
    return left == right


@dataclass(frozen=True)
class PageField:
    name: str
    storage: str
    offset: int
    shape: tuple[int, ...] = ()
    scale: float | str = 1.0
    translate: float | str = 0.0
    bits: tuple[int, int] | None = None
    ini_name: str | None = None

    def __post_init__(self):
        if self.storage not in STORAGE_FORMATS:
            raise ValueError(f"Unsupported storage type {self.storage!r} for {self.name}")
        if self.offset < 0 or len(self.shape) > 2 or any(size <= 0 for size in self.shape):
            raise ValueError(f"Invalid layout for {self.name}")
        if self.bits is not None:
            low, high = self.bits
            if self.shape or self.storage.startswith("F") or not 0 <= low <= high < self.width * 8:
                raise ValueError(f"Invalid bit range for {self.name}")
        for value in (self.scale, self.translate):
            if isinstance(value, str):
                _expression_tree(value)
            elif not math.isfinite(value):
                raise ValueError(f"Non-finite conversion for {self.name}")
        if self.scale == 0:
            raise ValueError(f"Zero scale for {self.name}")

    @property
    def width(self):
        return struct.calcsize(STORAGE_FORMATS[self.storage])

    @property
    def size(self):
        return self.width * math.prod(self.shape)

    @property
    def annotation(self):
        result = ("int" if self.bits is not None or (
            not self.storage.startswith("F") and self.scale == 1 and self.translate == 0
        ) else "float")
        for _ in self.shape:
            result = f"list[{result}]"
        return result + " | None"

    def factors(self, context):
        scale = conversion_value(self.scale, context)
        translate = conversion_value(self.translate, context)
        if scale is None or translate is None:
            return None
        if not math.isfinite(scale) or not math.isfinite(translate) or scale == 0:
            raise ValueError(f"Invalid conversion for {self.name}")
        return scale, translate

    def decode(self, data, byteorder, factors):
        if self.bits is not None:
            low, high = self.bits
            return (int.from_bytes(data, byteorder) >> low) & ((1 << (high - low + 1)) - 1)
        scale, translate = factors
        fmt = (">" if byteorder == "big" else "<") + STORAGE_FORMATS[self.storage]
        values = [item[0] for item in struct.iter_unpack(fmt, data)]
        if scale != 1 or translate != 0:
            values = [(item + translate) * scale for item in values]
        if len(self.shape) == 2:
            rows, columns = self.shape
            return [values[row * columns:(row + 1) * columns] for row in range(rows)]
        return values if self.shape else values[0]

    def flatten(self, value):
        if not self.shape:
            return [value]
        if not isinstance(value, list) or len(value) != self.shape[0]:
            raise ValueError(f"{self.name} must have shape {self.shape}")
        if len(self.shape) == 1:
            return value
        if any(not isinstance(row, list) or len(row) != self.shape[1] for row in value):
            raise ValueError(f"{self.name} must have shape {self.shape}")
        return [item for row in value for item in row]

    def encode_value(self, value, byteorder, factors):
        if not isinstance(value, (int, float)):
            raise ValueError(f"{self.name} must contain numeric values")
        if self.bits is not None:
            low, high = self.bits
            if not isinstance(value, int) or not 0 <= value < 1 << (high - low + 1):
                raise ValueError(f"{self.name} does not fit its bit range")
            return (value << low).to_bytes(self.width, byteorder)
        scale, translate = factors
        raw = value if scale == 1 and translate == 0 else value / scale - translate
        if not self.storage.startswith("F"):
            if isinstance(raw, float):
                if not math.isfinite(raw):
                    raise ValueError(f"{self.name} cannot store a non-finite integer")
                raw = round(raw)
        fmt = (">" if byteorder == "big" else "<") + STORAGE_FORMATS[self.storage]
        try:
            return struct.pack(fmt, raw)
        except (struct.error, OverflowError) as exc:
            raise ValueError(f"{self.name} does not fit {self.storage}: {value!r}") from exc


class PageResponse:
    """Generated dataclass fields are snapshots; only edited values are encoded."""

    _page_fields = ()
    _page_size = None
    _byteorder = "big"

    @classmethod
    def from_bytes(cls, data: bytes, *, offset=0, context=None):
        if not isinstance(data, bytes):
            raise TypeError("Page data must be bytes")
        if not isinstance(offset, int) or offset < 0:
            raise ValueError("Page offset must be a nonnegative integer")
        diagnostic = getattr(data, "status", 0) == 0x03
        if not diagnostic and cls._page_size is not None and offset + len(data) > cls._page_size:
            raise ValueError("Response extends past the end of the page")
        result = cls()
        result._raw = data
        result._offset = offset
        result._context = dict(context or {})
        result._original = {}
        result._diagnostic = diagnostic
        pending = []
        for field in cls._page_fields:
            start = field.offset - offset
            if not diagnostic and 0 <= start and start + field.size <= len(data):
                pending.append(field)
        available_names = {field.ini_name or field.name for field in pending}
        values = {name: value for name, value in result._context.items() if name not in available_names}
        # Resolve conversions that depend on other fully received fields.
        while pending:
            remaining = []
            for field in pending:
                factors = field.factors(values)
                if factors is None:
                    remaining.append(field)
                    continue
                start = field.offset - offset
                value = field.decode(data[start:start + field.size], cls._byteorder, factors)
                setattr(result, field.name, value)
                result._original[field.name] = copy.deepcopy(value)
                values[field.ini_name or field.name] = value
            if len(remaining) == len(pending):
                break
            pending = remaining
        return result

    def to_bytes(self) -> bytes:
        if not hasattr(self, "_raw"):
            raise ValueError("Create a page response with from_bytes() before serializing it")
        output = bytearray(self._raw)
        context = dict(self._context)
        context.update({field.ini_name or field.name: getattr(self, field.name)
                        for field in self._page_fields if getattr(self, field.name) is not None})
        patches = {}
        for field in self._page_fields:
            value = getattr(self, field.name)
            original = self._original.get(field.name)
            if value is None or _equal(value, original):
                continue
            start = field.offset - self._offset
            if self._diagnostic or start < 0 or start + field.size > len(output):
                raise ValueError(f"{field.name} is outside the received page segment")
            factors = field.factors(context)
            if factors is None:
                raise ValueError(f"Missing conversion context for {field.name}")
            values = field.flatten(value)
            originals = field.flatten(original) if original is not None else [None] * len(values)
            mask = b"\xff" * field.width
            if field.bits is not None:
                low, high = field.bits
                mask = (((1 << (high - low + 1)) - 1) << low).to_bytes(field.width, self._byteorder)
            for index, (item, previous) in enumerate(zip(values, originals)):
                if _equal(item, previous):
                    continue
                encoded = field.encode_value(item, self._byteorder, factors)
                for byte_index, (byte, bit_mask) in enumerate(zip(encoded, mask)):
                    position = start + index * field.width + byte_index
                    old_mask, old_value = patches.get(position, (0, 0))
                    if (old_value ^ byte) & old_mask & bit_mask:
                        raise ValueError(f"Conflicting edits to overlapping field {field.name}")
                    patches[position] = old_mask | bit_mask, old_value | (byte & bit_mask)
        for position, (mask, value) in patches.items():
            output[position] = (output[position] & ~mask) | value
        data = bytes(output)
        return ECUResponse(data, status=self._raw.status) if isinstance(self._raw, ECUResponse) else data
