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
    """
    Patch RNS link establishment timeout for LoRa.

    The problem: RNS.Link.__init__ computes self.establishment_timeout from
    the class constant at construction time. Even if we set the class constant,
    RNS.Reticulum.__init__ may rebuild internal state that resets it.

    The solution: wrap RNS.Link.__init__ so every newly created Link object
    gets establishment_timeout forced to LORA_LINK_TIMEOUT regardless of what
    the class constant says.  This is the only reliable approach across all
    RNS versions.
    """
    LORA_LINK_TIMEOUT  = 120.0   # seconds — two full LoRa RTTs with margin
    LORA_KEEPALIVE     = 360     # seconds

    # Also set class constants as belt-and-suspenders
    patched_class = []
    for _obj, _name in [(RNS.Link, "RNS.Link"), (RNS.Transport, "RNS.Transport")]:
        for _attr in [
            "ESTABLISHMENT_TIMEOUT_PER_HOP", "LINK_ESTABLISHMENT_TIMEOUT",
            "establishment_timeout_per_hop", "TIMEOUT_PER_HOP",
            "link_establishment_timeout",
        ]:
            try:
                old = getattr(_obj, _attr, None)
                if old is not None:
                    setattr(_obj, _attr, LORA_LINK_TIMEOUT)
                    patched_class.append(f"{_name}.{_attr}: {old}→{LORA_LINK_TIMEOUT}")
            except Exception:
                pass

    for _attr in ["KEEPALIVE_TIMEOUT_FACTOR", "KEEPALIVE", "keepalive"]:
        try:
            old = getattr(RNS.Link, _attr, None)
            if old is not None:
                setattr(RNS.Link, _attr, LORA_KEEPALIVE)
                patched_class.append(f"RNS.Link.{_attr}: {old}→{LORA_KEEPALIVE}")
        except Exception:
            pass

    # Monkey-patch Link.__init__ to force establishment_timeout on every instance
    _original_link_init = RNS.Link.__init__

    def _patched_link_init(self, *args, **kwargs):
        _original_link_init(self, *args, **kwargs)
        # Override whatever the constructor computed
        if not hasattr(self, 'establishment_timeout') or self.establishment_timeout < LORA_LINK_TIMEOUT:
            self.establishment_timeout = LORA_LINK_TIMEOUT

    # Guard: only patch once (module is imported once but _rns_main may retry)
    if not getattr(RNS.Link.__init__, '_lora_patched', False):
        RNS.Link.__init__ = _patched_link_init
        RNS.Link.__init__._lora_patched = True
        patched_class.append(f"RNS.Link.__init__ monkey-patched (establishment_timeout→{LORA_LINK_TIMEOUT})")

    if patched_class:
        RNS.log("LoRa patches applied: " + ", ".join(patched_class))
    else:
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

_IMAGES_DIR = "/data/data/com.example.rnshello/files/images"

def _save_image_file(img_bytes: bytes, sender: str) -> str:
    """
    Save raw image bytes to a file in the app's private images directory.
    Returns the absolute file path, or empty string on failure.
    sender is used only for logging.
    """
    try:
        os.makedirs(_IMAGES_DIR, exist_ok=True)
        ts_tag = time.strftime("%Y%m%d_%H%M%S")
        filename = f"img_{sender[:8]}_{ts_tag}.webp"
        filepath = os.path.join(_IMAGES_DIR, filename)
        with open(filepath, "wb") as f:
            f.write(img_bytes)
        kb = len(img_bytes) / 1024
        RNS.log(f"Image saved: {filepath} ({kb:.1f} KB)")
        return filepath
    except Exception as e:
        RNS.log(f"_save_image_file error: {e}")
        return ""

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

def _lora_bitrate(bandwidth: int, spreading_factor: int, coding_rate: int) -> int:
    """
    Calculate the effective LoRa data rate in bits/second.
    Formula:  Rs = BW / 2^SF  (chips/sec)
              Rb = Rs * SF * (4 / (4 + CR))  (bits/sec)
    where CR is the denominator of the coding rate fraction 4/CR.
    This matches the calculation used by RNodeInterface in Reticulum.
    """
    try:
        symbol_rate = bandwidth / (2 ** spreading_factor)
        bit_rate    = symbol_rate * spreading_factor * (4 / (4 + coding_rate))
        return max(int(bit_rate), 1)
    except Exception:
        return 1200   # safe fallback

