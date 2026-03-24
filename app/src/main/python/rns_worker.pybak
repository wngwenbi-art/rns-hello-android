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
image_peer_hashes = {}
active_links      = {}   # peer_hash -> active RNS.Link  # lxmf_hash -> rnshello.image destination hash

RNS_CONFIG = """
[reticulum]
  enable_transport = yes
  share_instance = no
  share_instance_port = 37428
  panic_on_interface_error = no
  use_implicit_proof = yes

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
        # NOTE: read loop is NOT started here — call start_reading() after
        # RNS.Reticulum() has fully initialised so Transport's reverse table
        # and interface lookup structures include this interface before any
        # inbound packets (including link proofs) are processed.

    def start_reading(self):
        """Start the BT read loop. Call this AFTER RNS.Reticulum() init."""
        self._flush_until = time.time() + 2.0  # Discard stale packets for first 2s
        threading.Thread(target=self._read_loop, daemon=True).start()
        RNS.log(f"AndroidBTInterface read loop started for {self.name}")

    def _read_loop(self):
        while self.online:
            try:
                data = self._socket.read(512)
                if data and len(data) > 0:
                    self._parse_kiss(data)
            except Exception as e:
                RNS.log(f"BT read error: {e}")
                self.online = False

    def _deliver(self, pkt):
        """Pass a packet to RNS Transport."""
        if len(pkt) > 0:
            # Discard packets during startup flush window (clears RNode buffer)
            if hasattr(self, '_flush_until') and time.time() < self._flush_until:
                RNS.log(f"Discarding buffered packet (flush window) len={len(pkt)}")
                return
            self.rxb += len(pkt)
            try:
                self.owner.inbound(pkt, self)
            except Exception as e:
                RNS.log(f"inbound error: {e}")

    def _parse_kiss(self, data):
        for byte in data:
            if byte == KISS_FEND:
                if self._in_frame and len(self._kiss_buf) > 1:
                    port = self._kiss_buf[0]
                    body = bytes(self._kiss_buf[1:])
                    RNS.log(f"RX KISS port=0x{port:02x} len={len(body)}")
                    if port == CMD_DATA and len(body) > 0:
                        self._deliver(body)
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
                RNS.log(f"IMG RECEIVED (LXMF ia) from {sender}: {img_fmt} {len(img_bytes)}B")
                filepath = _save_image_file(bytes(img_bytes), sender)
                with _data_lock:
                    chat_messages.append({
                        "from": sender,
                        "text": f"IMG_FILE:{filepath}",
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
    Store it in active_links so send_image can reuse it.
    """
    peer_hash = None
    try:
        remote_id = link.get_remote_identity()
        if remote_id:
            peer_hash = RNS.prettyhexrep(remote_id.hash).strip("<>")
    except Exception:
        pass
    RNS.log(f"LXMF incoming link from {peer_hash}")
    if peer_hash:
        with _data_lock:
            active_links[peer_hash] = link
        def on_close(lnk):
            with _data_lock:
                if active_links.get(peer_hash) is lnk:
                    del active_links[peer_hash]
        link.set_link_closed_callback(on_close)
        link.set_resource_strategy(RNS.Link.ACCEPT_ALL)
        RNS.log(f"Stored active link for {peer_hash}")

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

    # CRITICAL: must set ACCEPT_ALL or resource advertisements are silently rejected
    link.set_resource_strategy(RNS.Link.ACCEPT_ALL)
    link.set_resource_started_callback(resource_started)
    link.set_resource_concluded_callback(resource_concluded)
    link.set_link_closed_callback(lambda lnk: RNS.log("Image link closed"))
    RNS.log("Image link ready: ACCEPT_ALL set")

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
        if image_destination:
            try:
                image_destination.announce()
                RNS.log(f"Image re-announce at +{delay}s")
            except Exception as e:
                RNS.log(f"Image re-announce error: {e}")

    while True:
        time.sleep(600)  # 10 minutes
        if destination:
            try:
                destination.announce()
                RNS.log("Periodic re-announce sent (10 min)")
            except Exception as e:
                RNS.log(f"Periodic re-announce error: {e}")
        if image_destination:
            try:
                image_destination.announce()
                RNS.log("Periodic image re-announce sent")
            except Exception as e:
                RNS.log(f"Periodic image re-announce error: {e}")

