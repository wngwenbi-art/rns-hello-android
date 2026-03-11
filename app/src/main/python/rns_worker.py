import RNS
import LXMF
import threading

destination = None
lxmf_router = None

class AndroidBTInterface(RNS.Interfaces.Interface):
    BITRATE_GUESS = 9600
    def __init__(self, socket):
        super().__init__()
        self.name = "RNodeBT"
        self.rxb = 0
        self.txb = 0
        self.online = True
        self._socket = socket
        threading.Thread(target=self._read_loop, daemon=True).start()

    def _read_loop(self):
        while self.online:
            try:
                data = self._socket.read(512)
                if data:
                    self.rxb += len(data)
                    self.processIncoming(bytes(data))
            except Exception as e:
                RNS.log(f"BT read error: {e}")
                self.online = False

    def processOutgoing(self, data):
        try:
            self._socket.write(data)
            self.txb += len(data)
        except Exception as e:
            RNS.log(f"BT write error: {e}")

def message_received(message):
    RNS.log(f"MSG from {RNS.prettyhexrep(message.source_hash)}: {message.content_as_string()}")

def start(bt_socket_wrapper):
    global destination, lxmf_router
    RNS.Reticulum(configdir=None, loglevel=RNS.LOG_DEBUG)
    iface = AndroidBTInterface(bt_socket_wrapper)
    RNS.Transport.interfaces.append(iface)
    identity = RNS.Identity()
    lxmf_router = LXMF.LXMRouter(storagepath="/data/data/com.example.rnshello/files/lxmf")
    destination = lxmf_router.register_delivery_identity(identity, display_name="RNS Hello Android")
    lxmf_router.register_delivery_callback(message_received)
    destination.announce()
    return RNS.prettyhexrep(destination.hash)

def send_hello(dest_hash_hex):
    global lxmf_router, destination
    if not lxmf_router or not destination:
        return "Not connected"
    try:
        msg = LXMF.LXMessage(
            destination_hash=bytes.fromhex(dest_hash_hex),
            source=destination,
            content="Hello World",
            title="Hello",
            desired_method=LXMF.LXMessage.DIRECT
        )
        lxmf_router.handle_outbound(msg)
        return "Sent"
    except Exception as e:
        return f"Error: {e}"

def get_address():
    global destination
    return RNS.prettyhexrep(destination.hash) if destination else "Not initialized"
