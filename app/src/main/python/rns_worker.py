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

# Shared state
chat_messages = []
seen_announces = []
known_identities = {}  # hash_hex -> RNS.Identity, populated from announces

# Send queue — ensures half-duplex LoRa only sends one message at a time
_send_queue = []                      # list of (dest_hash_hex, text, result_holder)
_send_lock = threading.Lock()
_send_event = threading.Event()       # signals queue worker there's work to do
_current_send_done = threading.Event()  # signals current message finished
contacts = {}  # hash_hex -> nickname string, persisted to disk

CONTACTS_PATH = "/data/data/com.example.rnshello/files/contacts.json"

def load_contacts():
    global contacts
    try:
        import json
        if os.path.exists(CONTACTS_PATH):
            with open(CONTACTS_PATH, "r") as f:
                contacts = json.load(f)
            RNS.log(f"Loaded {len(contacts)} contacts")
    except Exception as e:
        RNS.log(f"Could not load contacts: {e}")
        contacts = {}

def save_contacts():
    try:
        import json
        with open(CONTACTS_PATH, "w") as f:
            json.dump(contacts, f)
    except Exception as e:
        RNS.log(f"Could not save contacts: {e}")

def set_contact(hash_hex, name):
    global contacts
    hash_hex = hash_hex.strip().replace("<", "").replace(">", "")
    if name.strip():
        contacts[hash_hex] = name.strip()
    else:
        contacts.pop(hash_hex, None)  # empty name = delete contact
    save_contacts()
    return "OK"

def get_contact(hash_hex):
    hash_hex = hash_hex.strip().replace("<", "").replace(">", "")
    return contacts.get(hash_hex, "")

RNS_CONFIG = """
[reticulum]
  enable_transport = True
  share_instance = False
  shared_instance_port = 37428
  instance_control_port = 37429
  panic_on_interface_error = False

[interfaces]

"""

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
RADIO_STATE_ON  = 0x01

def kiss_escape(data):
    out = []
    for b in data:
        if b == KISS_FEND:
            out += [KISS_FESC, KISS_TFEND]
        elif b == KISS_FESC:
            out += [KISS_FESC, KISS_TFESC]
        else:
            out.append(b)
    return bytes(out)

def kiss_cmd(cmd, data=b""):
    return bytes([KISS_FEND, cmd]) + kiss_escape(data) + bytes([KISS_FEND])

def configure_rnode(socket):
    RNS.log("Configuring RNode radio parameters...")
    socket.write(kiss_cmd(CMD_FREQUENCY, struct.pack(">I", 433025000)))
    time.sleep(0.1)
    socket.write(kiss_cmd(CMD_BANDWIDTH, struct.pack(">I", 31250)))
    time.sleep(0.1)
    socket.write(kiss_cmd(CMD_TXPOWER, bytes([17])))
    time.sleep(0.1)
    socket.write(kiss_cmd(CMD_SF, bytes([8])))
    time.sleep(0.1)
    socket.write(kiss_cmd(CMD_CR, bytes([6])))
    time.sleep(0.1)
    socket.write(kiss_cmd(CMD_RADIO_STATE, bytes([RADIO_STATE_ON])))
    time.sleep(0.5)
    RNS.log("RNode radio configured and ON")

