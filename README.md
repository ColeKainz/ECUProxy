# TunerStudio TCP ECU Proxy

TunerStudio requests are dispatched to an ECU-specific Python class generated from
the ECU's INI. Each generated method has a command decorator, typed parameters for
the command's variables, and a required `response_length` parameter. An injected
`ECUInterface` performs communication with the connected ECU.

```text
TunerStudio -> TCP framing -> CommandDispatcher -> <ECU>Proxy -> ECUInterface -> ECU
```

Python 3.10 or newer is required. Only serial transport needs a third-party package:

```sh
python3 -m pip install pyserial
python3 server.py /path/to/mainController-tcp.ini --class-name MS2Proxy
```

The server binds to `0.0.0.0:30000` by default. Use `--host`, `--port`,
`--serial-port`, and `--baud` to override these settings. Serial autodetection
compares the ECU response with the loaded INI's signature. `--can-id` selects the
CAN ID used for that query; incoming requests retain their own CAN ID.

Validate the INI and list the generated commands without opening serial ports or
starting a TCP listener:

```sh
python3 server.py /path/to/mainController-tcp.ini --class-name MS2Proxy --check
```

## Project Layout

```text
tcp_ecu/
  ini/
    config.py          # INI loading and command definitions
    generator.py       # Proxy class generation and generator CLI
  interfaces/
    base.py            # ECUInterface, ECUResponse, ECUError
    serial.py          # Serial transport
    serial_proxy.py    # Legacy class-name alias
  tcp/
    handler.py         # TCP buffering, framing, and dispatch
    server.py          # Server configuration and CLI
  protocol.py          # Command decorators, templates, and dispatcher
  pages.py             # Byte-preserving codecs for generated page responses
  proxy.py             # ECUProxy base class
  constants.py         # Connection defaults and protocol constants
  utils.py             # Shared INI and framing helpers
tests/
  fixtures/            # Synthetic ECU definitions
server.py              # Server launcher
generate_proxy.py      # Generator launcher
Dockerfile
```

The root launchers keep the commands above working. Module entry points are also
available: `python3 -m tcp_ecu.tcp.server` and
`python3 -m tcp_ecu.ini.generator`, with the same arguments.
Python imports now use the `tcp_ecu` package. Regenerate previously generated
proxy modules to update their imports, or update the imports in customized modules
using the examples below.

## Project Definition

`mainController.ini` is the project-local copy of the original ECU definition,
converted from Latin-1 to UTF-8. It adds an eight-byte page 8 (wire page `0x0d`)
with two unsigned 32-bit integers in Hz:

| Page Property | Byte Offset | Output Channel Alias |
| --- | --- | --- |
| `output_shaft_pulse_rate` | 0 | `outputShaftPulseRate` |
| `cruise_control_pulse_rate` | 4 | `cruiseControlPulseRate` |

