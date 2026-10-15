"""Base class for generated and customized ECU command implementations."""

from tcp_ecu.protocol import ecu_command
from tcp_ecu.interfaces.base import ECUInterface


class ECUProxy:
    byteorder = "big"

    def __init__(self, interface: ECUInterface):
        self.interface = interface
        self._patterns = {}

    def _exchange(self, method_name, response_length, **values):
        if method_name not in self._patterns:
            spec = getattr(self, method_name).__ecu_command__
            self._patterns[method_name] = spec.compile(self.byteorder)
        command = self._patterns[method_name].render(values)
        return self.interface.exchange(command, response_length)

    @ecu_command("F", response_length=3)
    def protocol_version(self, response_length: int) -> bytes:
        return self.interface.exchange(b"F", response_length)
