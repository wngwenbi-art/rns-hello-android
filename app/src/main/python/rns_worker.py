Set-Content app/src/main/python/rns_worker.py @'
import RNS
import LXMF
import threading
import signal
import os
import time
import struct
from RNS.Interfaces.Interface import Interface
from collections import deque

# --- GLOBAL LORA TIMEOUT PATCH ---
# This prevents the exact bug you found! If the path table ever lacks a bitrate,
# RNS will fall back to this number instead of 1.0 seconds. 
# 25 seconds is enough for a slow SF8 / 125kHz round-trip.
RNS.Link.ESTABLISHMENT_TIMEOUT_PER_HOP = 25.0
RNS.Link.KEEPALIVE_TIMEOUT_FACTOR = 360

destination       = None
image_destination = None
lxmf_router       = None
reticulum         = None
_rns_started      = False
_start_done       = threading.Event()
_start_result     = {"addr": None, "error": None}

_data_lock        = threading.Lock()
chat_messages     = deque(maxlen=500)
seen_announces    =[]
known_identities  = {}
active_links      = {}
image_peer_hashes = {}

_IMAGES_DIR = "/data/data/com.reticulum.mesh/files/images"

def _save_image_file(img_bytes: bytes, sender: str) -> str:
    try:
        os.makedirs(_IMAGES_DIR, exist_ok=True)
        ts_tag = time.strftime("%Y%m%d_%H%M%S")
        filename = f"img_{sender[:8]}_{ts_tag}.webp"
        filepath = os.path.join(_IMAGES_DIR, filename)
        with open(filepath, "wb") as f:
            f.write(img_bytes)
        return filepath
    except Exception as e:
        RNS.log(f"Image save error: {e}")
        return ""

KISS_FEND       = 0xC0
KISS_FESC       = 0xDB
KISS_TFEND      = 0xDC
KISS_TFESC      = 0xDD
CMD_DATA        = 0x00
CMD_FREQUENCY   = 0x01
CMD_BANDWIDTH   = 0x02
CMD_TXPOWER     = 0x03
CMD_SF          = 0x04
CMD_CR          = 0x05
CMD_RADIO_STATE = 0x06
CMD_DETECT      = 0x08
CMD_READY       = 0x0F
RADIO_STATE_ON  = 0x01

def kiss_escape(data):
    out =[]
    for b in data:
        if b == KISS_FEND:
            out += [KISS_FESC, KISS_TFEND]
        elif b == KISS_FESC:
            out +=[KISS_FESC, KISS_TFESC]
        else:
            out.append(b)
    return bytes(out)

def kiss_cmd(cmd, data=b""):
    return bytes([KISS_FEND, cmd]) + kiss_escape(data) + bytes([KISS_FEND])

def configure_rnode(socket):
    freq  = 433025000
    bw    = 125000
    txpwr = 17
    sf    = 8
    cr    = 6
    
    socket.write(kiss_cmd(CMD_DETECT, bytes([0x00])))
    time.sleep(0.3)
    socket.write(kiss_cmd(CMD_RADIO_STATE, bytes([0x00])))
    time.sleep(0.8)
    socket.write(kiss_cmd(CMD_FREQUENCY, struct.pack(">I", freq)))
    time.sleep(0.2)
    socket.write(kiss_cmd(CMD_BANDWIDTH, struct.pack(">I", bw)))
    time.sleep(0.2)
    socket.write(kiss_cmd(CMD_TXPOWER, bytes([txpwr])))
    time.sleep(0.2)
    socket.write(kiss_cmd(CMD_SF, bytes([sf])))
    time.sleep(0.2)
    socket.write(kiss_cmd(CMD_CR, bytes([cr])))
    time.sleep(0.2)
    socket.write(kiss_cmd(CMD_RADIO_STATE, bytes([RADIO_STATE_ON])))
    time.sleep(1.5)
    socket.write(kiss_cmd(CMD_READY, bytes([0x00])))
    time.sleep(0.2)
    RNS.log("RNode configured and ON")

