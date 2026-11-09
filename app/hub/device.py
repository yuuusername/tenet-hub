"""Talking to the TENET 70's vendor interface.

Hard rule (learned 2026-11-05): never send a vendor command before the previous one has been
answered. Flooding the keyboard overflowed its input buffer and jammed its command channel until
the firmware was re-flashed. Every packet here goes through Keyboard.cmd(), which holds a lock and
waits for the reply.
"""
import threading, time

VID, PID = 0x3311, 0xF005
VENDOR_USAGE_PAGE = 0xFF00
MODEL_TENET70 = 0x0C


def hid_module():
    import hid
    return hid


def enumerate_keyboards():
    """Returns ('app', path) / ('iap', path) / (None, None)."""
    hid = hid_module()
    for d in hid.enumerate(VID, PID):
        if "IAP" in (d.get("product_string") or ""):
            return "iap", d["path"]
    for d in hid.enumerate(VID, PID):
        if d["usage_page"] == VENDOR_USAGE_PAGE:
            return "app", d["path"]
    return None, None


class FirmwareInfo:
    """What the keyboard is running, worked out from which commands it answers."""

    def __init__(self, kind, patch=None, exact=False):
        self.kind = kind          # "official" | "patch"
        self.patch = patch        # patch number (lower bound when not exact)
        self.exact = exact

    def at_least(self, n):
        return self.kind == "patch" and self.patch is not None and self.patch >= n

    def __str__(self):
        if self.kind != "patch":
            return "Official firmware (no patch)"
        return f"REVIVER v{self.patch}" + ("" if self.exact else "+ (older REVIVER)")


class Keyboard:
    def __init__(self, path):
        hid = hid_module()
        self.dev = hid.device()
        self.dev.open_path(path)
        self.dev.set_nonblocking(0)
        self.lock = threading.RLock()
        while self.dev.read(64, timeout_ms=20):      # drop anything stale
            pass

    def close(self):
        try:
            self.dev.close()
        except Exception:
            pass

    def cmd(self, sub, payload=b"", timeout=300, group=0x70):
        """One packet (70-xx unless `group` says otherwise); waits for the ACK. Returns the reply
        payload or None."""
        p = bytes([0x7B, 0x12, 0x31, 0x00, 2 + len(payload), 0, 0, 0x7D, group, sub]) + bytes(payload)
        with self.lock:
            self.dev.write(b"\x00" + p.ljust(64, b"\x00"))
            end = time.time() + timeout / 1000
            while True:
                left = int((end - time.time()) * 1000)
                if left <= 0:
                    return None
                r = bytes(self.dev.read(64, timeout_ms=left) or b"")
                if not r:
                    return None
                if r[8] in (group, group ^ 0x80) and r[9] == sub:      # (reads come back either way)
                    return r[10:10 + r[4] - 2]
                # an unrelated report (e.g. the keyboard talking to Hexcore Link): keep waiting

    def lights(self, rgb):
        """Every key one colour (stock 20 03, what Hexcore Link sends; (0, 0, 0) = off). Live only:
        the keyboard goes back to its own lighting when it powers on. -> True if accepted."""
        r = self.cmd(0x03, bytes([0xFF, 0x01] + [max(0, min(255, int(c))) for c in rgb]), group=0x20)
        return r is not None and r[:1] == b"\x00"

    def brightness(self):
        """Key light brightness 0-100 (stock A0 15), or None."""
        r = self.cmd(0x15, group=0xA0)
        return r[0] if r else None

    def set_brightness(self, level):
        """Stock 20 15: the keyboard saves it in its flash, so don't send it more than needed."""
        level = max(0, min(100, int(level)))
        r = self.cmd(0x15, bytes([level]), group=0x20)
        return r is not None and len(r) >= 1

    # --- firmware identification ---------------------------------------------------------
    def probe(self):
        """Sub-commands above 0x0A are ignored by the official firmware, so these are safe."""
        r = self.cmd(0x14)                                   # v13+: patch version
        if r and len(r) >= 4 and r[0] == 0 and r[1:3] == b"TH":
            return FirmwareInfo("patch", r[3], exact=True)
        if self.cmd(0x12) is not None:                       # v6..v12: mode get
            return FirmwareInfo("patch", 6)
        if self.cmd(0x10, bytes([0, 0, 1, 0])) is not None:  # v4/v5: band buffer
            return FirmwareInfo("patch", 4)
        if self.cmd(0x0C, bytes([1, 0xE3])) is not None:     # v2/v3: raw OLED (NOP)
            return FirmwareInfo("patch", 2)
        if self.brightness() is None:                        # not even a stock command answered: the
            return None                                      # keyboard isn't talking yet (just plugged in /
        return FirmwareInfo("official")                      # woke up / another app had it), not "official"

    # --- helpers used by the host ---------------------------------------------------------
    def mode(self):
        r = self.cmd(0x12)
        return r[1] if r and len(r) >= 2 and r[0] == 0 else None

    def screens(self, heartbeat=True):
        """REVIVER v21: -> (mode, current screen code, hub alive, [list]) or None. With heartbeat,
        also tells the keyboard TENET Hub is running (package screens stay in the cycle 5 s)."""
        r = self.cmd(0x20 if heartbeat else 0x1F)
        if not r or r[0] != 0 or len(r) < 5 or len(r) < 5 + r[4]:
            return None
        return r[1], r[2], bool(r[3]), list(r[5:5 + r[4]])

    def set_screens(self, codes):
        r = self.cmd(0x21, bytes([len(codes)] + list(codes)), timeout=3000)
        return r is not None and r[:1] == b"\x00"

    def show_screen(self, code):
        r = self.cmd(0x22, bytes([code]))
        return r is not None and r[:1] == b"\x00"

    # --- stock commands (what Hexcore Link sends; the official firmware answers them too) ------
    def set_time(self, dt):
        """70 07: month, day, weekday (Mon=1..Sun=7), hour, minute, second. The keyboard keeps
        counting from there; the Dashboard shows time/date/day once it has been set."""
        r = self.cmd(0x07, bytes([dt.month, dt.day, dt.isoweekday(), dt.hour, dt.minute, dt.second]))
        return r is not None and r[:1] == b"\x00"

    def set_clock_12h(self, twelve):
        """70 0A: 1 = 12-hour clock, 0 = 24-hour. Saved in the keyboard's settings (a flash write)."""
        r = self.cmd(0x0A, bytes([1 if twelve else 0]))
        return r is not None and r[:1] == b"\x00"

    def set_mode(self, m):
        return self.cmd(0x13, bytes([m])) == b"\x00"

    def oled(self, *command_bytes):
        """Raw OLED controller command (REVIVER v2+)."""
        return self.cmd(0x0C, bytes([len(command_bytes)]) + bytes(command_bytes)) == b"\x00"

    def set_i2c(self, sh, sl):
        return self.cmd(0x0E, bytes([sh & 255, sh >> 8, sl & 255, sl >> 8])) == b"\x00"
