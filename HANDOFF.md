# Handoff — XDJ-XZ v1.24 Pro DJ Link peer registration

## ✅ RESOLVED — 2026-04-17

The XDJ-XZ now unicasts `CDJ-Status` packets to our vcdj **indefinitely** (verified 168+ seconds of continuous flow, live track switch + crossfade + mode transitions driving DMX/Govee). End-to-end pipeline is live.

### The root cause

Real rekordbox answers the XDJ's `rekordbox_hello` ping every ~5s with a `rekordbox_reply`. Our vcdj silently dropped every hello (eatStatus logged `"Received rekordbox_hello status packet from player 1, ignoring"`). After ~12 unanswered hellos (~60 seconds), the XDJ de-trusted our peer and stopped unicasting status — even though we were still sending keepalives *and* DJM-class status packets every 200ms, and the XDJ still showed up in our client list via keepalives on 50000.

The symptom exactly matched the original handoff: 50000 keepalives ✓, 50001 beats ✓, 50002 status ✗. The missing piece wasn't packet format or routing — it was a request/response step we weren't participating in.

### The fix (two small edits, both in the vendored `python-prodj-link`)

**`dj-lights/python-prodj-link/prodj/core/prodj.py::handle_status_packet`** — intercept inbound `rekordbox_hello` (type `0x10`) before `eatStatus` drops it, and hand off to vcdj so it can reply:

```python
if packet.type == "rekordbox_hello":
    try:
        self.vcdj.send_rekordbox_reply(addr[0])
    except Exception as e:
        logging.warning("Failed to send rekordbox_reply to %s: %s", addr[0], e)
```

**`dj-lights/python-prodj-link/prodj/core/vcdj.py::send_rekordbox_reply`** — build a `rekordbox_reply` (type `0x11`) with our model name as 256-byte UTF-16-BE, pad to declared length (`header 38 + remaining_bytes 0x104 = 298` bytes), unicast from port 50002 to the hello sender's IP.

The XDJ doesn't check the *name* we send — it just verifies the reply is well-formed and keyed to its hello. Sending `"rekordbox"` is fine.

### Why this was invisible from the wire

The rekordbox_hello packet is only **38 bytes** — no obvious "please respond" flag, and the prodj-link library already parsed it correctly. It just had no default responder. No existing doc (beat-link, dysentery, prodj-link) called this out as a required handshake step; you only discover it when you watch status flow die at exactly 60s while hellos keep arriving.

### Enduring Pro DJ Link rules (learned the hard way)

Keep these in mind any time you touch `vcdj.py`:

| Rule | Why |
|---|---|
| Announce as `device_type = "rekordbox"`, `player_number = 17` (0x11), `stype_status_mixer`, `is_rekordbox=True` | XDJ-XZ segregates peer types; pn=5/CDJ-class announcements are silently dropped. See `send_keepalive_packet`. |
| Send DJM-class status (type `0x29`) every 200ms, not CDJ-class, when model=`rekordbox` | Matches what a real rekordbox transmits to a DJM/XDJ. `u2=1`, `remaining_bytes=0x14`. |
| Unicast status from source port **50002** to every peer IP | Pro DJ Link convention. Peers ignore replies from ephemeral ports. |
| **Answer every `rekordbox_hello` (0x10) with a `rekordbox_reply` (0x11)** | ← this handoff. Missing replies → XDJ de-trusts us after ~60s. |
| Pin the vcdj keepalive/broadcast socket to the right NIC via `IP_BOUND_IF = 25` on macOS | macOS can have two link-local routes (en0 + en8); without `IP_BOUND_IF`, broadcasts leave the wrong NIC. |
| If you add new outbound sockets, set `SO_REUSEADDR + SO_REUSEPORT` | `status_sock` at 50002 can otherwise collide with probes/test scripts. |

See also: `dj-lights-mvp2/README.md` (running + dashboard) and code comments in `vcdj.py` / `prodj.py` that call out each rule inline.

---

## Original problem statement (archived — kept for historical context)

Our virtual CDJ (vcdj) can't get the XDJ-XZ to send **status packets** on port 50002. Without them, we can't identify tracks or fetch PSSI phrase data.

Everything *else* in the stack works. Do not rebuild or re-verify it — see "Already Working" at the bottom.

## Current state of the wire (tcpdump verified)