The aliases expose stored page values, not live pulse measurements. They follow
TunerStudio's expression-channel syntax and reference its loaded constants;
the original 212-byte telemetry block is unchanged. See the
[EFI Analytics INI specification](https://www.efianalytics.com/TunerStudio/docs/EFI%20Analytics%20ECU%20Definition%20files.pdf),
section 11.1.3.

This defines the new page's protocol but does not implement backing storage.
The generated proxy forwards its reads, writes, burn, and CRC commands to the
injected `ECUInterface`. Implement page `0x0d` in the proxy/interface or ECU
before using this definition with hardware; the original ECU firmware is not
made aware of it by changing the INI.

Regenerate and validate the project proxy without connecting to hardware:

```sh
python3 generate_proxy.py mainController.ini --class-name MainControllerProxy --output main_controller_proxy.py
python3 server.py mainController.ini --proxy-class main_controller_proxy:MainControllerProxy --check
```

## Generated Classes

The server generates its proxy class in memory by default. To produce an editable,
importable Python module:

```sh
python3 generate_proxy.py /path/to/mainController-tcp.ini \
  --class-name MS2Proxy --output ms2_proxy.py
python3 server.py /path/to/mainController-tcp.ini --proxy-class ms2_proxy:MS2Proxy
```

With `--output`, page response classes are written to a sibling package named
`<output_stem>_pages`, one class per file. The proxy imports those classes:

```text
ms2_proxy.py
ms2_proxy_pages/
  __init__.py
  page_1_response.py
  page_2_response.py
  ...
```

Keep the proxy module and its page package together when moving or deploying
them. Regeneration overwrites the generated files, so preserve any custom edits
first. In-memory generation and stdout output remain self-contained; neither
writes a page package to disk.

A generated page-read method has this structure:

```python
from typing import Annotated
from tcp_ecu.protocol import IniParameter, ecu_command
from tcp_ecu.proxy import ECUProxy
from ms2_proxy_pages import Page1Response

class MS2Proxy(ECUProxy):
    @ecu_command(r"r\$tsCanId\x04%2o%2c", response_length="c")
    def page_read_command_1(
        self,
        ts_can_id: Annotated[int, IniParameter("tsCanId")],
        offset: Annotated[int, IniParameter("o")],
        count: Annotated[int, IniParameter("c")],
        *,
        response_length: int | None,
    ) -> bytes:
        data = self._exchange(
            "page_read_command_1", response_length,
            tsCanId=ts_can_id, o=offset, c=count,
        )
        page = Page1Response.from_bytes(data, offset=offset)
        # Customize populated page properties here before serialization.
        return page.to_bytes()
```

`MS2Proxy(interface)` inherits the `ECUInterface` constructor from `ECUProxy`.
For example, `72 03 04 01 02 00 10` invokes this method with CAN ID 3, offset 258,
count 16, and response length 16. The compiled template contains literal byte
segments and `Variable` delimiters; delimiters are metadata, never extra wire bytes.
The dispatcher discovers decorated methods when the server loads the proxy.
Custom methods can name parameters differently using `Annotated[..., IniParameter(...)]`.
An undecorated override removes that method from dispatch; redecorate overrides
to retain or change their wire command.

Command-list entries get numbered methods, such as `page_read_command_1` through
`page_read_command_7`. Repeated logger definitions and controller command chains
also get numbered methods. Each chain component is dispatched separately, as
TunerStudio sends it. Identical wire templates are aliases; the alphabetically
first matching method handles them. More specific literal templates take precedence
over generic templates, and ambiguous non-alias matches are rejected.

## Page Responses

The generator also emits a dataclass for each tuning page, such as
`Page1Response`, with typed properties named after the INI's constants. Every
property defaults to `None`. Generated page-read methods decode the ECU's bytes,
provide a place to customize the page, and serialize it back to bytes. Other
command responses are unchanged. Classes are also available through
`Proxy.page_response_types[page_number]` when generating a proxy in memory.

```python
page = Page1Response.from_bytes(data, offset=requested_offset)
if page.nCylinders is not None:
    page.nCylinders = 8
return page.to_bytes()
```

`offset` is the zero-based position of the received segment in the page. Only
fields completely contained in that segment are populated; an array remains
`None` unless all its elements were received. Unchanged responses round-trip
byte-for-byte, including padding, unknown bytes, floating-point representations,
and ECU status. Serialization preserves the original segment length. Start with
`from_bytes()` rather than a blank instance so those backing bytes are available.

Numeric scalars, bit fields, and fixed one- or two-dimensional arrays honor the
INI's storage types and byte order. Arrays become lists or row-by-row nested
lists. Scaled properties use `(raw + translate) * scale`; edited integer-backed
values are rounded to the nearest representable raw integer and range-checked.
Only edited properties or array elements are written. Bit-field edits preserve
neighboring bits, and contradictory edits to overlapping aliases raise an error.
Aliases are snapshots, not automatically synchronized views. Setting a property
to `None` leaves its original bytes untouched; editing an unavailable field is
rejected.

Scale and translation expressions support numeric constants, field references,
and basic arithmetic. Same-page dependencies resolve automatically when their
fields were received. Missing dependencies leave the property `None`; supply
known values from other pages or earlier reads with `context`, for example
`Page7Response.from_bytes(data, context={"maf_range": 1})`. Generated methods do
not cache other pages. Strings, variable-length arrays, and more complex
conversion expressions fail explicitly during generation instead of guessing a
layout or conversion. ECU configuration-error responses remain opaque.

## Lengths and Framing

- `\$tsCanId` and other `\$name` variables capture one unsigned byte.
- `%2o`, `%2c`, and other `%<width><letter>` tokens capture unsigned integers using
  the INI's `endianness`. An omitted width defaults to one byte.
- `%i` fields can be constrained by the corresponding `pageIdentifier`.
- `%v` captures binary write data; a preceding `%c` supplies its byte length.
  Without a count, a width or decorator `value_length` supplies the length.
  An unbounded final `%v` is supported only inside a complete framed request.
- Every method receives `response_length`, counting only response data, excluding
  framing and the status byte. Read counts, page sizes, output block sizes, fixed
  MS2 read requests, and CRC commands supply known lengths. Writes and burns use
  zero. Unknown lengths use `None`, meaning the interface reads the frame's length
  or waits for the raw serial timeout. Counted writes never treat `%c` as a response
  length. A decorator may also bind `%c` directly to `response_length`.
- This project's optional `<commandName>ResponseLength` INI extension sets a fixed
  response length when it cannot be inferred; for example `versionInfoResponseLength = 60`.

TCP supports raw requests and the existing two-byte length / payload / CRC32
envelope. Framed responses add the ECU's status byte before the response data.
CRC is checked before invoking a method; fragmented and coalesced TCP requests
are buffered. ECU status/error codes are preserved. The serial interface handles
its own framing after probing protocol `002`, independently of the TCP framing.

`--tcp-mode auto` accepts a raw `F` probe followed by framed traffic. Raw command
prefixes take precedence in this mode. Binary raw commands and large frame-length
headers can be indistinguishable; use `--tcp-mode framed` or `--tcp-mode raw` when
the connection uses only one format.

The INI reader supports command-bearing sections, per-page command lists,
`[ControllerCommands]`, repeated logger commands, and `#set`, `#unset`, `#if`,
`#elif`, `#else`, `#endif`, and `#error`. Boolean conditions support names, `!`,
`&&`, and `||`. `INI_VERSION_2` is enabled as TunerStudio does. Use `--define NAME`
and `--undefine NAME` to override file flags. Other directives, including
`#include`, fail explicitly; this is a command reader, not a complete INI UI engine.

## Other Interfaces

Implement `ECUInterface.exchange(command: bytes, response_length: int | None)` to
use another transport or a test double. Return `bytes`, or `ECUResponse(data,
status=...)` to retain a protocol status. Raise `ECUError(status)` for ECU-reported
errors. `start()` and `close()` are optional lifecycle hooks.

```python
from tcp_ecu.ini.generator import generate_proxy_class
from tcp_ecu.ini.config import IniProtocolConfig
from tcp_ecu.tcp.handler import TcpProxyHandler, ThreadingTCPServer

config = IniProtocolConfig("/path/to/ecu.ini")
Proxy = generate_proxy_class(config, "MyECUProxy")
proxy = Proxy(my_interface)
with ThreadingTCPServer(("127.0.0.1", 30000), TcpProxyHandler, proxy) as server:
    server.serve_forever()
```

`SerialECUInterface` serializes exchanges across TCP clients, reconnects after
transport failures, and verifies response lengths and CRCs. `tcp_ecu/interfaces/serial_proxy.py`
retains the old class import name as an alias; the transport API is now `exchange`.

## Verification

```sh
python3 -m unittest discover -v
```

Tests use fake ECU connections and a temporary localhost TCP listener. They do
not open serial devices or send commands to an actual ECU.

## Docker

```sh
docker build -t tcp-ecu .
docker run --rm -p 30000:30000 \
  --device /dev/ttyUSB0:/dev/ttyUSB0 \
  -v /path/to/projectCfg:/config:ro \
  tcp-ecu /config/mainController-tcp.ini --serial-port /dev/ttyUSB0
```

The device mapping example targets a Linux Docker host.

Protocol references: [EFI Analytics INI specification](https://www.efianalytics.com/TunerStudio/docs/EFI%20Analytics%20ECU%20Definition%20files.pdf),
[controller commands](https://www.tunerstudio.com/index.php/support/manuals/tsdevmanuals/80-command-buttons),
and [Megasquirt serial protocol](https://www.msextra.com/doc/pdf/Megasquirt_Serial_Protocol-2014-10-28.pdf).