class AndroidBTInterface(Interface):
    BITRATE_GUESS = 1200

    def __init__(self, owner, name, socket):
        super().__init__()
        self.owner                  = owner   # RNS Transport instance
        self.name                   = name
        self.rxb                    = 0
        self.txb                    = 0
        self.online                 = False
        self.IN                     = True
        self.OUT                    = True
        self.FWD                    = False
        self.RPT                    = False
        self._socket                = socket
        self.bitrate                = self.BITRATE_GUESS
        self.ingress_control        = False
        self.ic_max_held_announces  = 0
        self.ic_burst_hold_time     = 0
        self.ic_burst_freq_new      = 0
        self.ic_burst_freq          = 0
        self.announce_cap           = 2
        self.announce_queue         = []
        self.held_announces         = {}
        self.announced_identity     = None
        self.mode                   = Interface.MODE_FULL
        self.oa_freq_deque          = deque(maxlen=16)
        self.ifac_size              = None
        self.ifac_netkey            = None
        self.ifac_key               = None
        self.ifac_identity          = None
        self.ifac_signature         = None
        # Attributes required by newer RNS versions
        self.announce_rate_target   = None
        self.announce_rate_grace    = None
        self.announce_rate_penalty  = None
        self.announce_allowed_at    = 0
        self.announce_time          = None
        self.stamp_cost             = None
        self.online                 = True
        self._kiss_buf              = []
        self._in_frame              = False
        self._escape                = False
        threading.Thread(target=self._read_loop, daemon=True).start()

    def _read_loop(self):
        while self.online:
            try:
                data = self._socket.read(512)
                if data and len(data) > 0:
                    self._parse_kiss(data)
            except Exception as e:
                err = str(e)
                # Only kill the loop on actual BT/IO errors, not RNS internal errors
                if "socket" in err.lower() or "bluetooth" in err.lower() or "read ret" in err.lower() or "closed" in err.lower():
                    RNS.log(f"BT read error (stopping): {e}")
                    self.online = False
                else:
                    RNS.log(f"BT read non-fatal error (continuing): {e}")

    def _parse_kiss(self, data):
        for byte in data:
            if byte == KISS_FEND:
                if self._in_frame and len(self._kiss_buf) > 1:
                    if self._kiss_buf[0] == CMD_DATA:
                        pkt = bytes(self._kiss_buf[1:])
                        self.rxb += len(pkt)
                        try:
                            self.owner.inbound(pkt, self)
                        except Exception as e:
                            RNS.log(f"inbound processing error (non-fatal): {e}")
                self._kiss_buf = []
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
            RNS.log(f"BT write error: {e}")

def message_received(message):
    sender = RNS.prettyhexrep(message.source_hash)
    ts = time.strftime("%H:%M:%S")
    try:
        raw = message.content
        if isinstance(raw, bytes):
            text = raw.decode("utf-8")
        else:
            text = message.content_as_string()
    except Exception:
        text = message.content_as_string()
    # Strip null bytes and whitespace that LXMF sometimes adds
    text = text.strip().strip("\x00")
    is_image = text.startswith("IMG:")
    RNS.log(f"MSG RECEIVED from {sender}: type={type(text).__name__} len={len(text)} starts={repr(text[:20])}")
    RNS.log(f"Is image: {is_image}")
    entry = {"from": sender, "text": text, "ts": ts, "direction": "in"}
    chat_messages.append(entry)

def _decode_lxmf_app_data(app_data):
    """LXMF encodes display_name as msgpack {0: "name"} — try that first, fallback to raw utf-8."""
    if not app_data:
        return ""
    # Try msgpack (LXMF standard encoding)
    try:
        import msgpack
        unpacked = msgpack.unpackb(app_data, raw=False)
        if isinstance(unpacked, dict):
            # LXMF uses key 0 for display name
            val = unpacked.get(0, unpacked.get("name", ""))
            return str(val) if val else ""
        if isinstance(unpacked, str):
            return unpacked
        if isinstance(unpacked, bytes):
            return unpacked.decode("utf-8", errors="replace")
    except Exception as e:
        RNS.log(f"msgpack decode failed: {e}, trying utf-8")
    # Fallback: raw UTF-8
    try:
        decoded = app_data.decode("utf-8").strip()
        # Sanity check — if it looks like binary garbage, return empty
        if all(32 <= ord(c) < 127 or c in "\t\n" for c in decoded):
            return decoded
    except Exception:
        pass
    return ""


def announce_received(destination_hash, announced_identity, app_data):
    global known_identities
    # destination_hash may be bytes — normalise to clean hex string
    if isinstance(destination_hash, bytes):
        hash_str = destination_hash.hex()
    else:
        hash_str = str(destination_hash).replace("<","").replace(">","")

    name = _decode_lxmf_app_data(app_data)
    ts = time.strftime("%H:%M:%S")
    RNS.log(f"ANNOUNCE from {hash_str} name={repr(name)} app_data={repr(app_data[:32] if app_data else None)}")

    if announced_identity is not None:
        known_identities[hash_str] = announced_identity
        RNS.log(f"Identity stored for {hash_str}")

    entry = {"hash": hash_str, "name": name, "ts": ts}
    for i, a in enumerate(seen_announces):
        if a["hash"] == hash_str:
            seen_announces[i] = entry
            RNS.log(f"Updated existing announce for {hash_str}")
            return
    seen_announces.append(entry)
    RNS.log(f"New announce added, total={len(seen_announces)}")


