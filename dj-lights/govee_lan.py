"""
Govee client — LAN UDP (fast, ~10ms, no rate limit) with cloud fallback.

In-process API:
    client = GoveeClient()
    client.ensure_ready()          # scan LAN once; fall back to cloud list
    client.set_color(r, g, b)
    client.set_brightness(1..100)
    client.turn(True / False)

Device cache at ~/.config/govee/devices.json — persisted LAN IPs so we skip
the 2s scan on every startup.
"""
from __future__ import annotations

import concurrent.futures
import ipaddress
import json
import os
import socket
import struct
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

try:
    import netifaces as _ni
except Exception:
    _ni = None

CONFIG_DIR = Path.home() / ".config" / "govee"
CREDS_PATH = CONFIG_DIR / "credentials.json"
DEVICES_PATH = CONFIG_DIR / "devices.json"
SCENES_PATH = CONFIG_DIR / "scenes.json"

LAN_MULTICAST = "239.255.255.250"
LAN_SCAN_PORT = 4001
LAN_PORT_RECV = 4002
LAN_PORT_SEND = 4003

CLOUD_BASE = "https://openapi.api.govee.com/router/api/v1"
CLOUD_TIMEOUT = 4.0
# Retries for transient cloud failures (429 rate-limit, 5xx, timeouts).
# Delays between attempts — 3 total attempts (initial + len(retries)).
CLOUD_RETRY_DELAYS_S = (0.2, 0.5)

# Govee devices go white or drop commands when packets overlap. Hold this gap
# between successive commands per device (LAN or cloud).
COMMAND_GAP_S = 0.08


def _api_key() -> str | None:
    try:
        return json.loads(CREDS_PATH.read_text()).get("api_key")
    except Exception:
        return None


def _load_cache() -> list[dict]:
    try:
        return json.loads(DEVICES_PATH.read_text())
    except Exception:
        return []


def _save_cache(devices: list[dict]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DEVICES_PATH.write_text(json.dumps(devices, indent=2))


def cloud_fetch_devices(api_key: str) -> list[dict]:
    headers = {"Govee-API-Key": api_key, "Content-Type": "application/json"}
    req = Request(CLOUD_BASE + "/user/devices", headers=headers, method="GET")
    with urlopen(req, timeout=CLOUD_TIMEOUT) as resp:
        data = json.loads(resp.read().decode())
    return [
        {"sku": d["sku"], "device": d["device"], "name": d.get("deviceName", ""), "ip": None}
        for d in data.get("data", [])
        if d.get("sku") not in {"SameModeGroup"} and d.get("device")
    ]


def _local_ipv4_broadcasts() -> list[tuple[str, str]]:
    """Return [(src_ip, subnet_broadcast)] for every usable IPv4 interface."""
    out: list[tuple[str, str]] = []
    if _ni is None:
        return out
    for iface in _ni.interfaces():
        try:
            addrs = _ni.ifaddresses(iface).get(_ni.AF_INET, [])
        except Exception:
            continue
        for a in addrs:
            ip, mask = a.get("addr"), a.get("netmask")
            if not ip or not mask or ip.startswith("127."):
                continue
            try:
                net = ipaddress.IPv4Network(f"{ip}/{mask}", strict=False)
            except Exception:
                continue
            if net.prefixlen >= 31:
                continue
            out.append((ip, str(net.broadcast_address)))
    return out


def lan_scan(timeout: float = 3.0) -> dict[str, dict]:
    """Broadcast-scan the LAN for Govee devices with LAN Control enabled.

    Sends the scan payload to 239.255.255.250:4001 (multicast),
    255.255.255.255:4001 (limited broadcast), and each local subnet's
    directed broadcast (e.g. 192.168.8.255). Listens on UDP 4002.
    """
    recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    recv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    recv_sock.settimeout(timeout)
    try:
        recv_sock.bind(("", LAN_PORT_RECV))
    except OSError:
        recv_sock.close()
        return {}

    scan_msg = json.dumps({"msg": {"cmd": "scan", "data": {"account_topic": "reserve"}}}).encode()
    subnets = _local_ipv4_broadcasts()

    # Global broadcast + multicast from default interface
    send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    send_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    send_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    send_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, struct.pack("b", 2))
    for dest in (LAN_MULTICAST, "255.255.255.255"):
        try:
            send_sock.sendto(scan_msg, (dest, LAN_SCAN_PORT))
        except OSError:
            pass
    send_sock.close()

    # One send per local interface, bound to that interface's source IP, to the
    # subnet-directed broadcast. This is what actually lands on Govee devices
    # when you have multiple network interfaces (en0 + utun, etc.).
    for src_ip, bcast in subnets:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(src_ip))
            s.bind((src_ip, 0))
            s.sendto(scan_msg, (bcast, LAN_SCAN_PORT))
            s.sendto(scan_msg, (LAN_MULTICAST, LAN_SCAN_PORT))
            s.close()
        except OSError:
            pass

    found: dict[str, dict] = {}
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            data, addr = recv_sock.recvfrom(4096)
        except socket.timeout:
            break
        try:
            msg = json.loads(data.decode())
            d = msg["msg"]["data"]
        except Exception:
            continue
        ip = addr[0]
        found[ip] = {
            "ip": ip,
            "device": d.get("device"),
            "sku": d.get("sku"),
        }
    recv_sock.close()
    return found


