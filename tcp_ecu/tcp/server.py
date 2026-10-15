import argparse
import importlib
import logging

from tcp_ecu.constants import BAUD_RATE, HOST, PORT
from tcp_ecu.ini.generator import add_ini_arguments, config_from_args, generate_proxy_class
from tcp_ecu.interfaces.serial import SerialECUInterface
from tcp_ecu.tcp.handler import TcpProxyHandler, ThreadingTCPServer


def parse_args():
    parser = argparse.ArgumentParser(description="Dispatch TunerStudio TCP requests through an INI-generated ECU proxy.")
    add_ini_arguments(parser)
    parser.add_argument("--host", default=HOST, help=f"TCP host to bind, default {HOST}")
    parser.add_argument("--port", type=int, default=PORT, help=f"TCP port to bind, default {PORT}")
    parser.add_argument("--serial-port", help="Serial port to use instead of auto-detecting")
    parser.add_argument("--baud", type=int, default=BAUD_RATE, help=f"Serial baud rate, default {BAUD_RATE}")
    parser.add_argument("--proxy-class", metavar="MODULE:CLASS", help="Load a generated or customized proxy class")
    parser.add_argument("--tcp-mode", choices=["auto", "raw", "framed"], default="auto")
    parser.add_argument("--check", action="store_true", help="Validate and list commands without connecting to an ECU")
    return parser.parse_args()


def main():
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    protocol_config = config_from_args(args)
    if args.proxy_class:
        module, separator, name = args.proxy_class.partition(":")
        if not separator:
            raise ValueError("--proxy-class must use MODULE:CLASS format")
        proxy_class = getattr(importlib.import_module(module), name)
    else:
        proxy_class = generate_proxy_class(protocol_config, args.class_name)
    interface = SerialECUInterface(protocol_config, port=args.serial_port, baud=args.baud)
    proxy = proxy_class(interface)
    if args.check:
        from tcp_ecu.protocol import CommandDispatcher

        dispatcher = CommandDispatcher(proxy)
        print(f"{proxy_class.__name__}: {len(dispatcher.commands)} annotated methods")
        for method, spec, _, _ in dispatcher.commands:
            print(f"  {method.__name__}: {spec.template!r} response_length={spec.response_length!r}")
        return

    try:
        with ThreadingTCPServer((args.host, args.port), TcpProxyHandler, proxy, tcp_mode=args.tcp_mode) as server:
            interface.start()
            print(f"TCP TunerStudio proxy listening on {args.host}:{server.server_address[1]}")
            print(f"Loaded {proxy_class.__name__}; ECU auto-detect is running in the background")
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        interface.close()


if __name__ == "__main__":
    main()
