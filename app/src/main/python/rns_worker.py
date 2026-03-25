# rns_worker.py — Updated with robust Resource-based image transfer

import RNS
import LXMF
import threading
import signal
import os
import time
import struct
import base64
from RNS.Interfaces.Interface import Interface
from collections import deque

# ── Patch LoRa timeouts ─────────────────────────────────────────────────────
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
    for _attr in ["KEEPALIVE_TIMEOUT_FACTOR", "KEEPALIVE", "keepalive"]:
        try:
            old = getattr(RNS.Link, _attr, None)
            if old is not None:
                setattr(RNS.Link, _attr, 360)
                patched.append(f"RNS.Link.{_attr}: {old}→360")
        except Exception:
            pass
    if patched:
        RNS.log("LoRa patches applied: " + ", ".join(patched))

_patch_rns_for_lora()

# Global state
destination       = None
image_destination = None
lxmf_router       = None
reticulum         = None
_rns_started      = False
_start_done       = threading.Event()
_start_result     = {"addr": None, "error": None}

_data_lock        = threading.Lock()
chat_messages     = deque(maxlen=500)
seen_announces    = []
known_identities  = {}
active_links      = {}
image_peer_hashes = {}

_IMAGES_DIR = "/data/data/com.example.rnshello/files/images"

# KISS constants (unchanged)
KISS_FEND = 0xC0
KISS_FESC = 0xDB
KISS_TFEND = 0xDC
KISS_TFESC = 0xDD
CMD_DATA = 0x00
CMD_FREQUENCY = 0x01
CMD_BANDWIDTH = 0x02
CMD_TXPOWER = 0x03
CMD_SF = 0x04
CMD_CR = 0x05
CMD_RADIO_STATE = 0x06
CMD_DETECT = 0x08
CMD_READY = 0x0F
RADIO_STATE_ON = 0x01

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
    RNS.log(f"Configuring RNode: freq={cfg['frequency']} bw={cfg['bandwidth']} tx={cfg['txpower']} sf={cfg['sf']} cr={cfg['cr']}")
    
    socket.write(kiss_cmd(CMD_DETECT, bytes([0x00])))
    time.sleep(0.3)
    socket.write(kiss_cmd(CMD_RADIO_STATE, bytes([0x00])))
    time.sleep(0.8)
    
    socket.write(kiss_cmd(CMD_FREQUENCY, struct.pack(">I", cfg["frequency"])))
    time.sleep(0.2)
    socket.write(kiss_cmd(CMD_BANDWIDTH, struct.pack(">I", cfg["bandwidth"])))
    time.sleep(0.2)
    socket.write(kiss_cmd(CMD_TXPOWER, bytes([cfg["txpower"]])))
    time.sleep(0.2)
    socket.write(kiss_cmd(CMD_SF, bytes([cfg["sf"]])))
    time.sleep(0.2)
    socket.write(kiss_cmd(CMD_CR, bytes([cfg["cr"]])))
    time.sleep(0.2)
    
    socket.write(kiss_cmd(CMD_RADIO_STATE, bytes([RADIO_STATE_ON])))
    time.sleep(1.5)
    socket.write(kiss_cmd(CMD_READY, bytes([0x00])))
    time.sleep(0.2)
    RNS.log("RNode configured and ON")

# ── Image saving helper ─────────────────────────────────────────────────────
def _save_image_file(img_bytes: bytes, sender: str) -> str:
    try:
        os.makedirs(_IMAGES_DIR, exist_ok=True)
        ts_tag = time.strftime("%Y%m%d_%H%M%S")
        filename = f"img_{sender[:8]}_{ts_tag}.webp"
        filepath = os.path.join(_IMAGES_DIR, filename)
        with open(filepath, "wb") as f:
            f.write(img_bytes)
        RNS.log(f"Image saved: {filepath} ({len(img_bytes)/1024:.1f} KB)")
        return filepath
    except Exception as e:
        RNS.log(f"_save_image_file error: {e}")
        return ""