- `169.254.20.137` = XDJ-XZ, `169.254.160.162` = our Mac, en8 link-local
- Port **50000**: XDJ broadcasts 3 keepalives every ~2s (one per virtual player 1/2/33). ✓
- Port **50001**: beat packets flow when a deck is playing — we parse `bpm` correctly. ✓
- Port **50002**: **zero inbound packets from XDJ**, regardless of what we send. ✗

## What's been tried

### ✅ Already done in the previous two sessions

| Attempt | Result |
|---|---|
| Pass MAC as string to `set_interface_data` (was a list — library crashed silently on every keepalive) | Keepalives now build and send cleanly |
| `vcdj_set_player_number(4)` instead of 5 | No change |
| Unicast the phantom-mixer packet to XDJ IP instead of broadcast | No change |
| **Replace the 26-byte phantom mixer packet (type 0x29) with a real CDJ-Status packet (type 0x0a) built via `packets.StatusPacket`** | Packet builds cleanly (205 bytes, verified round-trip via `StatusPacket.parse`). Unicast every 200ms from `status_sock` (source port 50002) to every discovered peer IP. **XDJ still silent on 50002.** |

The current `send_status_packet` in [dj-lights/python-prodj-link/prodj/core/vcdj.py:82](dj-lights/python-prodj-link/prodj/core/vcdj.py:82) builds a dysentery-spec CDJ-Status (header `Qspt1WmJOL` + `0x0a` + model `"rekordbox"` + player_number 5 + state flag byte 0x84 at offset 0x89). It's a proper peer-registration packet, not the old cargo-cult 26-byte blob.

### ❌ New symptom: macOS routing confusion

During the live test, about 8 seconds after the vcdj starts sending status packets, the keepalive broadcast starts failing with `[Errno 65] No route to host` → `[Errno 64] Host is down`. `netstat -rn -f inet` shows TWO `169.254.0.0/16` routes:

```
169.254            link#14            UCS                   en0      !
169.254            link#26            UCSI                  en8      !
169.254.20.137     c8:3d:fc:11:14:89  UHLSW                 en8   1180   ← unicast works
169.254.255.255    link#14            UHRLSW                en0      !   ← broadcast picks en0 (wrong iface)
```

**Theory:** `status_sock` bound to `0.0.0.0:50002` may pick en0 (unplugged/wrong) for outbound unicast too, so our status packets might be leaving the wrong NIC. That would explain why XDJ never sees them. `_send_sock` (bound to our specific interface IP) does NOT have this issue — but its source port is ephemeral, not 50002, and Pro DJ Link convention requires source port 50002.

## The actual task

Make XDJ-XZ v1.24 actually accept our packet so it starts sending status back on 50002. Concrete next steps, roughly ordered cheapest-first:

1. **Fix the interface-binding issue first.** In `vcdj.Vcdj.set_interface_data`, create a dedicated send socket bound to `(self.ip_addr, 50002)` with `SO_REUSEADDR + SO_REUSEPORT`, and ALSO set those flags on `self.prodj.status_sock` (in [prodj.py](dj-lights/python-prodj-link/prodj/core/prodj.py:45)) so both can coexist. Then use this new socket in `send_status_packet` instead of `status_sock`. This guarantees packets leave en8 with source port 50002. Also flush the bad route: `sudo route delete 169.254.0.0/16 -ifscope en0`.

2. **Confirm with tcpdump** that our packet actually reaches the XDJ. Run `sudo tcpdump -i en8 -vvv -X udp and port 50002` while probe is running. If you see our packet leaving but nothing coming back, the packet format is still wrong. If you don't see our packet, the routing theory is correct.

3. **Try keepalive `device_type = "rekordbox"` (3) instead of default `"cdj"` (2)** to match our `model = "rekordbox"`. Currently `KeepAlivePacket` in [packets.py:66](dj-lights/python-prodj-link/prodj/network/packets.py:66) has `Default(DeviceType, "cdj")`. Override in `send_keepalive_packet` by adding `"device_type": "rekordbox"` to the `data` dict. XDJ-XZ might filter peers by the announced device type.

4. **Try `u2 = 3` at status-packet offset 0x20** instead of our current 1. Beat-link docs call this byte "subtype" and say 0x03 for CDJ-class packets. The prodj-link comment calls it a "revision". One of them is right for XDJ-XZ.