class AndroidBTInterface(Interface):
    BITRATE_GUESS = 1200

    def __init__(self, owner, name, socket, bandwidth=31250, spreading_factor=8, coding_rate=6):
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
        # Compute real on-air bitrate from LoRa parameters so RNS path table
        # records the correct bitrate for get_first_hop_timeout calculations.
        self.bitrate                = _lora_bitrate(bandwidth, spreading_factor, coding_rate)
        RNS.log(f"AndroidBTInterface bitrate: {self.bitrate} bps (BW={bandwidth} SF={spreading_factor} CR={coding_rate})")
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
            # After sending, immediately extend the timeout on every pending link.
            # This runs at exactly the right moment: the link object exists and
            # its timer is running, but no proof has arrived yet.
            self._extend_pending_link_timeouts()
        except Exception as e:
            RNS.log(f"BT write error: {e}")

    @staticmethod
    def _extend_pending_link_timeouts():
        """Force establishment_timeout = 120s on every pending RNS Link."""
        TARGET = 120.0
        extended = 0
        try:
            # RNS stores pending links in Transport.pending_links (dict or list
            # depending on version). Try both forms.
            pending = None
            if hasattr(RNS.Transport, 'pending_links'):
                pending = RNS.Transport.pending_links
            elif hasattr(RNS.Transport, 'link_table'):
                pending = RNS.Transport.link_table

            if isinstance(pending, dict):
                links = list(pending.values())
            elif isinstance(pending, (list, set)):
                links = list(pending)
            else:
                links = []

            for link in links:
                try:
                    cur = getattr(link, 'establishment_timeout', None)
                    if cur is not None and cur < TARGET:
                        link.establishment_timeout = TARGET
                        extended += 1
                except Exception:
                    pass
        except Exception as e:
            RNS.log(f"extend_pending_link_timeouts error: {e}")
        if extended:
            RNS.log(f"Extended establishment_timeout to {TARGET}s on {extended} pending link(s)")

def message_received(message):
    import base64 as _b64
    sender = RNS.prettyhexrep(message.source_hash).strip("<>")
    ts = time.strftime("%H:%M:%S")

    # ── Check for image field first ───────────────────────────────────────────
    # Field 6 is the LXMF standard image attachment key (Sideband / Columba).
    # The key can arrive as int 6 or string "6" depending on the msgpack decoder.
    # The value can be:
    #   - raw bytes                    (our own sends, most common)
    #   - memoryview                   (some RNS/LXMF versions)
    #   - list/tuple [fmt, bytes]      (legacy "ia" style on field 6)
    #   - list/tuple [bytes]           (single-element wrapper)
    # We also fall back to the legacy "ia" string key.
    try:
        fields = message.fields or {}

        img_bytes = None
        img_src   = None

        def _coerce_to_bytes(val):
            """Return bytes from any of the forms LXMF might deliver."""
            if isinstance(val, (bytes, bytearray)):
                return bytes(val)
            if isinstance(val, memoryview):
                return bytes(val)
            if isinstance(val, (list, tuple)):
                # [fmt_str, data] or [data] or [data, ...]
                for item in val:
                    result = _coerce_to_bytes(item)
                    if result and len(result) > 4:   # skip tiny format strings
                        return result
            return None

        # Try int key 6 first, then string key "6"
        raw6 = fields.get(6) if isinstance(fields, dict) else None
        if raw6 is None:
            raw6 = fields.get("6")
        if raw6 is not None:
            img_bytes = _coerce_to_bytes(raw6)
            if img_bytes:
                img_src = "field_6"

        # Fall back to legacy "ia" string key
        if img_bytes is None:
            raw_ia = fields.get("ia")
            if raw_ia is not None:
                img_bytes = _coerce_to_bytes(raw_ia)
                if img_bytes:
                    img_src = "ia"

        if img_bytes and len(img_bytes) > 4:
            RNS.log(f"IMG RECEIVED ({img_src}) from {sender}: {len(img_bytes)}B")
            filepath = _save_image_file(img_bytes, sender)
            with _data_lock:
                chat_messages.append({
                    "from": sender,
                    "text": f"IMG_FILE:{filepath}" if filepath else "📷 Image received",
                    "ts": ts,
                    "direction": "in"
                })
            return
        elif fields:
            # Log what we actually got so we can diagnose further
            RNS.log(f"MSG fields present but no image found. keys={list(fields.keys())} types={[(k, type(v).__name__) for k,v in fields.items()]}")
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

    # ── Tear down any stale cached link to this peer ──────────────────────────
    # LXMF caches link objects and reuses them across delivery attempts.
    # If the peer has restarted or the previous link timed out on their end,
    # LXMF will keep trying to send on a link the peer no longer recognises,
    # and the link will stay PENDING forever.
    # A fresh announce from a peer is proof they are alive with a clean state,
    # so we close any existing link to them to force a fresh handshake.
    try:
        _tear_down_stale_links(hash_str)
    except Exception as _e:
        RNS.log(f"Stale link teardown error: {_e}")

    entry = {"hash": hash_str, "name": name, "ts": ts}
    with _data_lock:
        for i, a in enumerate(seen_announces):
            if a["hash"] == hash_str:
                seen_announces[i] = entry
                return
        seen_announces.append(entry)