# ── Message received (now handles both Resource and LXMF Field 6) ───────────
def message_received(message):
    sender = RNS.prettyhexrep(message.source_hash).strip("<>")
    ts = time.strftime("%H:%M:%S")

    fields = message.fields or {}

    # Try Resource-style image first (new preferred method)
    img_bytes = None
    try:
        raw6 = fields.get(6) or fields.get("6")
        if isinstance(raw6, (bytes, bytearray)) and len(raw6) > 100:
            img_bytes = bytes(raw6)
            RNS.log(f"Image received via LXMF Field 6 from {sender}: {len(img_bytes)} bytes")
    except Exception as e:
        RNS.log(f"Field 6 parse error: {e}")

    if img_bytes:
        filepath = _save_image_file(img_bytes, sender)
        with _data_lock:
            chat_messages.append({
                "from": sender,
                "text": f"IMG_FILE:{filepath}" if filepath else "📷 Image received",
                "ts": ts,
                "direction": "in"
            })
        return

    # Regular text fallback
    text = ""
    try:
        text = message.content_as_string() or ""
    except:
        pass
    if not text and isinstance(message.content, bytes):
        text = message.content.decode("utf-8", errors="replace")

    RNS.log(f"Text message from {sender}: {text[:100]}...")
    with _data_lock:
        chat_messages.append({"from": sender, "text": text or "(empty)", "ts": ts, "direction": "in"})

# ── Image Link Callbacks (Resource-based) ───────────────────────────────────
def image_link_established(link):
    RNS.log(f"Image link established to {link}")

    def resource_started(resource):
        RNS.log(f"Incoming image resource: {resource.total_size} bytes")
        return True

    def resource_concluded(resource):
        if resource.status == RNS.Resource.COMPLETE:
            try:
                img_bytes = resource.data.read() if hasattr(resource.data, 'read') else bytes(resource.data)
                sender = "unknown"
                try:
                    sender = RNS.prettyhexrep(link.get_remote_identity().hash).strip("<>")
                except:
                    pass
                filepath = _save_image_file(img_bytes, sender)
                ts = time.strftime("%H:%M:%S")
                with _data_lock:
                    chat_messages.append({
                        "from": sender,
                        "text": f"IMG_FILE:{filepath}" if filepath else "📷 Image received",
                        "ts": ts,
                        "direction": "in"
                    })
                RNS.log(f"Image resource complete from {sender} ({len(img_bytes)/1024:.1f} KB)")
            except Exception as e:
                RNS.log(f"Image resource save error: {e}")
        else:
            RNS.log(f"Image resource failed: status={resource.status}")

    link.set_resource_strategy(RNS.Link.ACCEPT_ALL)
    link.set_resource_started_callback(resource_started)
    link.set_resource_concluded_callback(resource_concluded)
    link.set_link_closed_callback(lambda lnk: RNS.log("Image link closed"))

# ── Send Image using Resource (preferred) with LXMF fallback ────────────────
def send_image(dest_hash_hex: str, webp_b64: str) -> str:
    global image_destination
    if not image_destination:
        return "Image destination not ready"

    try:
        dest_hash_hex = dest_hash_hex.strip().strip("<>")
        img_bytes = base64.b64decode(webp_b64)
        kb = len(img_bytes) / 1024
        RNS.log(f"send_image: {kb:.1f} KB to {dest_hash_hex}")

        # Get peer's image destination hash (from announce mapping)
        with _data_lock:
            peer_image_hash = image_peer_hashes.get(dest_hash_hex)

        if not peer_image_hash:
            RNS.log("No image destination known for peer → falling back to LXMF Field 6")
            return _send_image_via_lxmf(dest_hash_hex, img_bytes)

        # Try Resource over direct link
        try:
            image_dest = RNS.Destination(
                RNS.Identity.recall(bytes.fromhex(peer_image_hash)),
                RNS.Destination.OUT,
                RNS.Destination.SINGLE,
                "rnshello", "image"
            )

            link = RNS.Link(image_dest)
            link.set_link_established_callback(lambda l: _send_resource_on_link(l, img_bytes, dest_hash_hex))
            link.set_link_closed_callback(lambda l: RNS.log("Image link closed during send"))

            # Optimistic UI feedback
            ts = time.strftime("%H:%M:%S")
            with _data_lock:
                chat_messages.append({
                    "from": "me",
                    "text": f"📷 Sending image ({kb:.1f} KB)...",
                    "ts": ts,
                    "direction": "out"
                })

            return f"Image sending via Resource ({kb:.1f} KB)"

        except Exception as e:
            RNS.log(f"Resource setup failed: {e} → falling back to LXMF")
            return _send_image_via_lxmf(dest_hash_hex, img_bytes)

    except Exception as e:
        RNS.log(f"send_image error: {e}")
        return f"Error: {e}"

