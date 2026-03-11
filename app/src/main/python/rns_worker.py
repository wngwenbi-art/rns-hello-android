import RNS
import LXMF
import threading
import signal
import os
import time
import struct
from RNS.Interfaces.Interface import Interface

destination = None
lxmf_router = None
reticulum = None
_rns_started = False
_start_done = threading.Event()
_start_result = {"addr": None, "error": None}

RNS_CONFIG = """
[reticulum]
  enable_transport = False
  share_instance = False
  shared_instance_port = 37428
  instance_control_port = 37429
  panic_on_interface_error = False

[interfaces]

"""

# ---------------------------------------------------------------
# RNode KISS protocol constants
# ---------------------------------------------------------------
KISS_FEND         = 0xC0
KISS_FESC         = 0xDB
KISS_TFEND        = 0xDC
KISS_TFESC        = 0xDD

CMD_DATA          = 0x00
CMD_FREQUENCY     = 0x01
CMD_BANDWIDTH     = 0x02
CMD_TXPOWER       = 0x03
CMD_SF            = 0x04
CMD_CR            = 0x05
CMD_RADIO_STATE   = 0x06
CMD_RADIO_LOCK    = 0x07
CMD_DETECT        = 0x08
CMD_IMPLICIT      = 0x09
CMD_LEAVE         = 0x0A

RADIO_STATE_OFF   = 0x00
RADIO_STATE_ON    = 0x01

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
    payload = bytes([KISS_FEND, cmd]) + kiss_escape(data) + bytes([KISS_FEND])
    return payload

def configure_rnode(socket):
    """
    Send RNode KISS commands to set:
      Frequency : 433.025 MHz
      Bandwidth : 31.25 kHz
      TX Power  : 17 dBm
      SF        : 8
      CR        : 6
    Then turn the radio ON.
    """
    RNS.log("Configuring RNode radio parameters...")

    # Frequency: 433025000 Hz as 4-byte big-endian uint32
    freq = 433025000
    freq_bytes = struct.pack(">I", freq)
    socket.write(kiss_cmd(CMD_FREQUENCY, freq_bytes))
    time.sleep(0.1)

    # Bandwidth: 31250 Hz as 4-byte big-endian uint32
    bw = 31250
    bw_bytes = struct.pack(">I", bw)
    socket.write(kiss_cmd(CMD_BANDWIDTH, bw_bytes))
    time.sleep(0.1)

    # TX Power: 17 dBm as single byte
    socket.write(kiss_cmd(CMD_TXPOWER, bytes([17])))
    time.sleep(0.1)

    # Spreading Factor: 8
    socket.write(kiss_cmd(CMD_SF, bytes([8])))
    time.sleep(0.1)

    # Coding Rate: 6 (means 4/6 in LoRa terms)
    socket.write(kiss_cmd(CMD_CR, bytes([6])))
    time.sleep(0.1)

    # Turn radio ON
    socket.write(kiss_cmd(CMD_RADIO_STATE, bytes([RADIO_STATE_ON])))
    time.sleep(0.5)

    RNS.log("RNode radio configured and ON")


class AndroidBTInterface(Interface):
    BITRATE_GUESS = 1200   # ~1.2 kbps at SF8/BW31.25/CR6

    def __init__(self, owner, name, socket):
        self.name                  = name
        self.rxb                   = 0
        self.txb                   = 0
        self.online                = False
        self.IN                    = True
        self.OUT                   = True
        self.FWD                   = False
        self.RPT                   = False
        self.owner                 = owner
        self._socket               = socket
        self.bitrate               = self.BITRATE_GUESS
        self.ingress_control       = False
        self.ic_max_held_announces = 0
        self.ic_burst_hold_time    = 0
        self.ic_burst_freq_new     = 0
        self.ic_burst_freq         = 0
        self.announce_cap          = 2
        self.announce_queue        = []
        self.announced_identity    = None
        self.mode                  = Interface.MODE_FULL
        self._kiss_buf             = []
        self._in_frame             = False
        self._escape               = False
        self.online                = True
        threading.Thread(target=self._read_loop, daemon=True).start()

    def _read_loop(self):
        while self.online:
            try:
                data = self._socket.read(512)
                if data and len(data) > 0:
                    self.rxb += len(data)
                    self._parse_kiss(data)
            except Exception as e:
                RNS.log(f"BT read error: {e}")
                self.online = False

    def _parse_kiss(self, data):
        """Parse incoming KISS-framed data and pass payloads to RNS."""
        for byte in data:
            if byte == KISS_FEND:
                if self._in_frame and len(self._kiss_buf) > 1:
                    # First byte is command, rest is payload
                    if self._kiss_buf[0] == CMD_DATA:
                        self.processIncoming(bytes(self._kiss_buf[1:]))
                self._kiss_buf = []
                self._in_frame = True
                self._escape = False
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

    def processOutgoing(self, data):
        try:
            # Wrap outgoing RNS packets in KISS DATA frame
            frame = kiss_cmd(CMD_DATA, data)
            self._socket.write(frame)
            self.txb += len(data)
        except Exception as e:
            RNS.log(f"BT write error: {e}")


def message_received(message):
    RNS.log(f"MSG from {RNS.prettyhexrep(message.source_hash)}: {message.content_as_string()}")

def _noop_signal(sig, handler):
    pass

def _rns_main(bt_socket_wrapper):
    global destination, lxmf_router, reticulum
    try:
        # Configure RNode radio FIRST before starting RNS
        configure_rnode(bt_socket_wrapper)

        configdir = "/data/data/com.example.rnshello/files/.reticulum"
        os.makedirs(configdir, exist_ok=True)
        with open(os.path.join(configdir, "config"), "w") as f:
            f.write(RNS_CONFIG)

        original_signal = signal.signal
        signal.signal = _noop_signal
        reticulum = RNS.Reticulum(configdir=configdir, loglevel=RNS.LOG_DEBUG)
        signal.signal = original_signal

        iface = AndroidBTInterface(reticulum, "RNodeBT", bt_socket_wrapper)
        RNS.Transport.interfaces.append(iface)

        identity = RNS.Identity()
        lxmf_router = LXMF.LXMRouter(
            storagepath="/data/data/com.example.rnshello/files/lxmf",
            autopeer=True
        )
        destination = lxmf_router.register_delivery_identity(
            identity,
            display_name="RNS Hello Android"
        )
        lxmf_router.register_delivery_callback(message_received)
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
