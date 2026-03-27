import time

class BtWrapper:
    """
    Wraps the Kotlin BluetoothService so Python/RNS can call read() and write().

    Key properties of the underlying Kotlin layer:
    - read() blocks for up to 2 seconds (socket SO_TIMEOUT), then returns b""
    - SocketTimeoutException is caught in Kotlin and returns ByteArray(0)
    - available() returns bytes ready without blocking
    - write() throws on BT disconnect so Python can log and move on
    """

    def __init__(self, kt_service):
        self._svc = kt_service

    def read(self, max_bytes=512):
        """
        Read up to max_bytes from the RNode.
        Blocks for up to 2 seconds (the socket SO_TIMEOUT set in Kotlin).
        Returns b"" on timeout or disconnect — never raises.
        """
        data = self._svc.read(max_bytes)
        if data is None:
            return b""
        return bytes(data)

    def available(self):
        """
        Return the number of bytes ready to read without blocking.
        Safe to call from any thread.
        """
        try:
            return int(self._svc.available())
        except Exception:
            return 0

    def read_available(self, max_bytes=512):
        """
        Non-blocking read: returns whatever bytes are in the buffer right now.
        Returns b"" immediately if nothing is available.
        Used by the drain loop so it can check the deadline without hanging.
        """
        if self.available() == 0:
            return b""
        return self.read(max_bytes)

    def drain(self, duration_secs: float = 3.0):
        """
        Read and discard all incoming bytes for duration_secs seconds.
        Because read() has a 2s timeout, this loop will return at most once
        per 2 seconds even if the RNode goes quiet — it won't hang.
        Returns total bytes discarded.
        """
        deadline = time.time() + duration_secs
        discarded = 0
        while time.time() < deadline:
            data = self.read(512)   # blocks up to 2s then returns
            if data:
                discarded += len(data)
        return discarded

    def write(self, data: bytes):
        self._svc.write(data)

    def disconnect(self):
        self._svc.disconnect()
