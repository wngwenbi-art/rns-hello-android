"""
rns_worker.py — RNS + LXMF worker with robust Resource-based image transfer
"""

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

# ── LoRa timeout patches ─────────────────────────────────────────────────────
def _patch_rns_for_lora():
    for attr in ["ESTABLISHMENT_TIMEOUT_PER_HOP", "LINK_ESTABLISHMENT_TIMEOUT",
                 "establishment_timeout_per_hop", "TIMEOUT_PER_HOP"]:
        try:
            if hasattr(RNS.Link, attr):
                setattr(RNS.Link, attr, 60.0)
        except:
            pass
    for attr in ["KEEPALIVE_TIMEOUT_FACTOR", "KEEPALIVE", "keepalive"]:
        try:
            if hasattr(RNS.Link, attr):
                setattr(RNS.Link, attr, 360)
        except:
            pass
    RNS.log("LoRa patches applied")

_patch_rns_for_lora()

# Global state
destination = None
image_destination = None
lxmf_router = None
reticulum = None
_rns_started = False
_start_done = threading.Event()
_start_result = {"addr": None, "error": None}

_data_lock = threading.Lock()
chat_messages = deque(maxlen=500)
seen_announces = []
known_identities = {}
active_links = {}
image_peer_hashes = {}

_IMAGES_DIR = "/data/data/com.example.rnshello/files/images"

# KISS constants
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