def _lan_send(ip: str, cmd: str, data: dict) -> None:
    msg = json.dumps({"msg": {"cmd": cmd, "data": data}}).encode()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.sendto(msg, (ip, LAN_PORT_SEND))
        sock.close()
    except Exception:
        pass


def _cloud_control(api_key: str, sku: str, device: str, capability: dict) -> bool:
    body = json.dumps({
        "requestId": str(uuid.uuid4()),
        "payload": {"sku": sku, "device": device, "capability": capability},
    }).encode()
    headers = {"Govee-API-Key": api_key, "Content-Type": "application/json"}
    last_err: str | None = None
    attempts = len(CLOUD_RETRY_DELAYS_S) + 1
    for attempt in range(attempts):
        req = Request(CLOUD_BASE + "/device/control", data=body, headers=headers, method="POST")
        try:
            with urlopen(req, timeout=CLOUD_TIMEOUT) as resp:
                data = json.loads(resp.read().decode())
            code = data.get("code")
            if code == 200:
                if attempt > 0:
                    print(f"[govee] cloud {device[-8:]} ok on retry #{attempt}", flush=True)
                return True
            msg = data.get("msg") or data.get("message")
            # 429 rate-limit / 5xx are transient; anything else (400 bad request,
            # 401 auth, etc.) won't get better by retrying.
            transient = code == 429 or (isinstance(code, int) and 500 <= code < 600)
            if not transient:
                print(f"[govee] cloud {device[-8:]} -> {code} {msg}", flush=True)
                return False
            last_err = f"{code} {msg}"
        except Exception as e:
            last_err = str(e)
        if attempt < attempts - 1:
            time.sleep(CLOUD_RETRY_DELAYS_S[attempt])
    print(f"[govee] cloud {device[-8:]} failed after {attempts} attempts: {last_err}", flush=True)
    return False