# Keep a module-level reference so it isn't garbage collected
_announce_handler_instance = None

class AnnounceHandler:
    aspect_filter = "lxmf.delivery"

    def received_announce(self, destination_hash, announced_identity, app_data):
        announce_received(destination_hash, announced_identity, app_data)

def _noop_signal(sig, handler):
    pass

def _rns_main(bt_socket_wrapper):
    global destination, lxmf_router, reticulum
    try:
        configure_rnode(bt_socket_wrapper)

        configdir = "/data/data/com.example.rnshello/files/.reticulum"
        os.makedirs(configdir, exist_ok=True)
        with open(os.path.join(configdir, "config"), "w") as f:
            f.write(RNS_CONFIG)

        original_signal = signal.signal
        signal.signal = _noop_signal

        load_contacts()
        reticulum = RNS.Reticulum(configdir=configdir, loglevel=RNS.LOG_DEBUG)

        iface = AndroidBTInterface(RNS.Transport, "RNodeBT", bt_socket_wrapper)
        RNS.Transport.interfaces.append(iface)

        identity_path = "/data/data/com.example.rnshello/files/identity"
        identity = None
        if os.path.exists(identity_path):
            try:
                identity = RNS.Identity.from_file(identity_path)
                if identity is not None:
                    RNS.log(f"Loaded existing identity: {RNS.prettyhexrep(identity.hash)}")
                else:
                    RNS.log("from_file returned None, will recreate")
            except Exception as e:
                RNS.log(f"Failed to load identity: {e}, will recreate")
                identity = None
        if identity is None:
            identity = RNS.Identity()
            try:
                identity.to_file(identity_path)
                RNS.log(f"Created and saved new identity: {RNS.prettyhexrep(identity.hash)}")
            except Exception as e:
                RNS.log(f"WARNING: Could not save identity to file: {e}")
                RNS.log("Address will change on next restart!")

        lxmf_router = LXMF.LXMRouter(
            storagepath="/data/data/com.example.rnshello/files/lxmf",
            autopeer=True
        )

        signal.signal = original_signal

        destination = lxmf_router.register_delivery_identity(
            identity,
            display_name="RNS Hello Android"
        )
        lxmf_router.register_delivery_callback(message_received)
        global _announce_handler_instance
        _announce_handler_instance = AnnounceHandler()
        RNS.Transport.register_announce_handler(_announce_handler_instance)

        # Start the send queue worker
        threading.Thread(target=_queue_worker, daemon=True).start()

        destination.announce()

        addr = RNS.prettyhexrep(destination.hash)
        RNS.log(f"LXMF address announced: {addr}")
        _start_result["addr"] = addr

    except Exception as e:
        import traceback
        RNS.log(f"RNS start error: {e}\n{traceback.format_exc()}")
        _start_result["error"] = str(e)
    finally:
        _start_done.set()

def start(bt_socket_wrapper):
    global _rns_started
    if _rns_started:
        if destination:
            return RNS.prettyhexrep(destination.hash)
        return "Error: already started but no address"
    _rns_started = True
    _start_done.clear()
    _start_result["addr"] = None
    _start_result["error"] = None
    threading.Thread(target=_rns_main, args=(bt_socket_wrapper,), daemon=True).start()
    _start_done.wait(timeout=30)
    if _start_result["error"]:
        return f"Error: {_start_result['error']}"
    return _start_result["addr"] or "Timeout"