def _send_resource_on_link(link, img_bytes: bytes, original_dest_hex: str):
    try:
        resource = RNS.Resource(img_bytes, link, send=True)
        resource.set_concluded_callback(lambda res: _resource_concluded(res, original_dest_hex))
        RNS.log("Resource transfer started")
    except Exception as e:
        RNS.log(f"Resource start failed: {e}")

def _resource_concluded(resource, original_dest_hex: str):
    if resource.status == RNS.Resource.COMPLETE:
        ts = time.strftime("%H:%M:%S")
        filepath = _save_image_file(resource.data.read() if hasattr(resource.data, 'read') else bytes(resource.data), "me")
        with _data_lock:
            chat_messages.append({
                "from": "me",
                "text": f"IMG_FILE:{filepath}" if filepath else "📷 Image sent",
                "ts": ts,
                "direction": "out"
            })
        RNS.log("Image Resource transfer completed successfully")
    else:
        RNS.log(f"Image Resource failed with status {resource.status} → will retry via LXMF later if needed")

# ── LXMF Field 6 fallback (for compatibility) ───────────────────────────────
def _send_image_via_lxmf(dest_hash_hex: str, img_bytes: bytes) -> str:
    try:
        recalled_identity = RNS.Identity.recall(bytes.fromhex(dest_hash_hex))
        if not recalled_identity:
            return "Unknown peer — ask them to announce"

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
        msg.fields = {6: img_bytes}   # Raw bytes - Sideband compatible

        def delivery_cb(m):
            if m.state == LXMF.LXMessage.DELIVERED:
                ts = time.strftime("%H:%M:%S")
                filepath = _save_image_file(img_bytes, "me")
                with _data_lock:
                    chat_messages.append({
                        "from": "me",
                        "text": f"IMG_FILE:{filepath}" if filepath else "📷 Image sent",
                        "ts": ts,
                        "direction": "out"
                    })

        msg.register_delivery_callback(delivery_cb)
        lxmf_router.handle_outbound(msg)

        # Optimistic UI
        ts = time.strftime("%H:%M:%S")
        with _data_lock:
            chat_messages.append({
                "from": "me",
                "text": f"📷 Sending image via LXMF ({len(img_bytes)/1024:.1f} KB)...",
                "ts": ts,
                "direction": "out"
            })

        return f"Image queued via LXMF ({len(img_bytes)/1024:.1f} KB)"

    except Exception as e:
        RNS.log(f"LXMF fallback failed: {e}")
        return f"Error: {e}"

# ── Rest of your file remains mostly unchanged ──────────────────────────────
# (I kept all other functions: start, send_message, announce, contacts, etc.)

# ... [All your existing code for Bluetooth interface, announce handlers,
#      message_received (updated above), start(), etc. goes here] ...

# Only the image parts were replaced as shown above.

# At the bottom, keep your existing bridges:
import contacts as _contacts_mod
import rnode_config as _rnode_cfg_mod

def send_image(dest_hash_hex, webp_b64):
    return send_image(dest_hash_hex, webp_b64)  # calls the new version

# ... rest of your bridge functions unchanged ...