class AndroidBTInterface(Interface):
    def __init__(self, owner, name, socket, bandwidth=125000, spreading_factor=8, coding_rate=6):
        super().__init__()
        self.owner  = owner
        self.name   = name
        self.online = False
        self.IN     = True
        self.OUT    = True
        self.FWD    = False
        self.RPT    = False
        self.rxb    = 0
        self.txb    = 0
        self._socket = socket
        
        # --- CRITICAL FIX 1: Prevent 3-byte truncation bug ---
        self.HW_MTU = 500 
        
        # --- CRITICAL FIX 2: Compute real bitrate as you discovered ---
        symbol_rate = bandwidth / (2 ** spreading_factor)
        self.bitrate = int(symbol_rate * spreading_factor * (4.0 / (4.0 + coding_rate)))
        RNS.log(f"Interface Bitrate Set: {self.bitrate} bps")

        self.mode = Interface.MODE_FULL
        self.online = True
        self._kiss_buf =[]
        self._in_frame = False
        self._escape   = False

    def start_reading(self):
        threading.Thread(target=self._read_loop, daemon=True).start()

    def _read_loop(self):
        while self.online:
            try:
                data = self._socket.read(512)
                if data and len(data) > 0:
                    self._parse_kiss(data)
            except Exception as e:
                self.online = False

    def _deliver(self, pkt):
        if len(pkt) > 0:
            self.rxb += len(pkt)
            try:
                self.owner.inbound(pkt, self)
            except Exception as e:
                pass

    def _parse_kiss(self, data):
        for byte in data:
            if byte == KISS_FEND:
                if self._in_frame and len(self._kiss_buf) > 1:
                    port = self._kiss_buf[0]
                    body = bytes(self._kiss_buf[1:])
                    if port == CMD_DATA and len(body) > 0:
                        self._deliver(body)
                self._kiss_buf =[]
                self._in_frame = True
                self._escape   = False
            elif self._in_frame:
                if byte == KISS_FESC:
                    self._escape = True
                elif self._escape:
                    self._escape = False
                    if byte == KISS_TFEND:
                        self._kiss_buf.append(KISS_FEND)
                    elif byte == KISS_TFESC:
                        self._kiss_buf.append(KISS_FESC)
                else:
                    self._kiss_buf.append(byte)

    def process_outgoing(self, data):
        try:
            self._socket.write(kiss_cmd(CMD_DATA, data))
            self.txb += len(data)
        except Exception as e:
            pass

def _rns_main(bt_socket_wrapper):
    global destination, lxmf_router, reticulum
    try:
        configure_rnode(bt_socket_wrapper)
        
        configdir = "/data/data/com.reticulum.mesh/files"
        os.makedirs(configdir, exist_ok=True)
        
        config_path = os.path.join(configdir, "config")
        with open(config_path, "w") as f:
            f.write("[reticulum]\n")
            f.write("enable_transport = False\n")
            f.write("share_instance = No\n")

        reticulum = RNS.Reticulum(configdir=configdir, loglevel=RNS.LOG_DEBUG)
        
        # Add interface
        iface = AndroidBTInterface(RNS.Transport, "RNodeBT", bt_socket_wrapper)
        RNS.Transport.interfaces.append(iface)
        iface.start_reading()

        identity_path = os.path.join(configdir, "identity")
        if os.path.exists(identity_path):
            identity = RNS.Identity.from_file(identity_path)
        else:
            identity = RNS.Identity()
            identity.to_file(identity_path)

        lxmf_router = LXMF.LXMRouter(storagepath=os.path.join(configdir, "lxmf"))
        LXMF.LXMRouter.DELIVERY_RETRY_WAIT = 30
        
        destination = lxmf_router.register_delivery_identity(identity, display_name="Android Node")
        destination.set_proof_strategy(RNS.Destination.PROVE_ALL)
        
        _start_result["addr"] = RNS.prettyhexrep(destination.hash).strip("<>")
        
    except Exception as e:
        _start_result["error"] = str(e)
    finally:
        _start_done.set()

def start(bt_socket_wrapper):
    global _rns_started
    if _rns_started:
        return _start_result.get("addr")
    _rns_started = True
    _start_done.clear()
    threading.Thread(target=_rns_main, args=(bt_socket_wrapper,), daemon=True).start()
    _start_done.wait()
    return _start_result["addr"]

def announce():
    if destination:
        destination.announce()
        return "Announced!"
    return "Not ready"

def send_image(dest_hash_hex, webp_b64):
    import base64 as _b64
    global lxmf_router, destination

    if not lxmf_router or not destination:
        return "Not connected"

    try:
        dest_hash_hex = dest_hash_hex.strip().strip("<>")
        img_bytes = _b64.b64decode(webp_b64)
        dest_hash = bytes.fromhex(dest_hash_hex)
        
        recalled_identity = RNS.Identity.recall(dest_hash)
        if recalled_identity is None:
            return "Unknown peer — ask them to tap Announce first"

        lxmf_dest = RNS.Destination(recalled_identity, RNS.Destination.OUT, RNS.Destination.SINGLE, "lxmf", "delivery")

        # --- CRITICAL FIX 3: Wait properly for path response ---
        if not RNS.Transport.has_path(lxmf_dest.hash):
            RNS.log("No path known, requesting before send...")
            RNS.Transport.request_path(lxmf_dest.hash)
            
            # Wait up to 15 seconds for the path to arrive over LoRa
            wait_start = time.time()
            while not RNS.Transport.has_path(lxmf_dest.hash) and (time.time() - wait_start) < 15.0:
                time.sleep(0.5)
                
            if not RNS.Transport.has_path(lxmf_dest.hash):
                return "Path request timed out. Peer may be too far away."

        msg = LXMF.LXMessage(lxmf_dest, destination, "", desired_method=LXMF.LXMessage.DIRECT)
        msg.fields = {6: img_bytes}

        lxmf_router.handle_outbound(msg)
        return "Image queued for delivery!"

    except Exception as e:
        return f"Error: {e}"
'@