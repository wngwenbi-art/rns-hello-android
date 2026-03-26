import RNS
import LXMF
import os
import threading
import time
from ColumbaRNodeInterface import ColumbaRNodeInterface

# --- LORA TIMEOUT PATCHES ---
RNS.Link.ESTABLISHMENT_TIMEOUT_PER_HOP = 25.0
RNS.Link.KEEPALIVE_TIMEOUT_FACTOR = 360

_global_wrapper_instance = None

class ReticulumWrapper:
    def __init__(self, storage_path):
        global _global_wrapper_instance
        self.storage_path = storage_path
        self.router = None
        self.rnode_interface = None
        self.rns_instance = None
        self.msg_callback = None
        _global_wrapper_instance = self
        
        if not os.path.exists(storage_path):
            os.makedirs(storage_path)

        config_path = os.path.join(storage_path, "config")
        with open(config_path, "w") as f:
            f.write("[reticulum]\n")
            f.write("enable_transport = False\n")
            f.write("share_instance = No\n")
            f.write("is_gateway = No\n")
            f.write("[logging]\n")
            f.write("loglevel = 4\n")
            f.write("[interfaces]\n")

        try:
            self.rns_instance = RNS.Reticulum(configdir=storage_path)
            RNS.log(f"RNS Core started at {storage_path}")
        except Exception as e:
            RNS.log(f"RNS Startup Error: {e}", RNS.LOG_ERROR)

    # 1. Store the Kotlin Callback object
    def set_callback(self, callback):
        self.msg_callback = callback

    def set_bridge(self, bridge):
        if self.rns_instance is None: 
            return
            
        self.rnode_interface = ColumbaRNodeInterface(
            owner=self.rns_instance,
            name="RNode_BT",
            bridge=bridge,
            frequency=433025000,
            sf=8
        )
        
        # Apply MTU & Bitrate fixes for LoRa
        self.rnode_interface.HW_MTU = 500
        self.rnode_interface.bitrate = 732
        
        RNS.Transport.interfaces.append(self.rnode_interface)
        threading.Thread(target=self.rnode_interface.read_loop, daemon=True).start()

    # 2. The Receive Function (Called by LXMF)
    def on_message_received(self, message):
        try:
            sender = RNS.prettyhexrep(message.source_hash).replace("<", "").replace(">", "")
            
            text = ""
            try:
                text = message.content_as_string()
            except:
                pass
            
            img_path = ""
            if message.fields:
                img_bytes = message.fields.get(6) or message.fields.get("6") or message.fields.get("ia")
                if img_bytes:
                    if isinstance(img_bytes, memoryview): img_bytes = bytes(img_bytes)
                    # Extract list wrappers if needed
                    if isinstance(img_bytes, list) and len(img_bytes) > 0:
                        img_bytes = bytes(img_bytes[0]) if isinstance(img_bytes[0], (bytes, bytearray)) else bytes(img_bytes)
                        
                    if len(img_bytes) > 4:
                        img_dir = os.path.join(self.storage_path, "images")
                        os.makedirs(img_dir, exist_ok=True)
                        fname = f"img_{sender[:8]}_{int(time.time())}.webp"
                        img_path = os.path.join(img_dir, fname)
                        
                        # 3. File-First Processing: Save to Android storage instantly
                        with open(img_path, "wb") as f:
                            f.write(img_bytes)
            
            # 4. Trigger Kotlin Callback
            if self.msg_callback:
                self.msg_callback.onMessageReceived(sender, text, img_path)
        except Exception as e:
            RNS.log(f"Receive error: {e}")

    def start_lxmf(self, user_name):
        if self.rns_instance is None: return "ERROR"
        self.router = LXMF.LXMRouter(storagepath=self.storage_path, display_name=user_name)
        self.local_identity = self.router.get_identity()
        
        # Register our receive function!
        self.router.register_delivery_callback(self.on_message_received)
        return RNS.hexrep(self.local_identity.hash)

    def send_message(self, dest_hash_hex, content, image_bytes=None):
        if not self.router: return False
        try:
            clean_hex = dest_hash_hex.replace("<", "").replace(">", "").replace(" ", "")
            dest_hash = bytes.fromhex(clean_hex)
            
            # Path Request loop for LoRa
            if not RNS.Transport.has_path(dest_hash):
                RNS.Transport.request_path(dest_hash)
                wait_start = time.time()
                while not RNS.Transport.has_path(dest_hash) and (time.time() - wait_start) < 15.0:
                    time.sleep(0.5)
            
            destination = RNS.Destination(None, RNS.Destination.OUT, RNS.Destination.SINGLE, "lxmf", "delivery")
            destination.hash = dest_hash
            
            lxmf_msg = LXMF.LXMessage(
                destination, 
                self.local_identity, 
                content, 
                title="RNS Mesh Transfer", 
                fields={6: image_bytes} if image_bytes else None
            )
            self.router.handle_outbound(lxmf_msg)
            return True
        except Exception as e:
            RNS.log(f"Send error: {e}")
            return False

    def announce_now(self):
        if self.router:
            self.router.announce()
            return True
        return False

def get_instance(storage_path=None):
    global _global_wrapper_instance
    if _global_wrapper_instance is None:
        _global_wrapper_instance = ReticulumWrapper(storage_path)
    return _global_wrapper_instance