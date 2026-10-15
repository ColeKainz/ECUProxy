"""Transport contract and ECU response types."""

from abc import ABC, abstractmethod


class ECUResponse(bytes):
    """Response data with the ECU's protocol status, excluding framing."""

    status: int

    def __new__(cls, data: bytes = b"", status: int = 0):
        response = super().__new__(cls, data)
        response.status = status
        return response


class ECUError(RuntimeError):
    """An error reported by the ECU, rather than a transport failure."""

    def __init__(self, status: int, message: str | None = None):
        self.status = status
        super().__init__(message or f"ECU returned protocol error status=0x{status:02x}")


class ECUInterface(ABC):
    @abstractmethod
    def exchange(self, command: bytes, response_length: int | None) -> bytes:
        """Exchange a command for data; response_length excludes protocol status."""

    def start(self):
        """Optionally start background transport work."""

    def close(self):
        """Optionally release transport resources."""
