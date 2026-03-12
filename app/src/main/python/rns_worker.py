import RNS
import LXMF
import threading
import signal
import os
import time
import struct
from RNS.Interfaces.Interface import Interface
from collections import deque

destination = None
lxmf_router = None
reticulum = None
_rns_started = False
_start_done = threading.Event()
_start_result = {"addr": None, "error": None}

# Shared state with Thread Safety
data_lock = threading.Lock()
chat_messages = []
seen_announces = []
known_identities = {}

# Corrected RNS_CONFIG: share_instance=False is often better for 
# standalone mobile apps to avoid background port conflicts.
RNS_CONFIG = """
[reticulum]
  enable_transport = True
  share_instance = False
  panic_on_interface_error = False

[interfaces]
"""

KISS_FEND       = 0xC0
KISS_FESC       = 0xDB
KISS_TFEND      = 0xDC
KISS_TFESC      = 0xDD
CMD_DATA        = 0x00
CMD_RADIO_STATE = 0x06
RADIO_STATE_ON  = 0x01

# ... (keep your kiss_escape, kiss_cmd, and configure_rnode functions as they are) ...

class AndroidBTInterface(Interface):
    BITRATE_GUESS = 1200

    def __init__(self, owner, name, socket):
        super().__init__()
        self.owner = owner
        self.name = name
        self._socket = socket
        
        # REQUIRED ATTRIBUTES for RNS Transport compatibility
        self.announce_rate_target = None
        self.announce_rate_grace = None
        self.announce_rate_penalty = None
        self.bitrate = self.BITRATE_GUESS
        self.online = True
        self.IN = True
        self.OUT = True
        self.FWD = False
        self.RPT = False
        
        # KISS State
        self._kiss_buf = []
        self._in_frame = False
        self._escape = False
        self.rxb = 0
        self.txb = 0

        threading.Thread(target=self._read_loop, daemon=True).start()

    def _read_loop(self):
        while self.online:
            try:
                data = self._socket.read(512)
                if data:
                    self._parse_kiss(data)
            except Exception as e:
                RNS.log(f"BT read error: {e}")
                self.online = False

    def _parse_kiss(self, data):
        for byte in data:
            if byte == KISS_FEND:
                if self._in_frame and len(self._kiss_buf) > 1:
                    # RNodes use Port 0 (0x00) for data. 
                    # We strip the port byte and pass the rest.
                    if self._kiss_buf[0] == 0x00:
                        pkt = bytes(self._kiss_buf[1:])
                        self.rxb += len(pkt)
                        try:
                            # This call failed previously due to missing attributes
                            self.owner.inbound(pkt, self)
                        except Exception as e:
                            RNS.log(f"Inbound processing error: {e}")
                    else:
                        # Log telemetry/status ports (0x25, 0x27 etc) if needed
                        pass
                self._kiss_buf = []
                self._in_frame = True
                self._escape = False
            elif self._in_frame:
                if byte == KISS_FESC:
                    self._escape = True
                elif self._escape:
                    self._escape = False
                    if byte == KISS_TFEND: self._kiss_buf.append(KISS_FEND)
                    elif byte == KISS_TFESC: self._kiss_buf.append(KISS_FESC)
                else:
                    self._kiss_buf.append(byte)

    def process_outgoing(self, data):
        try:
            # Wrap Reticulum data in KISS for the RNode hardware
            self._socket.write(kiss_cmd(CMD_DATA, data))
            self.txb += len(data)
        except Exception as e:
            RNS.log(f"BT write error: {e}")

# ... (keep message_received and announce_received as they are) ...

def _rns_main(bt_socket_wrapper):
    global destination, lxmf_router, reticulum
    try:
        configure_rnode(bt_socket_wrapper)
        
        configdir = "/data/data/com.example.rnshello/files/.reticulum"
        os.makedirs(configdir, exist_ok=True)
        with open(os.path.join(configdir, "config"), "w") as f:
            f.write(RNS_CONFIG)

        # 1. Initialize Reticulum first so Transport is ready
        reticulum = RNS.Reticulum(configdir=configdir, loglevel=RNS.LOG_DEBUG)
        
        # 2. Add interface to the live Transport instance
        iface = AndroidBTInterface(RNS.Transport, "RNodeBT", bt_socket_wrapper)
        RNS.Transport.interfaces.append(iface)

        identity_path = os.path.join(configdir, "identity")
        # ... (Identity loading logic remains the same) ...

        lxmf_router = LXMF.LXMRouter(
            storagepath="/data/data/com.example.rnshello/files/lxmf",
            autopeer=True
        )

        destination = lxmf_router.register_delivery_identity(identity, display_name="RNS Hello")
        lxmf_router.register_delivery_callback(message_received)
        RNS.Transport.register_announce_handler(AnnounceHandler())

        _start_result["addr"] = RNS.prettyhexrep(destination.hash)
        destination.announce()

    except Exception as e:
        RNS.log(f"RNS start error: {e}")
        _start_result["error"] = str(e)
    finally:
        _start_done.set()

def send_message(dest_hash_hex, text):
    # Clean input to prevent bytes.fromhex errors
    dest_hash_hex = dest_hash_hex.strip().replace("<", "").replace(">", "")
    try:
        dest_hash = bytes.fromhex(dest_hash_hex)
        with data_lock:
            recalled_identity = known_identities.get(dest_hash_hex)
        
        if not recalled_identity:
            recalled_identity = RNS.Identity.recall(dest_hash)
            
        if not recalled_identity:
            RNS.Transport.request_path(dest_hash)
            return "Path requested. Try again in a few seconds."

        # ... (Rest of send logic) ...