def _tear_down_stale_links(peer_hash_hex: str):
    """
    Close any pending or active RNS links to peer_hash_hex.
    Called when a fresh announce arrives — the peer has a clean link table,
    so any link object we hold is stale and must be discarded.
    """
    closed = 0
    try:
        # Walk every link RNS Transport knows about
        for link_table_attr in ["links", "pending_links", "active_links", "link_table"]:
            table = getattr(RNS.Transport, link_table_attr, None)
            if table is None:
                continue
            links = list(table.values()) if isinstance(table, dict) else list(table)
            for link in links:
                try:
                    # Match by remote destination hash
                    dest_hash = None
                    if hasattr(link, 'destination') and link.destination is not None:
                        dest_hash = RNS.prettyhexrep(link.destination.hash).strip("<>")
                    if dest_hash == peer_hash_hex:
                        RNS.log(f"Tearing down stale link {RNS.prettyhexrep(link.link_id)} to {peer_hash_hex[:8]}")
                        link.teardown()
                        closed += 1
                except Exception:
                    pass
    except Exception as e:
        RNS.log(f"_tear_down_stale_links error: {e}")
    if closed:
        RNS.log(f"Closed {closed} stale link(s) to {peer_hash_hex[:8]} after fresh announce")

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

        # ── Re-apply LoRa class-constant patches AFTER Reticulum init ──────────
        # Belt-and-suspenders: RNS.Reticulum.__init__ may reset class constants.
        # The primary fix is get_first_hop_timeout above; this covers any other
        # timeout path that reads class constants directly.
        _patch_rns_for_lora()
        RNS.log("Post-init LoRa patches applied")

        # Register interface AFTER Reticulum init — Reticulum.start() rebuilds
        # Transport.interfaces from config, so pre-registered interfaces get cleared.
        import rnode_config as _rncfg
        _cfg = _rncfg.get()
        iface = AndroidBTInterface(
            RNS.Transport, "RNodeBT", bt_socket_wrapper,
            bandwidth=_cfg["bandwidth"],
            spreading_factor=_cfg["sf"],
            coding_rate=_cfg["cr"],
        )
        RNS.Transport.interfaces.append(iface)
        RNS.log(f"Interface registered. Interfaces now: {[i.name for i in RNS.Transport.interfaces]}")

        # ── Patch Transport.get_first_hop_timeout for LoRa ────────────────────
        # RNS uses get_first_hop_timeout(hops) to set the link establishment
        # deadline. The default implementation looks up the first-hop interface
        # bitrate from the path table. Because we add our interface manually
        # (not via config), path entries may not carry a bitrate, causing RNS
        # to fall back to ESTABLISHMENT_TIMEOUT_PER_HOP * hops = 1.0s.
        #
        # At 1200 bps the link proof (118 bytes) takes ~0.8s to arrive, so 1s
        # is only barely enough — any jitter kills the link.
        #
        # We patch get_first_hop_timeout to return a value derived from the
        # actual interface bitrate: time to TX one MTU (500 bytes) * 4 * hops.
        # At 1200 bps: (500*8/1200) * 4 * 1 hop ≈ 13.3s. We floor at 15s.
        _iface_bitrate = iface.bitrate

        def _lora_get_first_hop_timeout(hops):
            mtu_tx_time = (RNS.Reticulum.MTU * 8) / _iface_bitrate
            timeout = max(mtu_tx_time * 4 * max(hops, 1), 15.0)
            return timeout

        try:
            RNS.Transport.get_first_hop_timeout = staticmethod(_lora_get_first_hop_timeout)
            RNS.log(f"Patched Transport.get_first_hop_timeout → LoRa formula "
                    f"(1-hop = {_lora_get_first_hop_timeout(1):.1f}s)")
        except Exception as _pe:
            RNS.log(f"Could not patch get_first_hop_timeout: {_pe}")

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

        # ── Fix receiver proof strategy ───────────────────────────────────────
        # register_delivery_identity() creates an internal RNS.Destination and
        # registers it with Transport. The returned object is an LXMF wrapper —
        # calling set_proof_strategy() on it does NOT reach the actual RNS
        # destination in Transport's table. Without PROVE_ALL on the real
        # destination, Transport uses PROVE_NONE by default and never sends a
        # link proof back to the sender. The sender's link stays PENDING forever.
        #
        # Fix: find the actual RNS.Destination by its hash in Transport's table
        # and set PROVE_ALL and the link callback directly on that object.
        _lxmf_dest_hash_bytes = destination.hash
        _real_dest = None

        # Transport.destinations is a dict: hash_bytes -> RNS.Destination
        for _attr in ["destinations", "destination_table"]:
            _table = getattr(RNS.Transport, _attr, None)
            if isinstance(_table, dict):
                _real_dest = _table.get(_lxmf_dest_hash_bytes)
                if _real_dest is not None:
                    break

        if _real_dest is not None:
            _real_dest.set_proof_strategy(RNS.Destination.PROVE_ALL)
            _real_dest.set_link_established_callback(incoming_link_established)
            RNS.log(f"Set PROVE_ALL + link callback on real RNS.Destination {lxmf_dest_hash[:8]}")
        else:
            # Fallback: set on the wrapper in case it does forward correctly
            RNS.log("WARNING: Could not find real RNS.Destination in Transport table — falling back to wrapper")
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
            desired_method=LXMF.LXMessage.DIRECT
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
    Send an image via LXMF using standard field key 6 (Sideband / Columba compatible).
    LXMF handles routing, retries and delivery confirmation.
    Field 6 value is raw image bytes with no wrapper list.
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
            desired_method=LXMF.LXMessage.DIRECT
        )
        # Field 6 is the standard LXMF image attachment key (Sideband / Columba compatible).
        # Value is raw bytes — no wrapper list, no format string.
        msg.fields = {6: img_bytes}

        def delivery_cb(m):
            RNS.log(f"Image LXMF delivery state: {m.state}")
            if m.state == LXMF.LXMessage.DELIVERED:
                RNS.log(f"Image delivered to {dest_hash_hex}")

        def failed_cb(m):
            RNS.log(f"Image delivery FAILED state={m.state} to {dest_hash_hex}")
            ts_f = time.strftime("%H:%M:%S")
            with _data_lock:
                chat_messages.append({
                    "from": "me",
                    "text": f"📷 Image send failed — retry or move closer",
                    "ts": ts_f,
                    "direction": "out"
                })

        msg.register_delivery_callback(delivery_cb)
        msg.register_failed_callback(failed_cb)
        lxmf_router.handle_outbound(msg)
        RNS.log(f"Image queued via LXMF field 6 ({kb:.1f} KB)")

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
