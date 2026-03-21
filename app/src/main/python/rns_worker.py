import RNS
import LXMF
import threading
import signal
import os
import time
import struct
from RNS.Interfaces.Interface import Interface
from collections import deque

# ── Patch LoRa timeouts at module import — MUST be before any RNS init ───────
# ESTABLISHMENT_TIMEOUT_PER_HOP default is 5s. At LoRa 1200 baud a single
# packet takes ~0.8s to TX. Round-trip for link handshake = 6-12s minimum.
# Patch here so it applies to every Link object created anywhere in this process.
def _patch_rns_for_lora():
    patched = []
    for _attr in ["ESTABLISHMENT_TIMEOUT_PER_HOP", "LINK_ESTABLISHMENT_TIMEOUT",
                  "establishment_timeout_per_hop", "TIMEOUT_PER_HOP"]:
        try:
            old = getattr(RNS.Link, _attr, None)
            if old is not None:
                setattr(RNS.Link, _attr, 60.0)
                patched.append(f"RNS.Link.{_attr}: {old}→60.0")
        except Exception:
            pass
    # Also patch keepalive — default fires too quickly during Resource transfer
    for _attr in ["KEEPALIVE_TIMEOUT_FACTOR", "KEEPALIVE", "keepalive"]:
        try:
            old = getattr(RNS.Link, _attr, None)
            if old is not None:
                setattr(RNS.Link, _attr, 360)   # 6 minutes
                patched.append(f"RNS.Link.{_attr}: {old}→360")
        except Exception:
            pass
    if patched:
        RNS.log("LoRa patches applied: " + ", ".join(patched))
    else:
        # Log all Link attrs so we know what's available
        attrs = [f"{a}={getattr(RNS.Link,a)}" for a in dir(RNS.Link)
                 if not a.startswith("_") and isinstance(getattr(RNS.Link,a,None),(int,float))]
        RNS.log("WARNING: No timeout attrs patched. RNS.Link numeric attrs: " + str(attrs))

_patch_rns_for_lora()
# ─────────────────────────────────────────────────────────────────────────────

destination       = None   # LXMF delivery destination
image_destination = None   # Raw RNS destination for Resource-based image transfer
lxmf_router = None
reticulum    = None
_rns_started = False
_start_done  = threading.Event()
_start_result = {"addr": None, "error": None}

# Thread-safe shared state
_data_lock    = threading.Lock()
chat_messages = deque(maxlen=500)   # FIX: was unbounded list, now capped
seen_announces = []
known_identities  = {}  # plain hex (no <>) -> RNS.Identity
active_links      = {}  # plain hex -> RNS.Link (most recent active link per peer)
image_peer_hashes = {}  # lxmf_hash -> rnshello.image destination hash