def cloud_fetch_scenes(api_key: str, sku: str, device: str, instance: str = "lightScene") -> list[dict]:
    """Fetch available named scenes for (sku, device). Preserves the full
    `value` structure because the control API needs both `id` and `paramId` —
    they are different numbers per scene."""
    path = "/device/diy-scenes" if instance == "diyScene" else "/device/scenes"
    body = {"requestId": str(uuid.uuid4()), "payload": {"sku": sku, "device": device}}
    req = Request(
        CLOUD_BASE + path,
        data=json.dumps(body).encode(),
        headers={"Govee-API-Key": api_key, "Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=CLOUD_TIMEOUT) as resp:
        data = json.loads(resp.read().decode())
    caps = data.get("payload", {}).get("capabilities", []) or []
    out: list[dict] = []
    for cap in caps:
        if cap.get("instance") != instance:
            continue
        for opt in cap.get("parameters", {}).get("options", []):
            val = opt.get("value")
            if not isinstance(val, dict) or "paramId" not in val:
                continue
            out.append({
                "paramId": int(val["paramId"]),
                "id": int(val.get("id", val["paramId"])),
                "name": opt.get("name", ""),
            })
    return out


class GoveeClient:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ready = False
        self._api_key = _api_key()
        self._devices: list[dict] = []
        self._device_locks: dict[str, threading.Lock] = {}
        self._device_last_send: dict[str, float] = {}
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=16)
        self._scenes_by_sku: dict[str, list[dict]] = {}
        self._load_scenes_cache()

    @property
    def devices(self) -> list[dict]:
        return list(self._devices)

    def ensure_ready(self, refresh: bool = False) -> None:
        with self._lock:
            if self._ready and not refresh:
                return
            cached = [] if refresh else _load_cache()
            if cached:
                self._devices = cached
                self._ready = True
                return
            self.refresh()

    def refresh(self) -> None:
        cloud_ok = False
        cloud_devs: list[dict] = []
        if self._api_key:
            try:
                cloud_devs = cloud_fetch_devices(self._api_key)
                cloud_ok = True
            except Exception as e:
                print(f"[govee] cloud fetch failed: {e}", flush=True)

        lan_found = lan_scan()
        lan_by_device = {d["device"]: d for d in lan_found.values() if d.get("device")}
        print(f"[govee] cloud={len(cloud_devs)} lan={len(lan_found)}", flush=True)

        # Seed from existing cache so a partial failure (cloud ok, LAN empty, or
        # vice versa) doesn't destroy previously-known devices.
        by_device: dict[str, dict] = {}
        for d in _load_cache():
            if d.get("device"):
                by_device[d["device"]] = dict(d)

        if cloud_ok:
            for c in cloud_devs:
                prev = by_device.get(c["device"], {})
                by_device[c["device"]] = {**prev, **c}

        for dev_id, lan in lan_by_device.items():
            prev = by_device.get(dev_id, {})
            by_device[dev_id] = {
                **prev,
                "device": dev_id,
                "sku": prev.get("sku") or lan.get("sku"),
                "name": prev.get("name", ""),
                "ip": lan.get("ip"),
            }

        if not cloud_ok and not lan_found and by_device:
            print("[govee] both refresh paths empty — keeping prior cache", flush=True)

        merged = list(by_device.values())
        self._devices = merged
        _save_cache(merged)
        self._ready = True

    def _device_lock(self, dev_id: str) -> threading.Lock:
        lock = self._device_locks.get(dev_id)
        if lock is None:
            lock = self._device_locks.setdefault(dev_id, threading.Lock())
        return lock

    def _wait_gap(self, dev_id: str) -> None:
        last = self._device_last_send.get(dev_id, 0.0)
        remaining = (last + COMMAND_GAP_S) - time.time()
        if remaining > 0:
            time.sleep(remaining)
        self._device_last_send[dev_id] = time.time()

    def _send_device_steps(self, dev: dict, steps: list[dict]) -> None:
        """Run a sequence of commands against one device with serialization.

        Each step: {lan: (cmd, data) | None, cap: dict | None, verify: bool}.
        LAN path preferred when the device has an IP AND the step has a LAN
        form. Steps without a LAN form (e.g. named scenes) always go to cloud,
        regardless of whether the device is also reachable over LAN.
        `verify=True` fires both LAN (instant) and cloud (reliable, retried)
        so a dropped UDP packet can't leave the device in a stale state —
        used for off-correctness commands where "stuck on" is the failure.
        """
        dev_id = dev.get("device") or dev.get("ip") or ""
        ip = dev.get("ip")
        sku = dev.get("sku")
        can_cloud = bool(sku and dev.get("device") and self._api_key)
        with self._device_lock(dev_id):
            for step in steps:
                self._wait_gap(dev_id)
                lan_sent = False
                if ip and step.get("lan"):
                    cmd, data = step["lan"]
                    _lan_send(ip, cmd, data)
                    lan_sent = True
                if can_cloud and step.get("cap") and (not lan_sent or step.get("verify")):
                    _cloud_control(self._api_key, sku, dev["device"], step["cap"])

    def _broadcast_steps(self, steps_for_dev: callable) -> None:
        """Fan out a per-device step sequence across all devices in parallel."""
        self.ensure_ready()
        futs = []
        for dev in self._devices:
            steps = steps_for_dev(dev)
            if not steps:
                continue
            futs.append(self._pool.submit(self._send_device_steps, dev, steps))
        # Wait so callers can assume commands have been dispatched.
        concurrent.futures.wait(futs, timeout=CLOUD_TIMEOUT + 1.0)

    # -- atomic operations --

    def turn(self, on: bool, *, verify: bool = False) -> None:
        val = 1 if on else 0
        cap = {"type": "devices.capabilities.on_off", "instance": "powerSwitch", "value": val}
        lan = ("turn", {"value": val})
        self._broadcast_steps(lambda d: [{"lan": lan, "cap": cap, "verify": verify}])

    def turn_skus(self, skus, on: bool, *, verify: bool = False) -> None:
        """Turn on/off only the devices whose sku is in the given set.

        verify=True sends both LAN and cloud so a dropped UDP packet doesn't
        leave the device stuck on — pass it for scene-boundary off commands
        where the symptom is a COB strip that wouldn't power down.
        """
        wanted = set(skus)
        if not wanted:
            return
        val = 1 if on else 0
        cap = {"type": "devices.capabilities.on_off", "instance": "powerSwitch", "value": val}
        lan = ("turn", {"value": val})

        def steps(dev):
            if dev.get("sku") not in wanted:
                return []
            return [{"lan": lan, "cap": cap, "verify": verify}]

        self._broadcast_steps(steps)

    def set_color_and_brightness(self, r: int, g: int, b: int, brightness: int | None = None) -> None:
        r, g, b = int(r) & 0xFF, int(g) & 0xFF, int(b) & 0xFF
        color_cap = {
            "type": "devices.capabilities.color_setting",
            "instance": "colorRgb",
            "value": (r << 16) | (g << 8) | b,
        }
        color_lan = ("colorwc", {"color": {"r": r, "g": g, "b": b}, "colorTemInKelvin": 0})

        def steps(dev):
            out = [{"lan": color_lan, "cap": color_cap}]
            if brightness is not None:
                pct = max(1, min(100, int(brightness)))
                out.append({
                    "lan": ("brightness", {"value": pct}),
                    "cap": {"type": "devices.capabilities.range", "instance": "brightness", "value": pct},
                })
            return out

        self._broadcast_steps(steps)

    def set_color(self, r: int, g: int, b: int) -> None:
        self.set_color_and_brightness(r, g, b, brightness=None)

    def set_brightness(self, pct: int) -> None:
        pct = max(1, min(100, int(pct)))
        cap = {"type": "devices.capabilities.range", "instance": "brightness", "value": pct}
        lan = ("brightness", {"value": pct})
        self._broadcast_steps(lambda d: [{"lan": lan, "cap": cap}])

    # -- scenes (cloud-only; LAN API has no scene support) --

    def _load_scenes_cache(self) -> None:
        try:
            self._scenes_by_sku = json.loads(SCENES_PATH.read_text())
        except Exception:
            self._scenes_by_sku = {}

    def scenes_for_sku(self, sku: str) -> list[dict]:
        return list(self._scenes_by_sku.get(sku, []))

    @property
    def scenes_by_sku(self) -> dict[str, list[dict]]:
        return dict(self._scenes_by_sku)

    def refresh_scenes(self) -> dict[str, int]:
        """Fetch scene lists once per unique SKU; cache to disk."""
        if not self._api_key:
            return {}
        self.ensure_ready()
        sample: dict[str, dict] = {}
        for d in self._devices:
            sku = d.get("sku")
            if sku and d.get("device") and sku not in sample:
                sample[sku] = d
        out: dict[str, list[dict]] = {}
        for sku, dev in sample.items():
            try:
                out[sku] = cloud_fetch_scenes(self._api_key, sku, dev["device"], instance="lightScene")
            except Exception as e:
                print(f"[govee] scene fetch {sku} failed: {e}", flush=True)
                out[sku] = []
        self._scenes_by_sku = out
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        SCENES_PATH.write_text(json.dumps(out, indent=2))
        return {sku: len(v) for sku, v in out.items()}

    def _scene_value(self, sku: str, param_id: int) -> dict | None:
        """Look up the full {paramId, id} value pair from the cached scene list.
        Govee's control API requires both, and they are NOT equal."""
        for s in self._scenes_by_sku.get(sku, []):
            if int(s.get("paramId", -1)) == int(param_id):
                return {"paramId": int(s["paramId"]), "id": int(s.get("id", s["paramId"]))}
        return None

    def set_scene_for_sku(self, sku: str, scene_id: int, instance: str = "lightScene") -> None:
        """Trigger a named scene on every device matching sku."""
        value = self._scene_value(sku, scene_id)
        if value is None:
            print(f"[govee] scene {scene_id} not in cache for sku {sku} — run `govee refresh-scenes`", flush=True)
            return
        cap = {
            "type": "devices.capabilities.dynamic_scene",
            "instance": instance,
            "value": value,
        }

        def steps(dev):
            if dev.get("sku") != sku:
                return []
            return [{"lan": None, "cap": cap}]

        self._broadcast_steps(steps)

    def apply_mode_scenes(self, mapping: dict[str, int]) -> None:
        """Apply a {sku: scene_paramId} mapping in parallel, one scene per SKU."""
        cap_by_sku: dict[str, dict] = {}
        for sku, param_id in mapping.items():
            value = self._scene_value(sku, param_id)
            if value is None:
                print(f"[govee] scene {param_id} not in cache for sku {sku}", flush=True)
                continue
            cap_by_sku[sku] = {
                "type": "devices.capabilities.dynamic_scene",
                "instance": "lightScene",
                "value": value,
            }

        def steps(dev):
            cap = cap_by_sku.get(dev.get("sku"))
            if cap is None:
                return []
            return [{"lan": None, "cap": cap}]

        self._broadcast_steps(steps)


_default_client: GoveeClient | None = None
_default_lock = threading.Lock()


def get_client() -> GoveeClient:
    global _default_client
    with _default_lock:
        if _default_client is None:
            _default_client = GoveeClient()
        return _default_client


def hex_to_rgb(hex_str: str) -> tuple[int, int, int]:
    s = hex_str.lstrip("#").strip()
    if len(s) != 6:
        raise ValueError(f"bad color: {hex_str}")
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