def _rns_main(bt_socket_wrapper):
    global destination, lxmf_router, reticulum
    try:
        configure_rnode(bt_socket_wrapper)

        # Brief pause to let any RNode-buffered packets from previous sessions arrive,
        # then the interface read loop will discard them during its startup flush period
        RNS.log("Waiting 2s for RNode buffer to clear...")
        import time as _time
        _time.sleep(2.0)

        configdir = "/data/data/com.example.rnshello/files/.reticulum"
        os.makedirs(configdir, exist_ok=True)
        with open(os.path.join(configdir, "config"), "w") as f:
            f.write(RNS_CONFIG)

        # Suppress signal() calls — on a background thread
        original_signal = signal.signal
        signal.signal = _noop_signal

            # Clear stale RNS storage if from old non-transport build
        version_file = "/data/data/com.example.rnshello/files/.rns_version"
        current_version = "transport_v1"
        needs_clear = True
        try:
            if os.path.exists(version_file):
                with open(version_file) as vf:
                    if vf.read().strip() == current_version:
                        needs_clear = False
        except Exception:
            pass
        if needs_clear:
            import shutil
            for stale in [configdir,
                          "/data/data/com.example.rnshello/files/lxmf"]:
                try:
                    if os.path.exists(stale):
                        shutil.rmtree(stale)
                        RNS.log(f"Cleared stale storage: {stale}")
                except Exception as e:
                    RNS.log(f"Clear error: {e}")
            os.makedirs(configdir, exist_ok=True)
            with open(version_file, "w") as vf:
                vf.write(current_version)
            RNS.log("Storage cleared and version stamp written")

        # Write config fresh — must happen after storage clear
        with open(os.path.join(configdir, "config"), "w") as _cf:
            _cf.write(RNS_CONFIG)
        RNS.log(f"Config written: {os.path.join(configdir, 'config')}")

        reticulum = RNS.Reticulum(configdir=configdir, loglevel=RNS.LOG_DEBUG)
        RNS.log(f"Reticulum init done. Interfaces before add: {[i.name for i in RNS.Transport.interfaces]}")

        # Register interface AFTER Reticulum init — Reticulum.start() rebuilds
        # Transport.interfaces from config, so pre-registered interfaces get cleared.
        iface = AndroidBTInterface(RNS.Transport, "RNodeBT", bt_socket_wrapper)
        RNS.Transport.interfaces.append(iface)
        RNS.log(f"Interface registered. Interfaces now: {[i.name for i in RNS.Transport.interfaces]}")

        iface.start_reading()
        RNS.log("BT read loop started")

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

        lxmf_router = LXMF.LXMRouter(
            storagepath="/data/data/com.example.rnshello/files/lxmf",
            autopeer=True
        )
        # Patch LXMF retry interval to be longer than link establishment timeout
        # Default is 4s which causes constant new link requests before proof arrives
        try:
            LXMF.LXMRouter.DELIVERY_RETRY_WAIT = 15
            RNS.log("Patched LXMF DELIVERY_RETRY_WAIT=15s")
        except Exception as e:
            RNS.log(f"Could not patch DELIVERY_RETRY_WAIT: {e}")
        try:
            lxmf_router.delivery_retry_wait = 15
        except Exception:
            pass
        signal.signal = original_signal
        # Patch LXMF constants for LoRa reliability
        # DELIVERY_RETRY_WAIT must be > LoRa RTT (~3s) to avoid link collision
        # where sender opens new link before proof from previous attempt arrives
        for attr, val, desc in [
            ("MAX_DELIVERY_ATTEMPTS", 20,  "max retries"),
            ("DELIVERY_RETRY_WAIT",   30,  "seconds between retries (must be > LoRa RTT)"),
            ("OUTBOUND_PROCESSING_INTERVAL", 4, "outbound processing interval"),
        ]:
            try:
                old_val = getattr(LXMF.LXMRouter, attr, None)
                if old_val is not None:
                    setattr(LXMF.LXMRouter, attr, val)
                    RNS.log(f"Patched LXMF.{attr}: {old_val}→{val} ({desc})")
            except Exception as e:
                RNS.log(f"Could not patch LXMF.{attr}: {e}")

        destination = lxmf_router.register_delivery_identity(
            identity,
            display_name="RNS Hello Android"
        )
        lxmf_dest_hash = RNS.prettyhexrep(destination.hash).strip("<>")
        id_hash = RNS.prettyhexrep(identity.hash).strip("<>")
        RNS.log(f"LXMF dest={lxmf_dest_hash} identity={id_hash}")
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

