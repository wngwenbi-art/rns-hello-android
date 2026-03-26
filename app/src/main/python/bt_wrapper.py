class BtWrapper:
    """Wraps the Kotlin BluetoothService so Python/RNS can call read() and write()."""
    def __init__(self, kt_service):
        self._svc = kt_service

    def read(self, max_bytes=512):
        data = self._svc.read(max_bytes)
        if data is None:
            return b""
        # Chaquopy returns a bytearray-like object, convert to bytes
        return bytes(data)

    def write(self, data: bytes):
        self._svc.write(data)

    def disconnect(self):
        self._svc.disconnect()
