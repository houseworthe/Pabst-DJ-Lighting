#!/usr/bin/env python3
"""
DMX Controller for Enttec Open DMX USB on macOS (libftdi).

Usage:
  python3 dmx_controller.py color ff0000          # all fixtures red
  python3 dmx_controller.py color 0000ff --dimmer 128   # blue at 50%
  python3 dmx_controller.py blackout              # all off
  python3 dmx_controller.py test                  # cycle colors
  python3 dmx_controller.py strobe 50             # strobe at speed 50/255
  python3 dmx_controller.py amber 200             # warm amber
  python3 dmx_controller.py warm                  # warm white preset
  python3 dmx_controller.py party                 # color cycle loop
"""

import ctypes
import time
import sys
import signal

# FTDI
FTDI_LIB = '/opt/homebrew/lib/libftdi1.dylib'
FTDI_VID = 0x0403
FTDI_PID = 0x6001

# DMX Addressing — update these if fixture addresses change
# Both Tetra 12s on d.001 (channels 1-6)
# Bar on d.008 (channels 8-31, 24-ch mode, 4 zones × 6ch)
TETRA12_ADDRS = [1]  # both fixtures respond to addr 1
TETRA_BAR_ADDR = 8
TETRA_BAR_ZONES = 4

# Channel offsets (6-ch mode)
CH_R, CH_G, CH_B, CH_A, CH_DIM, CH_STROBE = 0, 1, 2, 3, 4, 5


class DMX:
    def __init__(self):
        self.ftdi = ctypes.CDLL(FTDI_LIB)

        class ftdi_context(ctypes.Structure):
            pass
        self.ftdi.ftdi_new.restype = ctypes.POINTER(ftdi_context)
        self.ctx = self.ftdi.ftdi_new()
        self._break = (ctypes.c_ubyte * 1)(0)
        self.data = bytearray(513)

    def open(self):
        ret = self.ftdi.ftdi_usb_open(self.ctx, FTDI_VID, FTDI_PID)
        if ret < 0:
            self.ftdi.ftdi_get_error_string.restype = ctypes.c_char_p
            err = self.ftdi.ftdi_get_error_string(self.ctx)
            raise RuntimeError(f"Could not open FTDI: {err}")
        self.ftdi.ftdi_set_baudrate(self.ctx, 250000)
        self.ftdi.ftdi_set_line_property(self.ctx, 8, 2, 0)
        self.ftdi.ftdi_setflowctrl(self.ctx, 0)
        self.ftdi.ftdi_setrts(self.ctx, 0)

    def close(self):
        try:
            self.ftdi.ftdi_usb_close(self.ctx)
        except Exception:
            pass
        try:
            self.ftdi.ftdi_free(self.ctx)
        except Exception:
            pass

    def send_frame(self):
        buf = (ctypes.c_ubyte * len(self.data))(*self.data)
        self.ftdi.ftdi_set_baudrate(self.ctx, 57600)
        self.ftdi.ftdi_write_data(self.ctx, self._break, 1)
        self.ftdi.ftdi_set_baudrate(self.ctx, 250000)
        self.ftdi.ftdi_write_data(self.ctx, buf, len(self.data))

    def send_for(self, seconds):
        start = time.time()
        while time.time() - start < seconds:
            self.send_frame()
            time.sleep(0.023)

    def send_hold(self):
        """Send continuously until interrupted."""
        try:
            while True:
                self.send_frame()
                time.sleep(0.023)
        except KeyboardInterrupt:
            pass

    def set_ch(self, channel, value):
        if 1 <= channel <= 512:
            self.data[channel] = max(0, min(255, int(value)))

    def blackout(self):
        self.data = bytearray(513)

    def set_fixture(self, addr, r, g, b, a=0, dimmer=255, strobe=0):
        self.set_ch(addr + CH_R, r)
        self.set_ch(addr + CH_G, g)
        self.set_ch(addr + CH_B, b)
        self.set_ch(addr + CH_A, a)
        self.set_ch(addr + CH_DIM, dimmer)
        self.set_ch(addr + CH_STROBE, strobe)

    def set_all(self, r, g, b, a=0, dimmer=255, strobe=0):
        for addr in TETRA12_ADDRS:
            self.set_fixture(addr, r, g, b, a, dimmer, strobe)
        for zone in range(TETRA_BAR_ZONES):
            self.set_fixture(TETRA_BAR_ADDR + zone * 6, r, g, b, a, dimmer, strobe)

    def set_12s(self, r, g, b, a=0, dimmer=255, strobe=0):
        for addr in TETRA12_ADDRS:
            self.set_fixture(addr, r, g, b, a, dimmer, strobe)

    def set_bar(self, r, g, b, a=0, dimmer=255, strobe=0):
        for zone in range(TETRA_BAR_ZONES):
            self.set_fixture(TETRA_BAR_ADDR + zone * 6, r, g, b, a, dimmer, strobe)

    def set_bar_zone(self, zone, r, g, b, a=0, dimmer=255, strobe=0):
        self.set_fixture(TETRA_BAR_ADDR + (zone - 1) * 6, r, g, b, a, dimmer, strobe)

    def set_all_dimmer(self, level):
        for addr in TETRA12_ADDRS:
            self.set_ch(addr + CH_DIM, level)
        for zone in range(TETRA_BAR_ZONES):
            self.set_ch(TETRA_BAR_ADDR + zone * 6 + CH_DIM, level)

    def set_all_strobe(self, speed):
        for addr in TETRA12_ADDRS:
            self.set_ch(addr + CH_STROBE, speed)
        for zone in range(TETRA_BAR_ZONES):
            self.set_ch(TETRA_BAR_ADDR + zone * 6 + CH_STROBE, speed)