RNS_CONFIG = """
[reticulum]
  enable_transport = True
  share_instance = True
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
CMD_DETECT      = 0x08
CMD_READY       = 0x0F
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
    import rnode_config as _rc
    cfg = _rc.get()
    freq  = cfg["frequency"]
    bw    = cfg["bandwidth"]
    txpwr = cfg["txpower"]
    sf    = cfg["sf"]
    cr    = cfg["cr"]
    RNS.log(f"Configuring RNode: freq={freq} bw={bw} tx={txpwr} sf={sf} cr={cr}")
    # 1. Detect / wake RNode
    socket.write(kiss_cmd(CMD_DETECT, bytes([0x00])))
    time.sleep(0.3)
    # 2. Radio OFF — clean slate
    socket.write(kiss_cmd(CMD_RADIO_STATE, bytes([0x00])))
    time.sleep(0.8)
    # 3. Set params from saved config
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
    # 4. Radio ON — starts RX immediately
    socket.write(kiss_cmd(CMD_RADIO_STATE, bytes([RADIO_STATE_ON])))
    time.sleep(1.5)
    # 5. Signal ready
    socket.write(kiss_cmd(CMD_READY, bytes([0x00])))
    time.sleep(0.2)
    RNS.log("RNode radio configured and ON")

class AndroidBTInterface(Interface):
    BITRATE_GUESS = 1200

    def __init__(self, owner, name, socket):
        super().__init__()
        self.owner                  = owner
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
        # Required by RNS Transport
        self.announce_rate_target   = None
        self.announce_rate_grace    = None
        self.announce_rate_penalty  = None
        self.announce_allowed_at    = 0.0
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
                RNS.log(f"BT read error: {e}")
                self.online = False

    def _parse_kiss(self, data):
        for byte in data:
            if byte == KISS_FEND:
                if self._in_frame and len(self._kiss_buf) > 1:
                    pkt = bytes(self._kiss_buf[1:])
                    if len(pkt) > 0:
                        self.rxb += len(pkt)
                        port = self._kiss_buf[0]
                        RNS.log(f"RX KISS port=0x{port:02x} len={len(pkt)}")
                        if port == CMD_DATA:
                            try:
                                self.owner.inbound(pkt, self)
                            except Exception as e:
                                RNS.log(f"inbound error: {e}")
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
    import base64 as _b64
    sender = RNS.prettyhexrep(message.source_hash).strip("<>")
    ts = time.strftime("%H:%M:%S")

    # ── Check for image field first (Sideband-compatible) ────────────────────
    # 'ia' is the standard image field key in LXMF
    # Format: [format_string, raw_bytes]  e.g. ["jpg", b"..."]
    try:
        fields = message.fields or {}
        if "ia" in fields:
            image_field = fields["ia"]
            img_fmt   = image_field[0]   # e.g. "jpg", "webp", "png"
            img_bytes = image_field[1]
            if isinstance(img_bytes, (bytes, bytearray)) and len(img_bytes) > 0:
                b64 = _b64.b64encode(bytes(img_bytes)).decode("ascii")
                RNS.log(f"IMG RECEIVED from {sender}: {img_fmt} ({len(img_bytes)} bytes)")
                with _data_lock:
                    chat_messages.append({
                        "from": sender,
                        "text": f"IMG_B64:{b64}",
                        "ts": ts,
                        "direction": "in"
                    })
                return
    except Exception as e:
        RNS.log(f"Image field parse error: {e}")

    # ── Regular text message ───────────────────────────────────────────────────
    text = ""
    try:
        text = message.content_as_string()
    except:
        pass
    if not text:
        try:
            raw = message.content
            if isinstance(raw, bytes):
                text = raw.decode("utf-8", errors="replace")
            elif raw:
                text = str(raw)
        except:
            pass
    if not text:
        try:
            text = message.title_as_string() or ""
        except:
            pass
    RNS.log(f"MSG RECEIVED from {sender}: '{text}' (fields={message.fields})")
    with _data_lock:
        chat_messages.append({"from": sender, "text": text or "(empty)", "ts": ts, "direction": "in"})

def _msgpack_decode_first(data):
    """
    Pure-Python minimal msgpack decoder — no external library needed.
    Decodes only the first value from data, returns (value, bytes_consumed).
    Handles the types actually used in LXMF app_data:
      fixarray, bin8, bin16, str8, fixstr, nil, bool, int types.

    Sideband app_data format: fixarray[2] = [name_bytes, nil]
      b'\\x92\\xc4\\x0eAnonymous Peer\\xc0'
      \\x92       fixarray len 2
      \\xc4\\x0e  bin8, 14 bytes
      ...name...
      \\xc0       nil
    """
    if not data:
        raise ValueError("empty")
    b = data[0]
    # nil
    if b == 0xc0:
        return (None, 1)
    # bool
    if b == 0xc2:
        return (False, 1)
    if b == 0xc3:
        return (True, 1)
    # positive fixint
    if b <= 0x7f:
        return (b, 1)
    # fixstr (0xa0-0xbf)
    if 0xa0 <= b <= 0xbf:
        n = b & 0x1f
        return (data[1:1+n].decode("utf-8", errors="replace"), 1+n)
    # fixarray (0x90-0x9f)
    if 0x90 <= b <= 0x9f:
        count = b & 0x0f
        items = []
        pos = 1
        for _ in range(count):
            val, consumed = _msgpack_decode_first(data[pos:])
            items.append(val)
            pos += consumed
        return (items, pos)
    # bin8 (0xc4)
    if b == 0xc4:
        n = data[1]
        return (data[2:2+n], 2+n)
    # bin16 (0xc5)
    if b == 0xc5:
        n = (data[1] << 8) | data[2]
        return (data[3:3+n], 3+n)
    # str8 (0xd9)
    if b == 0xd9:
        n = data[1]
        return (data[2:2+n].decode("utf-8", errors="replace"), 2+n)
    # str16 (0xda)
    if b == 0xda:
        n = (data[1] << 8) | data[2]
        return (data[3:3+n].decode("utf-8", errors="replace"), 3+n)
    # uint8 (0xcc)
    if b == 0xcc:
        return (data[1], 2)
    # uint16 (0xcd)
    if b == 0xcd:
        return ((data[1] << 8) | data[2], 3)
    raise ValueError(f"Unsupported msgpack byte 0x{b:02x}")

def _decode_lxmf_app_data(app_data):
    """
    Decode LXMF announce app_data.
    Sideband encodes it as msgpack fixarray[name_bytes, nil].
    Uses pure Python decoder — no external library required.
    Falls back to plain UTF-8 if not valid msgpack.
    """
    if not app_data:
        return ""
    try:
        decoded, _ = _msgpack_decode_first(app_data)
        if isinstance(decoded, list) and len(decoded) >= 1:
            name_part = decoded[0]
            if isinstance(name_part, bytes):
                return name_part.decode("utf-8", errors="replace")
            elif isinstance(name_part, str):
                return name_part
            elif name_part is not None:
                return str(name_part)
    except Exception:
        pass
    # Fall back to plain UTF-8
    try:
        return app_data.decode("utf-8", errors="replace")
    except Exception:
        return str(app_data)

def announce_received(destination_hash, announced_identity, app_data):
    # Always store with plain hex key (no <> brackets)
    hash_str = RNS.prettyhexrep(destination_hash).strip("<>")

    # FIX: use msgpack-aware decoder to match Sideband's format
    name = _decode_lxmf_app_data(app_data)

    ts = time.strftime("%H:%M:%S")
    RNS.log(f"ANNOUNCE from {hash_str} name={name!r}")
    if announced_identity is not None:
        with _data_lock:
            known_identities[hash_str] = announced_identity
        RNS.log(f"Identity stored for {hash_str}")
    entry = {"hash": hash_str, "name": name, "ts": ts}
    with _data_lock:
        for i, a in enumerate(seen_announces):
            if a["hash"] == hash_str:
                seen_announces[i] = entry
                return
        seen_announces.append(entry)

class AnnounceHandler:
    aspect_filter = "lxmf.delivery"

    def received_announce(self, destination_hash, announced_identity, app_data):
        RNS.log(f"*** ANNOUNCE HANDLER FIRED: {RNS.prettyhexrep(destination_hash)}")
        announce_received(destination_hash, announced_identity, app_data)

class RawAnnounceHandler:
    """Catches ALL announces regardless of aspect — for debugging"""
    aspect_filter = None

    def received_announce(self, destination_hash, announced_identity, app_data):
        RNS.log(f"*** RAW ANNOUNCE: {RNS.prettyhexrep(destination_hash)} app_data={app_data}")

class ImageAnnounceHandler:
    """
    Listens for rnshello.image announces from peers.
    Maps their LXMF hash → their image destination hash so send_image
    can open a link to the correct destination.
    """
    aspect_filter = "rnshello.image"

    def received_announce(self, destination_hash, announced_identity, app_data):
        img_hash = RNS.prettyhexrep(destination_hash).strip("<>")
        RNS.log(f"*** IMAGE ANNOUNCE: {img_hash}")
        if announced_identity is not None:
            # The identity hash is the same as their LXMF identity hash
            # (both use the same RNS.Identity). Store the mapping.
            # We compute the LXMF delivery hash from this identity.
            try:
                lxmf_dest = RNS.Destination(
                    announced_identity,
                    RNS.Destination.OUT,
                    RNS.Destination.SINGLE,
                    "lxmf",
                    "delivery"
                )
                lxmf_hash = RNS.prettyhexrep(lxmf_dest.hash).strip("<>")
                with _data_lock:
                    image_peer_hashes[lxmf_hash] = img_hash
                    known_identities[lxmf_hash]  = announced_identity
                RNS.log(f"Image hash mapped: lxmf={lxmf_hash} → img={img_hash}")
            except Exception as e:
                RNS.log(f"ImageAnnounceHandler error: {e}")

def incoming_link_established(link):
    """
    Called when a remote peer opens a link to our LXMF destination.
    Used by LXMRouter internally — we just log it.
    """
    peer_hash = None
    try:
        peer_hash = RNS.prettyhexrep(link.destination.hash).strip("<>")
    except Exception:
        pass
    RNS.log(f"LXMF incoming link from {peer_hash}")

def image_link_established(link):
    """
    Called when a remote peer opens a link to our IMAGE destination.
    Set up Resource receive callbacks on this link.
    """
    import base64 as _b64

    RNS.log(f"Image link established: {link}")

    def resource_started(resource):
        RNS.log(f"Image resource incoming: size={resource.total_size} bytes")
        return True  # accept

    def resource_concluded(resource):
        if resource.status == RNS.Resource.COMPLETE:
            try:
                img_bytes = resource.data.read() if hasattr(resource.data, 'read') else bytes(resource.data)
                b64 = _b64.b64encode(img_bytes).decode("ascii")
                # Determine sender from the link's remote identity
                try:
                    sender_hash = RNS.prettyhexrep(link.get_remote_identity().hash).strip("<>")
                except Exception:
                    sender_hash = "unknown"
                ts = time.strftime("%H:%M:%S")
                kb = len(img_bytes) / 1024
                RNS.log(f"Image resource COMPLETE from {sender_hash}: {kb:.1f} KB")
                with _data_lock:
                    chat_messages.append({
                        "from": sender_hash,
                        "text": f"IMG_B64:{b64}",
                        "ts": ts,
                        "direction": "in"
                    })
            except Exception as e:
                import traceback
                RNS.log(f"Image resource decode error: {traceback.format_exc()}")
        else:
            RNS.log(f"Image resource failed/incomplete: status={resource.status}")

    # Keep the link alive during the slow incoming Resource transfer
    try:
        link.set_keepalive(120)
        RNS.log("Incoming image link keepalive set to 120s")
    except Exception as ke:
        RNS.log(f"set_keepalive not available: {ke}")
    link.set_resource_started_callback(resource_started)
    link.set_resource_concluded_callback(resource_concluded)
    link.set_link_closed_callback(lambda lnk: RNS.log("Image link closed"))

def _noop_signal(sig, handler):
    pass

def _startup_announce_loop():
    """
    FIX: Sideband re-announces periodically so peers that come online later
    can discover it. A single announce at startup is often missed if the other
    phone's RNode isn't fully ready yet.

    Schedule:
      +15s  — catch phones that were slow to connect
      +60s  — catch phones that connected after initial announce
      then every 10 min forever
    """
    for delay in [15, 60]:
        time.sleep(delay)
        if destination:
            try:
                destination.announce()
                RNS.log(f"Startup re-announce at +{delay}s")
            except Exception as e:
                RNS.log(f"Re-announce error at +{delay}s: {e}")

    while True:
        time.sleep(600)  # 10 minutes
        if destination:
            try:
                destination.announce()
                RNS.log("Periodic re-announce sent (10 min)")
            except Exception as e:
                RNS.log(f"Periodic re-announce error: {e}")

def _rns_main(bt_socket_wrapper):
    global destination, lxmf_router, reticulum
    try:
        configure_rnode(bt_socket_wrapper)

        configdir = "/data/data/com.example.rnshello/files/.reticulum"
        os.makedirs(configdir, exist_ok=True)
        with open(os.path.join(configdir, "config"), "w") as f:
            f.write(RNS_CONFIG)

        # Suppress signal() calls — we're on a background thread, not main
        original_signal = signal.signal
        signal.signal = _noop_signal

        # FIX: init Reticulum FIRST, then attach interface
        # Previously the interface was appended before RNS.Reticulum() was
        # called, which risked Transport reinitialising and orphaning the
        # interface so inbound packets went nowhere.
        reticulum = RNS.Reticulum(configdir=configdir, loglevel=RNS.LOG_DEBUG)

        iface = AndroidBTInterface(RNS.Transport, "RNodeBT", bt_socket_wrapper)
        RNS.Transport.interfaces.append(iface)
        RNS.log(f"AndroidBTInterface attached. Transport interfaces: {[i.name for i in RNS.Transport.interfaces]}")

        files_dir = "/data/data/com.example.rnshello/files"
        os.makedirs(files_dir, exist_ok=True)
        identity_path = os.path.join(files_dir, "identity")
        identity = None
        if os.path.exists(identity_path):
            try:
                identity = RNS.Identity.from_file(identity_path)
                if identity is not None:
                    RNS.log(f"Loaded existing identity: {RNS.prettyhexrep(identity.hash)}")
                else:
                    RNS.log("Identity file corrupt, recreating")
            except Exception as ie:
                RNS.log(f"Identity load error: {ie}, recreating")
                identity = None
        if identity is None:
            identity = RNS.Identity()
            try:
                identity.to_file(identity_path)
                RNS.log(f"Saved new identity: {RNS.prettyhexrep(identity.hash)}")
            except Exception as se:
                RNS.log(f"Identity save error: {se}")

        # LXMRouter also calls signal.signal internally — keep noop active through init
        lxmf_router = LXMF.LXMRouter(
            storagepath="/data/data/com.example.rnshello/files/lxmf",
            autopeer=True
        )
        signal.signal = original_signal
        # MAX_DELIVERY_ATTEMPTS — increase for LoRa reliability
        try:
            LXMF.LXMRouter.MAX_DELIVERY_ATTEMPTS = 20
            RNS.log("Patched LXMF MAX_DELIVERY_ATTEMPTS=20")
        except Exception as e:
            RNS.log(f"Could not patch MAX_DELIVERY_ATTEMPTS: {e}")

        destination = lxmf_router.register_delivery_identity(
            identity,
            display_name="RNS Hello Android"
        )
        destination.set_proof_strategy(RNS.Destination.PROVE_ALL)
        destination.set_link_established_callback(incoming_link_established)
        lxmf_router.register_delivery_callback(message_received)

        # ── Image destination — raw RNS, Resource-based transfer ──────────────
        # Uses aspect "rnshello.image" so both sides can address it directly.
        # A separate destination means we don't interfere with LXMF at all.
        global image_destination
        image_destination = RNS.Destination(
            identity,
            RNS.Destination.IN,
            RNS.Destination.SINGLE,
            "rnshello",
            "image"
        )
        image_destination.set_proof_strategy(RNS.Destination.PROVE_ALL)
        image_destination.set_link_established_callback(image_link_established)
        image_destination.announce()
        img_addr = RNS.prettyhexrep(image_destination.hash).strip("<>")
        RNS.log(f"Image destination ready: {img_addr}")

        RNS.Transport.register_announce_handler(AnnounceHandler())
        RNS.Transport.register_announce_handler(ImageAnnounceHandler())
        RNS.Transport.register_announce_handler(RawAnnounceHandler())

        # Initial announce
        destination.announce()
        addr = RNS.prettyhexrep(destination.hash).strip("<>")
        RNS.log(f"LXMF address announced: {addr}")
        _start_result["addr"] = addr

        # FIX: start periodic re-announce loop (daemon thread, won't block shutdown)
        threading.Thread(target=_startup_announce_loop, daemon=True).start()

    except Exception as e:
        import traceback
        RNS.log(f"RNS start error: {e}\n{traceback.format_exc()}")
        _start_result["error"] = str(e)
    finally:
        _start_done.set()

def start(bt_socket_wrapper):
    global _rns_started
    if _rns_started:
        _start_done.wait(timeout=30)
        if destination:
            return RNS.prettyhexrep(destination.hash).strip("<>")
        return _start_result.get("error") or "Timeout"
    _rns_started = True
    _start_done.clear()
    _start_result["addr"] = None
    _start_result["error"] = None
    threading.Thread(target=_rns_main, args=(bt_socket_wrapper,), daemon=True).start()
    _start_done.wait(timeout=30)
    if _start_result["error"]:
        return f"Error: {_start_result['error']}"
    return _start_result["addr"] or "Timeout"

def announce():
    try:
        if destination:
            destination.announce()
            addr = RNS.prettyhexrep(destination.hash).strip("<>")
            RNS.log(f"Manual announce sent: {addr}")
            return f"Announced! {addr}"
        return "Not ready yet"
    except Exception as e:
        return f"Error: {e}"

def send_message(dest_hash_hex, text):
    global lxmf_router, destination, known_identities
    if not lxmf_router or not destination:
        return "Not connected"
    try:
        # Normalise — always plain hex, no brackets
        dest_hash_hex = dest_hash_hex.strip().strip("<>")
        dest_hash = bytes.fromhex(dest_hash_hex)
        RNS.log(f"Sending to {dest_hash_hex}: {text}")

        # Get identity — cache first, then RNS recall
        with _data_lock:
            recalled_identity = known_identities.get(dest_hash_hex)
        if recalled_identity is None:
            recalled_identity = RNS.Identity.recall(dest_hash)
            RNS.log(f"Identity recall result: {recalled_identity}")
        else:
            RNS.log(f"Using cached identity for {dest_hash_hex}")

        if recalled_identity is None:
            RNS.Transport.request_path(dest_hash)
            return "Unknown destination — ask them to tap Announce first"

        # Build destination — hash MUST equal dest_hash_hex
        # lxmf.delivery aspect produces the correct LXMF address hash
        lxmf_dest = RNS.Destination(
            recalled_identity,
            RNS.Destination.OUT,
            RNS.Destination.SINGLE,
            "lxmf",
            "delivery"
        )
        actual_hash = RNS.prettyhexrep(lxmf_dest.hash).strip("<>")
        RNS.log(f"Built dest hash: {actual_hash}, target: {dest_hash_hex}")

        # Verify hash matches — if not, the identity is wrong
        if actual_hash != dest_hash_hex:
            RNS.log(f"HASH MISMATCH! Built {actual_hash} but want {dest_hash_hex}")
            return f"Hash mismatch: got {actual_hash}, expected {dest_hash_hex}. Try re-scanning their address."

        # Request path before sending — helps on LoRa single-hop
        if not RNS.Transport.has_path(lxmf_dest.hash):
            RNS.log("No path known, requesting before send...")
            RNS.Transport.request_path(lxmf_dest.hash)
            time.sleep(1.0)  # brief wait for path response

        msg = LXMF.LXMessage(
            lxmf_dest,
            destination,
            text,
            title="",
            desired_method=LXMF.LXMessage.OPPORTUNISTIC
        )
        msg.register_delivery_callback(lambda m: RNS.log(f"Delivered! state={m.state}"))
        msg.register_failed_callback(lambda m: RNS.log(f"Failed! state={m.state}"))
        lxmf_router.handle_outbound(msg)

        ts = time.strftime("%H:%M:%S")
        with _data_lock:
            chat_messages.append({"from": "me", "text": text, "ts": ts, "direction": "out"})
        return "Sent!"

    except Exception as e:
        import traceback
        RNS.log(f"send_message error: {traceback.format_exc()}")
        return f"Error: {e}"

def send_image(dest_hash_hex, webp_b64):
    """
    Send an image using RNS.Resource over a direct RNS.Link.
    Bypasses LXMF entirely — Resource handles sequencing, retransmit,
    integrity verification and reassembly natively. This is the correct
    RNS mechanism for transferring arbitrary binary data over a Link.

    Flow:
      1. Recall peer's identity from the announce cache
      2. Build outgoing "rnshello.image" destination (same aspect the
         receiver registered — this is how both sides find each other)
      3. Open RNS.Link to that destination
      4. On link ACTIVE: send RNS.Resource(img_bytes, link)
      5. On resource delivered: add to chat, close link
      6. On link/resource failure: log and return error via status dict
    """
    import base64 as _b64
    global image_destination, known_identities

    if not image_destination:
        return "Not connected"

    try:
        dest_hash_hex = dest_hash_hex.strip().strip("<>")
        img_bytes = _b64.b64decode(webp_b64)
        kb = len(img_bytes) / 1024
        RNS.log(f"send_image: {kb:.1f} KB to {dest_hash_hex}")

        # Look up peer's image destination hash from announce cache
        with _data_lock:
            img_dest_hex = image_peer_hashes.get(dest_hash_hex)
            recalled_identity = known_identities.get(dest_hash_hex)

        if img_dest_hex is None:
            # Never received their rnshello.image announce yet
            # Request path and ask user to wait for announce
            RNS.Transport.request_path(bytes.fromhex(dest_hash_hex))
            return ("Image destination unknown — wait for peer to announce "
                    "or ask them to tap Announce first")

        if recalled_identity is None:
            recalled_identity = RNS.Identity.recall(bytes.fromhex(dest_hash_hex))
        if recalled_identity is None:
            return "Unknown peer identity — ask them to tap Announce first"

        # Build outgoing destination using the exact hash we received from their announce
        img_dest = RNS.Destination(
            recalled_identity,
            RNS.Destination.OUT,
            RNS.Destination.SINGLE,
            "rnshello",
            "image"
        )
        actual_img_hash = RNS.prettyhexrep(img_dest.hash).strip("<>")
        RNS.log(f"Image dest hash: {actual_img_hash} (expected: {img_dest_hex})")
        if actual_img_hash != img_dest_hex:
            # Hash mismatch — use the raw known hash directly
            RNS.log(f"Hash mismatch — using raw hash {img_dest_hex}")
            img_dest_bytes = bytes.fromhex(img_dest_hex)
            img_dest = RNS.Destination.recall(img_dest_bytes) if hasattr(RNS.Destination, 'recall') else img_dest

        # Shared result dict for the async link/resource callbacks
        result = {"done": threading.Event(), "status": "pending"}

        def link_established(link):
            RNS.log(f"Image link ACTIVE, sending Resource ({kb:.1f} KB)...")
            try:
                # ── Keep the link alive during the slow Resource transfer ──────
                # Default keepalive is too short for LoRa at 1200 baud.
                # Set to 120s so the link watchdog doesn't fire mid-transfer.
                try:
                    link.set_keepalive(120)
                    RNS.log("Link keepalive set to 120s")
                except Exception as ke:
                    RNS.log(f"set_keepalive not available: {ke}")

                def resource_concluded(resource):
                    if resource.status == RNS.Resource.COMPLETE:
                        RNS.log("Image Resource DELIVERED")
                        ts = time.strftime("%H:%M:%S")
                        with _data_lock:
                            chat_messages.append({
                                "from": "me",
                                "text": f"IMG_B64:{webp_b64}",
                                "ts": ts,
                                "direction": "out"
                            })
                        result["status"] = "ok"
                    else:
                        RNS.log(f"Image Resource failed: status={resource.status}")
                        result["status"] = f"Resource failed (status={resource.status})"
                    link.teardown()
                    result["done"].set()

                resource = RNS.Resource(img_bytes, link, callback=resource_concluded)
                RNS.log(f"Resource queued, segments={getattr(resource, 'total_parts', '?')}")
            except Exception as e:
                import traceback
                RNS.log(f"Resource send error: {traceback.format_exc()}")
                result["status"] = f"Error: {e}"
                link.teardown()
                result["done"].set()

        def link_closed(link):
            if not result["done"].is_set():
                RNS.log("Image link closed before Resource completed")
                result["status"] = "Link closed before delivery"
                result["done"].set()

        # Open the link — callbacks fire on the RNS thread
        link = RNS.Link(img_dest)
        link.set_link_established_callback(link_established)
        link.set_link_closed_callback(link_closed)
        RNS.log("Image link opening...")

        # Wait up to 180s — link open + Resource transfer at LoRa speeds
        result["done"].wait(timeout=180)

        if result["status"] == "ok":
            return f"Image sent ({kb:.1f} KB)"
        elif result["status"] == "pending":
            return "Timed out waiting for image delivery"
        else:
            return result["status"]

    except Exception as e:
        import traceback
        RNS.log(f"send_image error: {traceback.format_exc()}")
        return f"Error: {e}"


def get_messages():
    with _data_lock:
        return list(chat_messages)

def get_announces():
    with _data_lock:
        return list(seen_announces)

def get_address():
    global destination
    if destination:
        return RNS.prettyhexrep(destination.hash).strip("<>")
    return "Not initialized"

# ── Contacts — thin delegation to contacts.py ─────────────────────────────────
# RNS layer never uses these. Only the UI layer calls them via RNSBridge.

import contacts as _contacts_mod

def save_contact(hash_hex: str, name: str) -> str:
    try:
        _contacts_mod.save(hash_hex, name)
        return "OK"
    except Exception as e:
        return f"Error: {e}"

def delete_contact(hash_hex: str) -> str:
    try:
        _contacts_mod.delete(hash_hex)
        return "OK"
    except Exception as e:
        return f"Error: {e}"

def get_contacts() -> list:
    return _contacts_mod.get_all()

def resolve_name(hash_hex: str, fallback: str = "") -> str:
    return _contacts_mod.resolve(hash_hex, fallback)

# ── RNode config — bridge functions ───────────────────────────────────────────

import rnode_config as _rnode_cfg_mod

def get_rnode_config() -> dict:
    return _rnode_cfg_mod.get()

def save_rnode_config(frequency: int, bandwidth: int, txpower: int, sf: int, cr: int) -> str:
    return _rnode_cfg_mod.save(
        int(frequency), int(bandwidth), int(txpower), int(sf), int(cr)
    )