def _do_send(dest_hash_hex, text):
    """Actually send one message. Called only from queue worker thread."""
    global lxmf_router, destination, known_identities
    try:
        dest_hash = bytes.fromhex(dest_hash_hex)
        RNS.log(f"[Queue] Sending to {dest_hash_hex}: {text[:40]}")

        # Get identity
        recalled_identity = known_identities.get(dest_hash_hex)
        if recalled_identity is None:
            recalled_identity = RNS.Identity.recall(dest_hash)
        if recalled_identity is None:
            RNS.log("No identity, requesting path...")
            RNS.Transport.request_path(dest_hash)
            for i in range(15):
                time.sleep(2)
                recalled_identity = known_identities.get(dest_hash_hex)
                if recalled_identity is None:
                    recalled_identity = RNS.Identity.recall(dest_hash)
                if recalled_identity is not None:
                    RNS.log(f"Got identity after {(i+1)*2}s")
                    break

        if recalled_identity is None:
            return "No identity known. Have they announced recently?"

        lxmf_dest = RNS.Destination(
            recalled_identity,
            RNS.Destination.OUT,
            RNS.Destination.SINGLE,
            "lxmf",
            "delivery"
        )

        method = LXMF.LXMessage.DIRECT
        if text.startswith("IMG:"):
            msg = LXMF.LXMessage(lxmf_dest, destination, text.encode("utf-8"), title="", desired_method=method)
        else:
            msg = LXMF.LXMessage(lxmf_dest, destination, text, title="", desired_method=method)

        # Signal done when delivered OR failed so queue can proceed
        def on_delivered(m):
            RNS.log(f"[Queue] Delivered! state={m.state}")
            _current_send_done.set()

        def on_failed(m):
            RNS.log(f"[Queue] Failed! state={m.state}")
            _current_send_done.set()

        msg.register_delivery_callback(on_delivered)
        msg.register_failed_callback(on_failed)

        _current_send_done.clear()
        lxmf_router.handle_outbound(msg)

        # Wait up to 120s for delivery/failure before allowing next send
        delivered = _current_send_done.wait(timeout=120)
        if not delivered:
            RNS.log("[Queue] Timed out waiting for delivery confirmation")

        ts = time.strftime("%H:%M:%S")
        chat_messages.append({"from": "me", "to": dest_hash_hex, "text": text, "ts": ts, "direction": "out"})
        return "Sent!"

    except Exception as e:
        import traceback
        RNS.log(f"[Queue] send error: {traceback.format_exc()}")
        _current_send_done.set()  # unblock queue on error
        return f"Error: {e}"


def _queue_worker():
    """Single worker thread — processes one outbound message at a time."""
    while True:
        _send_event.wait()
        _send_event.clear()
        while True:
            with _send_lock:
                if not _send_queue:
                    break
                dest, text, holder = _send_queue.pop(0)
            result = _do_send(dest, text)
            holder["result"] = result
            holder["done"].set()


def send_message(dest_hash_hex, text):
    if not lxmf_router or not destination:
        return "Not connected"
    dest_hash_hex = dest_hash_hex.strip()
    holder = {"result": None, "done": threading.Event()}
    with _send_lock:
        _send_queue.append((dest_hash_hex, text, holder))
    queue_pos = len(_send_queue)
    _send_event.set()
    if queue_pos > 1:
        RNS.log(f"[Queue] Message queued at position {queue_pos}")
    # Wait for this message's turn and completion (max 180s total)
    holder["done"].wait(timeout=180)
    return holder["result"] or "Timeout"

def get_messages():
    result = []
    for m in chat_messages:
        entry = dict(m)
        h = entry.get("from", "").replace("<", "").replace(">", "")
        nickname = contacts.get(h, "")
        if nickname:
            entry["display_from"] = nickname
        else:
            entry["display_from"] = entry.get("from", "")
        result.append(entry)
    return result

def get_announces():
    result = []
    for a in seen_announces:
        entry = dict(a)
        h = entry.get("hash", "").replace("<", "").replace(">", "")
        nickname = contacts.get(h, "")
        if nickname:
            entry["display"] = nickname
        else:
            entry["display"] = entry.get("name", "")
        result.append(entry)
    return result

def get_address():
    global destination
    return RNS.prettyhexrep(destination.hash) if destination else "Not initialized"