def hex_to_rgb(h):
    h = h.lstrip('#')
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


# ===== PRESETS =====

PRESETS = {
    'warm':    (255, 180, 80, 200, 200),   # r,g,b,a,dimmer
    'cool':    (100, 150, 255, 0, 200),
    'red':     (255, 0, 0, 0, 255),
    'green':   (0, 255, 0, 0, 255),
    'blue':    (0, 0, 255, 0, 255),
    'purple':  (128, 0, 255, 0, 255),
    'pink':    (255, 0, 128, 0, 255),
    'cyan':    (0, 255, 255, 0, 255),
    'orange':  (255, 80, 0, 128, 255),
    'amber':   (0, 0, 0, 255, 255),
}

PALETTE = [
    (148, 0, 211, 0),    # violet
    (75, 0, 130, 0),     # indigo
    (0, 0, 255, 0),      # blue
    (0, 128, 255, 0),    # cyan-blue
    (0, 255, 128, 0),    # teal
    (255, 0, 128, 0),    # hot pink
    (255, 0, 0, 0),      # red
    (255, 64, 0, 0),     # orange-red
    (255, 128, 0, 64),   # orange
    (128, 0, 255, 0),    # purple
]


def cmd_color(dmx, args):
    hex_color = args[0] if args else 'ff0000'
    r, g, b = hex_to_rgb(hex_color)
    dimmer = int(args[1]) if len(args) > 1 else 255
    dmx.set_all(r, g, b, dimmer=dimmer)
    print(f'🎨 #{hex_color} (dimmer={dimmer})')
    dmx.send_hold()


def cmd_preset(dmx, name):
    r, g, b, a, dim = PRESETS[name]
    dmx.set_all(r, g, b, a, dim)
    print(f'🎨 {name}')
    dmx.send_hold()


def cmd_blackout(dmx, args):
    dmx.blackout()
    print('⬛ Blackout')
    dmx.send_for(1)


def cmd_test(dmx, args):
    colors = [
        ('🔴 Red', 255, 0, 0, 0),
        ('🟢 Green', 0, 255, 0, 0),
        ('🔵 Blue', 0, 0, 255, 0),
        ('🟠 Amber', 0, 0, 0, 255),
        ('🟣 Purple', 128, 0, 255, 0),
        ('🩵 Cyan', 0, 255, 255, 0),
        ('💗 Hot Pink', 255, 0, 128, 0),
        ('🔥 Warm', 255, 180, 80, 200),
    ]
    for name, r, g, b, a in colors:
        print(name)
        dmx.set_all(r, g, b, a)
        dmx.send_for(2.5)
    dmx.blackout()
    dmx.send_for(0.5)
    print('✅ Done')


def cmd_party(dmx, args):
    interval = float(args[0]) if args else 3.0
    print(f'🎉 Party mode ({interval}s per color, Ctrl+C to stop)')
    i = 0
    try:
        while True:
            r, g, b, a = PALETTE[i % len(PALETTE)]
            # 12s get the color, bar gets complement
            comp = PALETTE[(i + len(PALETTE) // 2) % len(PALETTE)]
            dmx.set_12s(r, g, b, a)
            dmx.set_bar(*comp)
            dmx.send_for(interval)
            i += 1
    except KeyboardInterrupt:
        pass
    dmx.blackout()
    dmx.send_for(0.5)
    print('\n✅ Stopped')


def cmd_strobe(dmx, args):
    speed = int(args[0]) if args else 128
    dmx.set_all(255, 255, 255, 0, 255, speed)
    print(f'⚡ Strobe speed={speed}')
    dmx.send_hold()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1].lower()
    args = sys.argv[2:]

    dmx = DMX()
    running = True

    def cleanup(sig, frame):
        nonlocal running
        running = False
        dmx.blackout()
        dmx.send_for(0.5)
        dmx.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    try:
        dmx.open()

        if cmd == 'blackout':
            cmd_blackout(dmx, args)
        elif cmd == 'test':
            cmd_test(dmx, args)
        elif cmd == 'party':
            cmd_party(dmx, args)
        elif cmd == 'strobe':
            cmd_strobe(dmx, args)
        elif cmd == 'color':
            cmd_color(dmx, args)
        elif cmd in PRESETS:
            cmd_preset(dmx, cmd)
        else:
            print(f'Unknown command: {cmd}')
            print('Commands: color <hex>, test, party, strobe, blackout')
            print(f'Presets: {", ".join(PRESETS.keys())}')
    finally:
        dmx.blackout()
        dmx.send_for(0.5)
        dmx.close()


if __name__ == '__main__':
    main()