5. **Pad the status packet to 212 bytes.** Our current output is 205 bytes (at minimum); `remaining_bytes=0xf8` claims 248 bytes of payload. That's a 43-byte mismatch. Either shrink `remaining_bytes` to match actual length (`0xa9`) or pad the output with zeros. Dysentery reference is 212 bytes.

6. **If all else fails, capture what real rekordbox sends** using Wireshark on a machine running Pioneer's rekordbox against the XDJ-XZ, and byte-for-byte match the first status packet. beat-link issue tracker has some dumps: https://github.com/Deep-Symmetry/beat-link/issues

## Verification protocol

1. Ensure XDJ ethernet plugged in, USB with Rekordbox export in slot 1, track loaded on deck 1.
2. Terminal A (sudo needed for tcpdump):
   ```bash
   sudo tcpdump -i en8 -vvv -X 'udp and port 50002'
   ```
3. Terminal B:
   ```bash
   cd /Users/ethanhouseworth/Documents/Personal-Projects/dj-lighting-ultron
   .venv/bin/python /tmp/prodj_probe.py
   ```
4. When prompted, press PLAY on deck 1.
5. **Success criteria**: tcpdump shows packets from `169.254.20.137:50002 → 169.254.160.162:50002`; probe prints state lines with `track=<non-zero>`, `slot=usb1`, `play=playing`.
6. **Cleanup if probe hangs on port 50002**: `lsof -iUDP:50002 -t | xargs -r kill -9`.

`/tmp/prodj_probe.py` is current (uses `pn=5`, relies on the new status-packet fix in vcdj.py). Safe to rewrite.

## Key files

- [dj-lights-mvp2/main.py](dj-lights-mvp2/main.py) — Pro DJ Link consumer + mode dispatcher. Interface is `169.254.160.162` (verify with `ifconfig en8` — may drift on reboot).
- [dj-lights/python-prodj-link/prodj/core/vcdj.py](dj-lights/python-prodj-link/prodj/core/vcdj.py) — the fix lives here. `send_status_packet` (line 82) builds via `packets.StatusPacket`, unicasts every 200ms. Run-loop uses `status_interval=0.2` with keepalive every 8th tick. Already heavily diverged from upstream.
- [dj-lights/python-prodj-link/prodj/network/packets.py](dj-lights/python-prodj-link/prodj/network/packets.py) — `StatusPacket` construct. `MacAddrAdapter` requires MAC as string `"aa:bb:cc:dd:ee:ff"`, not a list (line 16).
- [dj-lights/python-prodj-link/prodj/core/prodj.py](dj-lights/python-prodj-link/prodj/core/prodj.py:45) — where `status_sock` is created at 0.0.0.0:50002. Needs SO_REUSEADDR/REUSEPORT if you add a dedicated bound-to-interface send sock.

## Environment

- Python 3.13 venv at `.venv/`. Do not try 3.14 (no wheels for netifaces/pyaudio).
- Interface may drift on each reboot: `ifconfig | grep 169.254` to find current Mac IP on en8.
- macOS may create a phantom `169.254/16` route via en0 — inspect with `netstat -rn -f inet`; delete with `sudo route delete 169.254.0.0/16 -ifscope en0` if present.
- If a hung probe holds port 50002: `lsof -iUDP:50002 -t | xargs -r kill -9`.
- XDJ-XZ on firmware **v1.24**. Confirmed reachable via ICMP (`ping 169.254.20.137`) and by observing its keepalives on 50000.

## Already working — leave alone

- DMX: 2x Tetra 12 @ D001 + Tetra Bar @ D008 via VenueLink wireless (universe C7/white on both dongles). `dj-lights/dmx_controller.py test` cycles all fixtures.
- Govee: 8 devices cached, 4x H61E5 strips on LAN (~10ms), 4x H6010 bulbs on cloud (~200-500ms). CLI at `~/.local/bin/govee`.
- Scenes: mapped per mode in [dj-lights-mvp2/direct_lights.py:29](dj-lights-mvp2/direct_lights.py:29). Tested end-to-end.
- `direct_lights.apply_mode(mode)` is the integration point — once status packets work, existing PSSI → mode logic in `main.py` will drive it.

Once the packet fix lands, run `.venv/bin/python dj-lights-mvp2/main.py` with a track loaded and watch `[track] ... phrases` and `[mode] ... -> drop` log lines start flowing.