def clear_reticulum_storage():
    """Delete cached RNS identity/path storage to force fresh state."""
    import shutil
    cleared = []
    for path in [
        "/data/data/com.example.rnshello/files/.reticulum",
        "/data/data/com.example.rnshello/files/lxmf",
    ]:
        try:
            if os.path.exists(path):
                shutil.rmtree(path)
                cleared.append(path)
        except Exception as e:
            cleared.append(f"ERROR {path}: {e}")
    RNS.log(f"Cleared storage: {cleared}")
    return f"Cleared: {', '.join(cleared)}"

def announce():
    try:
        if destination:
            destination.announce()
            addr = RNS.prettyhexrep(destination.hash).strip("<>")
            RNS.log(f"Manual announce sent: {addr}")
            # Also announce image destination so peers can find our image hash
            if image_destination:
                image_destination.announce()
                img_addr = RNS.prettyhexrep(image_destination.hash).strip("<>")
                RNS.log(f"Image destination re-announced: {img_addr}")
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
    Send an image via LXMF using the 'ia' (image attachment) field.
    This is exactly how Sideband sends images — no separate link needed,
    LXMF handles routing, retries and delivery confirmation.
    Format: field key 'ia', value ['webp', raw_bytes]
    """
    import base64 as _b64
    global lxmf_router, destination

    if not lxmf_router or not destination:
        return "Not connected"

    try:
        dest_hash_hex = dest_hash_hex.strip().strip("<>")
        img_bytes = _b64.b64decode(webp_b64)
        kb = len(img_bytes) / 1024
        RNS.log(f"send_image (LXMF): {kb:.1f} KB to {dest_hash_hex}")

        dest_hash = bytes.fromhex(dest_hash_hex)
        recalled_identity = RNS.Identity.recall(dest_hash)
        if recalled_identity is None:
            return "Unknown peer — ask them to tap Announce first"

        lxmf_dest = RNS.Destination(
            recalled_identity,
            RNS.Destination.OUT,
            RNS.Destination.SINGLE,
            "lxmf", "delivery"
        )

        msg = LXMF.LXMessage(
            lxmf_dest,
            destination,
            "",
            desired_method=LXMF.LXMessage.OPPORTUNISTIC
        )
        msg.fields = {"ia": ["webp", img_bytes]}

        def delivery_cb(m):
            RNS.log(f"Image LXMF delivery state: {m.state}")
            if m.state == LXMF.LXMessage.DELIVERED:
                try:
                    sent_path = _save_image_file(img_bytes, "me")
                except Exception:
                    sent_path = ""
                ts = time.strftime("%H:%M:%S")
                with _data_lock:
                    chat_messages.append({
                        "from": "me",
                        "text": f"IMG_FILE:{sent_path}" if sent_path else "📷 Image sent",
                        "ts": ts,
                        "direction": "out"
                    })

        msg.register_delivery_callback(delivery_cb)
        lxmf_router.handle_outbound(msg)
        RNS.log(f"Image queued via LXMF ia field ({kb:.1f} KB)")

        # Show optimistic sent bubble immediately
        try:
            sent_path = _save_image_file(img_bytes, "me")
        except Exception:
            sent_path = ""
        ts = time.strftime("%H:%M:%S")
        with _data_lock:
            chat_messages.append({
                "from": "me",
                "text": f"IMG_FILE:{sent_path}" if sent_path else f"📷 Sending ({kb:.1f} KB)...",
                "ts": ts,
                "direction": "out"
            })
        return f"Image sending ({kb:.1f} KB)"

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