def _save_image_file(img_bytes: bytes, sender: str) -> str:
    try:
        os.makedirs(_IMAGES_DIR, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        filename = f"img_{sender[:8]}_{ts}.webp"
        path = os.path.join(_IMAGES_DIR, filename)
        with open(path, "wb") as f:
            f.write(img_bytes)
        RNS.log(f"Image saved: {path} ({len(img_bytes)/1024:.1f} KB)")
        return path
    except Exception as e:
        RNS.log(f"_save_image_file error: {e}")
        return ""

# ── Message handler ─────────────────────────────────────────────────────────
def message_received(message):
    sender = RNS.prettyhexrep(message.source_hash).strip("<>")
    ts = time.strftime("%H:%M:%S")
    fields = message.fields or {}

    # Try LXMF Field 6 (compatibility + fallback)
    img_bytes = None
    raw = fields.get(6) or fields.get("6")
    if isinstance(raw, (bytes, bytearray)) and len(raw) > 100:
        img_bytes = bytes(raw)

    if img_bytes:
        RNS.log(f"Image via Field 6 from {sender}: {len(img_bytes)} bytes")
        path = _save_image_file(img_bytes, sender)
        with _data_lock:
            chat_messages.append({
                "from": sender,
                "text": f"IMG_FILE:{path}" if path else "📷 Image received",
                "ts": ts,
                "direction": "in"
            })
        return

    # Regular text
    text = message.content_as_string() or ""
    if not text and isinstance(message.content, bytes):
        text = message.content.decode("utf-8", errors="replace")

    with _data_lock:
        chat_messages.append({"from": sender, "text": text or "(empty)", "ts": ts, "direction": "in"})

# ── Image link for Resource transfer ────────────────────────────────────────
def image_link_established(link):
    RNS.log(f"Image link established: {link}")

    def resource_started(res):
        RNS.log(f"Resource started: {res.total_size} bytes")
        return True

    def resource_concluded(res):
        if res.status == RNS.Resource.COMPLETE:
            try:
                data = res.data.read() if hasattr(res.data, 'read') else bytes(res.data)
                sender = "unknown"
                try:
                    sender = RNS.prettyhexrep(link.get_remote_identity().hash).strip("<>")
                except:
                    pass
                path = _save_image_file(data, sender)
                ts = time.strftime("%H:%M:%S")
                with _data_lock:
                    chat_messages.append({
                        "from": sender,
                        "text": f"IMG_FILE:{path}" if path else "📷 Image received",
                        "ts": ts,
                        "direction": "in"
                    })
            except Exception as e:
                RNS.log(f"Resource save error: {e}")
        else:
            RNS.log(f"Resource failed: status={res.status}")

    link.set_resource_strategy(RNS.Link.ACCEPT_ALL)
    link.set_resource_started_callback(resource_started)
    link.set_resource_concluded_callback(resource_concluded)

# ── Send Image (Resource primary + LXMF fallback) ───────────────────────────
def send_image(dest_hash_hex: str, webp_b64: str) -> str:
    global image_destination
    if not image_destination:
        return "Image destination not ready"

    try:
        dest_hash_hex = dest_hash_hex.strip().strip("<>")
        img_bytes = base64.b64decode(webp_b64)
        kb = len(img_bytes) / 1024.0

        # Try Resource first
        with _data_lock:
            peer_img_hash = image_peer_hashes.get(dest_hash_hex)

        if peer_img_hash:
            try:
                peer_identity = RNS.Identity.recall(bytes.fromhex(peer_img_hash))
                if peer_identity:
                    img_dest = RNS.Destination(
                        peer_identity,
                        RNS.Destination.OUT,
                        RNS.Destination.SINGLE,
                        "rnshello", "image"
                    )
                    link = RNS.Link(img_dest)
                    link.set_link_established_callback(
                        lambda l: _send_resource(l, img_bytes, dest_hash_hex)
                    )
                    # Optimistic UI
                    ts = time.strftime("%H:%M:%S")
                    with _data_lock:
                        chat_messages.append({
                            "from": "me",
                            "text": f"📷 Sending via Resource ({kb:.1f} KB)...",
                            "ts": ts,
                            "direction": "out"
                        })
                    return f"Image sending via Resource ({kb:.1f} KB)"
            except Exception as e:
                RNS.log(f"Resource link failed: {e}")

        # Fallback to LXMF Field 6
        RNS.log("Resource unavailable → using LXMF Field 6 fallback")
        return _send_image_via_lxmf(dest_hash_hex, img_bytes)

    except Exception as e:
        RNS.log(f"send_image error: {e}")
        return f"Error: {e}"

def _send_resource(link, img_bytes: bytes, original_dest: str):
    try:
        resource = RNS.Resource(img_bytes, link, send=True)
        resource.set_concluded_callback(
            lambda res: _resource_done(res, original_dest)
        )
        RNS.log("Resource transfer started")
    except Exception as e:
        RNS.log(f"Resource start failed: {e}")

def _resource_done(resource, original_dest: str):
    if resource.status == RNS.Resource.COMPLETE:
        try:
            data = resource.data.read() if hasattr(resource.data, 'read') else bytes(resource.data)
            path = _save_image_file(data, "me")
            ts = time.strftime("%H:%M:%S")
            with _data_lock:
                chat_messages.append({
                    "from": "me",
                    "text": f"IMG_FILE:{path}" if path else "📷 Image sent",
                    "ts": ts,
                    "direction": "out"
                })
            RNS.log("Resource transfer completed")
        except Exception as e:
            RNS.log(f"Resource final save error: {e}")
    else:
        RNS.log(f"Resource failed with status {resource.status}")

def _send_image_via_lxmf(dest_hash_hex: str, img_bytes: bytes) -> str:
    try:
        identity = RNS.Identity.recall(bytes.fromhex(dest_hash_hex))
        if not identity:
            return "Unknown peer"

        lxmf_dest = RNS.Destination(identity, RNS.Destination.OUT, RNS.Destination.SINGLE, "lxmf", "delivery")
        msg = LXMF.LXMessage(lxmf_dest, destination, "", desired_method=LXMF.LXMessage.OPPORTUNISTIC)
        msg.fields = {6: img_bytes}

        def cb(m):
            if m.state == LXMF.LXMessage.DELIVERED:
                path = _save_image_file(img_bytes, "me")
                ts = time.strftime("%H:%M:%S")
                with _data_lock:
                    chat_messages.append({
                        "from": "me",
                        "text": f"IMG_FILE:{path}" if path else "📷 Image sent",
                        "ts": ts,
                        "direction": "out"
                    })
        msg.register_delivery_callback(cb)
        lxmf_router.handle_outbound(msg)

        ts = time.strftime("%H:%M:%S")
        with _data_lock:
            chat_messages.append({
                "from": "me",
                "text": f"📷 Sending via LXMF ({len(img_bytes)/1024:.1f} KB)...",
                "ts": ts,
                "direction": "out"
            })
        return f"Image queued via LXMF ({len(img_bytes)/1024:.1f} KB)"
    except Exception as e:
        return f"LXMF fallback error: {e}"

# ── Core startup (restored) ─────────────────────────────────────────────────
RNS_CONFIG = """
[reticulum]
  enable_transport = yes
  share_instance = no
  panic_on_interface_error = no
  use_implicit_proof = yes
[interfaces]
"""

def start(bt_socket_wrapper):
    global destination, image_destination, lxmf_router, reticulum, _rns_started
    if _rns_started:
        _start_done.wait(30)
        return _start_result["addr"] or _start_result["error"] or "Timeout"

    _rns_started = True
    _start_done.clear()
    _start_result["addr"] = None
    _start_result["error"] = None

    threading.Thread(target=_rns_main, args=(bt_socket_wrapper,), daemon=True).start()
    _start_done.wait(30)
    return _start_result["addr"] or _start_result["error"] or "Timeout"

def _rns_main(bt_socket_wrapper):
    global destination, image_destination, lxmf_router, reticulum
    try:
        configure_rnode(bt_socket_wrapper)
        time.sleep(2.0)

        configdir = "/data/data/com.example.rnshello/files/.reticulum"
        os.makedirs(configdir, exist_ok=True)
        with open(os.path.join(configdir, "config"), "w") as f:
            f.write(RNS_CONFIG)

        reticulum = RNS.Reticulum(configdir=configdir, loglevel=RNS.LOG_DEBUG)

        iface = AndroidBTInterface(RNS.Transport, "RNodeBT", bt_socket_wrapper)
        RNS.Transport.interfaces.append(iface)
        iface.start_reading()

        # Identity
        files_dir = "/data/data/com.example.rnshello/files"
        identity_path = os.path.join(files_dir, "identity")
        if os.path.exists(identity_path):
            identity = RNS.Identity.from_file(identity_path)
        else:
            identity = RNS.Identity()
            identity.to_file(identity_path)

        lxmf_router = LXMF.LXMRouter(storagepath=os.path.join(files_dir, "lxmf"), autopeer=True)
        destination = lxmf_router.register_delivery_identity(identity, display_name="RNS Hello")
        destination.set_proof_strategy(RNS.Destination.PROVE_ALL)
        destination.set_link_established_callback(lambda link: RNS.log("LXMF link established"))
        lxmf_router.register_delivery_callback(message_received)

        # Image destination
        image_destination = RNS.Destination(
            identity, RNS.Destination.IN, RNS.Destination.SINGLE, "rnshello", "image"
        )
        image_destination.set_proof_strategy(RNS.Destination.PROVE_ALL)
        image_destination.set_link_established_callback(image_link_established)
        image_destination.announce()

        RNS.Transport.register_announce_handler(AnnounceHandler())
        RNS.Transport.register_announce_handler(ImageAnnounceHandler())

        destination.announce()
        addr = RNS.prettyhexrep(destination.hash).strip("<>")
        _start_result["addr"] = addr
        RNS.log(f"RNS started. Address: {addr}")

    except Exception as e:
        import traceback
        RNS.log(f"RNS start error: {e}\n{traceback.format_exc()}")
        _start_result["error"] = str(e)
    finally:
        _start_done.set()

# ── Other functions (kept from your original) ───────────────────────────────
# announce, send_message, get_messages, get_announces, get_address,
# save_contact, delete_contact, get_contacts, resolve_name,
# get_rnode_config, save_rnode_config, etc.

# (Add them back from your original file if missing — they were unchanged)

class AndroidBTInterface(Interface):
    # ... your original AndroidBTInterface class unchanged ...
    pass

class AnnounceHandler:
    aspect_filter = "lxmf.delivery"
    def received_announce(self, destination_hash, announced_identity, app_data):
        announce_received(destination_hash, announced_identity, app_data)

class ImageAnnounceHandler:
    aspect_filter = "rnshello.image"
    def received_announce(self, destination_hash, announced_identity, app_data):
        # your original ImageAnnounceHandler logic
        img_hash = RNS.prettyhexrep(destination_hash).strip("<>")
        try:
            lxmf_dest = RNS.Destination(
                announced_identity,
                RNS.Destination.OUT,
                RNS.Destination.SINGLE,
                "lxmf", "delivery"
            )
            lxmf_hash = RNS.prettyhexrep(lxmf_dest.hash).strip("<>")
            with _data_lock:
                image_peer_hashes[lxmf_hash] = img_hash
                known_identities[lxmf_hash] = announced_identity
        except Exception as e:
            RNS.log(f"ImageAnnounceHandler error: {e}")

def announce_received(destination_hash, announced_identity, app_data):
    # your original announce logic
    pass

# ... rest of your helper functions (clear_reticulum_storage, etc.) ...

# Load on import
RNS.log("rns_worker.py loaded with Resource image support")