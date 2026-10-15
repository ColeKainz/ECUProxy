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

## Generated Classes

The server generates its proxy class in memory by default. To produce an editable,
importable Python module:

```sh
python3 generate_proxy.py /path/to/mainController-tcp.ini \
  --class-name MS2Proxy --output ms2_proxy.py
python3 server.py /path/to/mainController-tcp.ini --proxy-class ms2_proxy:MS2Proxy
```

A generated page-read method has this structure:

```python
from typing import Annotated
from tcp_ecu.protocol import IniParameter, ecu_command
from tcp_ecu.proxy import ECUProxy

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
        return self._exchange(
            "page_read_command_1", response_length,
            tsCanId=ts_can_id, o=offset, c=count,
        )
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
