import binascii
import re
import struct

from tcp_ecu.constants import STANDARD_ENVELOPE_MAX_PAYLOAD


def crc32(payload):
    return binascii.crc32(payload) & 0xFFFFFFFF


def bytes_hex(value):
    return value.hex(" ")


def build_standard_envelope(payload):
    if not 0 < len(payload) <= STANDARD_ENVELOPE_MAX_PAYLOAD:
        raise ValueError("Invalid envelope payload length")
    return struct.pack(">H", len(payload)) + payload + struct.pack(">I", crc32(payload))


def decode_ini_string(value):
    match = re.search(r'"((?:\\.|[^"\\])*)"', value)
    if not match:
        return None

    text = match.group(1)
    output = bytearray()
    index = 0
    while index < len(text):
        char = text[index]
        if char != "\\":
            output.extend(char.encode("latin1"))
            index += 1
            continue

        if index + 1 >= len(text):
            output.append(ord("\\"))
            index += 1
            continue

        escape = text[index + 1]
        if escape == "x" and index + 3 < len(text):
            output.append(int(text[index + 2 : index + 4], 16))
            index += 4
        elif escape == "$":
            output.append(ord("$"))
            index += 2
        elif escape == "n":
            output.append(0x0A)
            index += 2
        elif escape == "r":
            output.append(0x0D)
            index += 2
        elif escape == "t":
            output.append(0x09)
            index += 2
        elif escape in "01234567":
            octal = re.match(r"[0-7]{1,3}", text[index + 1:])[0]
            output.append(int(octal, 8))
            index += len(octal) + 1
        else:
            output.append(ord(escape))
            index += 2

    return bytes(output)


def strip_inline_comment(line):
    in_quote = False
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            in_quote = not in_quote
            continue
        if char == ";" and not in_quote:
            return line[:index]
    return line


def parse_standard_read(command):
    if len(command) < 7 or command[:1] != b"r":
        return None

    return {
        "can_id": command[1],
        "page": command[2],
        "offset": struct.unpack(">H", command[3:5])[0],
        "count": struct.unpack(">H", command[5:7])[0],
    }


def unwrap_standard_response(frame):
    from tcp_ecu.interfaces.base import ECUError, ECUResponse

    if len(frame) < 7:
        raise ValueError("framed response is too short")

    payload_length = struct.unpack(">H", frame[:2])[0]
    if payload_length == 0 or len(frame) != payload_length + 6:
        raise ValueError("framed response length does not match its header")
    payload = frame[2 : 2 + payload_length]
    if struct.unpack(">I", frame[-4:])[0] != crc32(payload):
        raise ValueError("bad framed response CRC")
    status = payload[0]
    if status >= 0x80:
        raise ECUError(status)
    return ECUResponse(payload[1:], status=status)
