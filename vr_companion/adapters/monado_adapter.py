"""Monado backend, via a ctypes binding to libmonado (monado.h).

libmonado talks to a running `monado-service` over its own IPC socket
(the same one `monado-ctl`/`monado-gui`'s "Remote" tooling use), so this
works whether Monado was started by Envision, a systemd unit, or by hand.
"""
import ctypes
import ctypes.util
import os
import re
import signal
import subprocess
import time
from ctypes import c_bool, c_char_p, c_float, c_int32, c_uint32, POINTER, byref
from pathlib import Path

from .base import BackendSnapshot, ClientStatus, DeviceKind, DeviceStatus, ServiceOption, VRAdapter

STATE_DIR = Path.home() / ".local" / "state" / "vr-companion"
SERVICE_LOG_PATH = STATE_DIR / "monado-service.log"
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
PIDFILE_PATH = RUNTIME_DIR / "monado.pid"

# Known-good env for the SteamVR lighthouse driver path, worked out over a
# long debugging session: STEAMVR_PATH avoids a stale-cache failure in
# steamvr_lh's own libraryfolders.vdf parsing, STEAMVR_LH_ENABLE turns on
# real lighthouse tracking, XRT_COMPOSITOR_USE_PRESENT_WAIT mitigates an
# NVIDIA-specific compositor latency issue, and U_PACING_LIVE_STATS feeds
# the performance graph. Deliberately NOT included: XRT_COMPOSITOR_DESIRED_MODE
# and U_PACING_COMP_TIME_FRACTION_PERCENT, which we validated cause more harm
# (ghosting/lag) than the tearing they were meant to fix. LH_DISCOVER_WAIT_MS is
# user-set from the Devices tab (see service_options()).
def _default_steamvr_path():
    p = Path.home() / ".local" / "share" / "Steam" / "steamapps" / "common" / "SteamVR"
    return str(p) if p.exists() else None

# steamvr_lh drops any device that shows up after its discovery window
# (LH_DISCOVER_WAIT_MS, default 3000) -- including one powered on after the
# service started -- and logs only this, with no serial or device class.
LATE_DEVICE_WARNING = "Cannot add device after setup"
LH_DISCOVER_WAIT_DEFAULT_MS = 3000

# When no builder recognises a real headset, Monado falls back to its
# "legacy" builder and hands out this instead -- e.g. when the Index's USB
# devices failed to enumerate (seen live: its internal hub stuck resetting).
SIMULATED_HMD_NAME = "Simulated HMD"
VALVE_USB_VENDOR = "28de"
INDEX_HMD_USB_PRODUCT = "2300"


def _edid_manufacturer(edid: bytes):
    """3-letter PNP ID from EDID bytes 8-9 (e.g. "VLV"), or None."""
    if len(edid) < 128 or edid[:8] != bytes.fromhex("00ffffffffffff00"):
        return None
    m = (edid[8] << 8) | edid[9]
    return "".join(chr(((m >> shift) & 0x1F) + 64) for shift in (10, 5, 0))


def _headset_display_state(root=Path("/sys/class/drm")):
    """Linux-only look at connected DRM outputs' EDIDs:
    "ok" -- some output reports Valve's "VLV" EDID (the Index display);
    "fallback" -- none does, but one has the NVIDIA driver's blank fallback
        EDID ("NVD", product 0, 640x480 only): the headset's real EDID read
        failed at power-up. Seen live 2026-09-25; a DP replug didn't fix it,
        power-cycling the headset did. Monado then can't find the display;
    None -- neither / can't tell (no sysfs, e.g. Windows)."""
    if not root.is_dir():
        return None
    fallback = False
    for conn in root.glob("card*-*"):
        try:
            if (conn / "status").read_text().strip() != "connected":
                continue
            edid = (conn / "edid").read_bytes()
        except OSError:
            continue
        mfg = _edid_manufacturer(edid)
        if mfg == "VLV":
            return "ok"
        if mfg == "NVD" and edid[10:12] == b"\0\0":
            fallback = True
    return "fallback" if fallback else None


def _index_hmd_on_usb():
    """True/False on Linux (via sysfs), None where that can't be checked."""
    root = Path("/sys/bus/usb/devices")
    if not root.is_dir():
        return None
    for dev in root.iterdir():
        try:
            if ((dev / "idVendor").read_text().strip() == VALVE_USB_VENDOR
                    and (dev / "idProduct").read_text().strip() == INDEX_HMD_USB_PRODUCT):
                return True
        except OSError:
            continue
    return False


# Service log markers for lighthouse startup (steamvr_lh.cpp).
DISCOVERY_START = "Lighthouse initialization complete"
DISCOVERY_END = "Device search time complete"
FOUND_DEVICE = re.compile(r"Found lighthouse (HMD|controller|tracker|base station): (\S+)")
FOUND_KINDS = {
    "HMD": DeviceKind.HMD,
    "controller": DeviceKind.CONTROLLER,
    "tracker": DeviceKind.TRACKER,
    "base station": DeviceKind.BASE_STATION,
}
PHASE_STARTING, PHASE_DISCOVERING, PHASE_DONE = "starting", "discovering", "done"
KIND_NAMES = {
    DeviceKind.HMD: "Headset",
    DeviceKind.CONTROLLER: "Controller",
    DeviceKind.TRACKER: "Tracker",
    DeviceKind.BASE_STATION: "Base station",
}


class _ServiceLogScanner:
    """Incrementally follows the service log we launched monado-service
    with: startup phase, devices found during lighthouse discovery, and
    LATE_DEVICE_WARNING lines."""

    def __init__(self):
        self._fh = None
        self.reset()

    def reset(self):
        if self._fh:
            self._fh.close()
        self._fh = None
        self._inode = None
        self._clear()

    def _clear(self):
        self.phase = PHASE_STARTING
        self.found = {}   # serial -> DeviceKind, in discovery order
        self.late = 0

    def poll(self, path):
        try:
            st = os.stat(path)
        except FileNotFoundError:
            self.reset()
            return
        if self._fh is None or self._inode != st.st_ino or st.st_size < self._fh.tell():
            if self._fh:
                self._fh.close()
            self._fh = open(path, "r", errors="replace")
            self._inode = st.st_ino
            self._clear()
        while True:
            pos = self._fh.tell()
            line = self._fh.readline()
            if not line:
                break
            if not line.endswith("\n"):
                self._fh.seek(pos)
                break
            if LATE_DEVICE_WARNING in line:
                self.late += 1
            elif DISCOVERY_END in line:
                self.phase = PHASE_DONE
            elif DISCOVERY_START in line and self.phase == PHASE_STARTING:
                self.phase = PHASE_DISCOVERING
            else:
                m = FOUND_DEVICE.search(line)
                if m:
                    self.found[m.group(2)] = FOUND_KINDS[m.group(1)]


# --- mnd_property_t ---
MND_PROPERTY_NAME_STRING = 0
MND_PROPERTY_SERIAL_STRING = 1
MND_PROPERTY_TRACKING_ORIGIN_U32 = 2
MND_PROPERTY_SUPPORTS_POSITION_BOOL = 3
MND_PROPERTY_SUPPORTS_ORIENTATION_BOOL = 4
MND_PROPERTY_SUPPORTS_BRIGHTNESS_BOOL = 5

# --- mnd_client_flags_t ---
MND_CLIENT_SESSION_ACTIVE = 1 << 1
MND_CLIENT_SESSION_FOCUSED = 1 << 3

ROLE_NAMES = ["head", "left", "right", "gamepad", "eyes"]


def _classify(name: str, role: str | None) -> DeviceKind:
    n = name.lower()
    if role == "head" or "hmd" in n or "index" in n and "controller" not in n:
        return DeviceKind.HMD
    if role in ("left", "right") or "controller" in n or "knuckles" in n:
        return DeviceKind.CONTROLLER
    if "tracker" in n or "vive tracker" in n:
        return DeviceKind.TRACKER
    if "base station" in n or "lighthouse" in n and "watchman" not in n:
        return DeviceKind.BASE_STATION
    return DeviceKind.OTHER


class MonadoAdapter(VRAdapter):
    name = "Monado"
    # monado-service we launched, if any. Class-level on purpose: the UI
    # replaces its adapter instance on a backend switch, and letting this
    # Popen get garbage-collected would close the service's stdin pipe --
    # which shuts the service down.
    _service_proc = None

    def __init__(self):
        self._lib = None
        self._root = None
        self._options = {"lh_discover_wait_ms": LH_DISCOVER_WAIT_DEFAULT_MS}
        self._log = _ServiceLogScanner()

    def is_available(self) -> bool:
        return ctypes.util.find_library("monado") is not None or self._try_load()

    def _try_load(self) -> bool:
        if self._lib is not None:
            return True
        try:
            self._lib = ctypes.CDLL("libmonado.so.25")
        except OSError:
            try:
                self._lib = ctypes.CDLL("libmonado.so")
            except OSError:
                self._lib = None
                return False
        self._setup_signatures()
        return True

    def _setup_signatures(self):
        lib = self._lib
        lib.mnd_root_create.argtypes = [POINTER(ctypes.c_void_p)]
        lib.mnd_root_create.restype = c_int32
        lib.mnd_root_destroy.argtypes = [POINTER(ctypes.c_void_p)]
        lib.mnd_root_update_client_list.argtypes = [ctypes.c_void_p]
        lib.mnd_root_update_client_list.restype = c_int32
        lib.mnd_root_get_number_clients.argtypes = [ctypes.c_void_p, POINTER(c_uint32)]
        lib.mnd_root_get_number_clients.restype = c_int32
        lib.mnd_root_get_client_id_at_index.argtypes = [ctypes.c_void_p, c_uint32, POINTER(c_uint32)]
        lib.mnd_root_get_client_id_at_index.restype = c_int32
        lib.mnd_root_get_client_name.argtypes = [ctypes.c_void_p, c_uint32, POINTER(c_char_p)]
        lib.mnd_root_get_client_name.restype = c_int32
        lib.mnd_root_get_client_state.argtypes = [ctypes.c_void_p, c_uint32, POINTER(c_uint32)]
        lib.mnd_root_get_client_state.restype = c_int32
        lib.mnd_root_get_device_count.argtypes = [ctypes.c_void_p, POINTER(c_uint32)]
        lib.mnd_root_get_device_count.restype = c_int32
        lib.mnd_root_get_device_info.argtypes = [ctypes.c_void_p, c_uint32, POINTER(c_uint32), POINTER(c_char_p)]
        lib.mnd_root_get_device_info.restype = c_int32
        lib.mnd_root_get_device_info_string.argtypes = [ctypes.c_void_p, c_uint32, c_int32, POINTER(c_char_p)]
        lib.mnd_root_get_device_info_string.restype = c_int32
        lib.mnd_root_get_device_from_role.argtypes = [ctypes.c_void_p, c_char_p, POINTER(c_int32)]
        lib.mnd_root_get_device_from_role.restype = c_int32
        lib.mnd_root_get_device_battery_status.argtypes = [
            ctypes.c_void_p, c_uint32, POINTER(c_bool), POINTER(c_bool), POINTER(c_float)
        ]
        lib.mnd_root_get_device_battery_status.restype = c_int32

    def connect(self) -> bool:
        if not self._try_load():
            return False
        if self._root is not None:
            return True
        root_ptr = ctypes.c_void_p()
        res = self._lib.mnd_root_create(byref(root_ptr))
        if res != 0 or not root_ptr:
            self._root = None
            return False
        self._root = root_ptr
        return True

    def disconnect(self):
        if self._root is not None and self._lib is not None:
            self._lib.mnd_root_destroy(byref(self._root))
        self._root = None

    def _device_role_map(self) -> dict:
        out = {}
        for role in ROLE_NAMES:
            idx = c_int32(-1)
            res = self._lib.mnd_root_get_device_from_role(self._root, role.encode(), byref(idx))
            if res == 0 and idx.value >= 0:
                out[idx.value] = role
        return out

    def poll(self) -> BackendSnapshot:
        if self._root is None:
            starting = self._startup_snapshot()
            if starting is not None:
                return starting
            if not self.connect():
                return BackendSnapshot(self.name, connected=False, error="No Monado service running",
                                       warnings=self._display_warnings())

        lib, root = self._lib, self._root
        try:
            role_map = self._device_role_map()

            count = c_uint32(0)
            lib.mnd_root_get_device_count(root, byref(count))
            devices = []
            for i in range(count.value):
                dev_id = c_uint32(0)
                name_ptr = c_char_p()
                if lib.mnd_root_get_device_info(root, i, byref(dev_id), byref(name_ptr)) != 0:
                    continue
                name = (name_ptr.value or b"").decode(errors="replace")
                role = role_map.get(i)
                serial_ptr = c_char_p()
                serial = None
                if lib.mnd_root_get_device_info_string(
                    root, i, MND_PROPERTY_SERIAL_STRING, byref(serial_ptr)
                ) == 0 and serial_ptr.value:
                    serial = serial_ptr.value.decode(errors="replace")

                present = c_bool(False)
                charging = c_bool(False)
                charge = c_float(0.0)
                battery_ok = lib.mnd_root_get_device_battery_status(
                    root, i, byref(present), byref(charging), byref(charge)
                ) == 0 and present.value

                devices.append(DeviceStatus(
                    id=str(i),
                    name=name,
                    kind=_classify(name, role),
                    tracking_ok=True,  # libmonado doesn't expose a direct "is tracking" flag; presence implies it
                    battery_percent=(charge.value * 100.0) if battery_ok else None,
                    charging=charging.value if battery_ok else None,
                    role=role,
                    serial=serial,
                ))

            lib.mnd_root_update_client_list(root)
            nclients = c_uint32(0)
            lib.mnd_root_get_number_clients(root, byref(nclients))
            clients = []
            for i in range(nclients.value):
                cid = c_uint32(0)
                if lib.mnd_root_get_client_id_at_index(root, i, byref(cid)) != 0:
                    continue
                name_ptr = c_char_p()
                lib.mnd_root_get_client_name(root, cid.value, byref(name_ptr))
                flags = c_uint32(0)
                lib.mnd_root_get_client_state(root, cid.value, byref(flags))
                clients.append(ClientStatus(
                    id=str(cid.value),
                    name=(name_ptr.value or b"").decode(errors="replace"),
                    active=bool(flags.value & MND_CLIENT_SESSION_ACTIVE),
                    focused=bool(flags.value & MND_CLIENT_SESSION_FOCUSED),
                ))

            late = self._late_device_placeholders()
            devices.extend(late)
            return BackendSnapshot(self.name, connected=True, devices=devices, clients=clients,
                                   warnings=self._warnings(devices, late))
        except Exception as e:
            # The service likely went away mid-poll -- drop our handle so the
            # next poll() call retries connect() from scratch.
            self._root = None
            return BackendSnapshot(self.name, connected=False, error=str(e))

    def _display_warnings(self) -> list:
        # Only meaningful while the headset is actually on: a powered-off
        # Index has no display attached at all.
        if _index_hmd_on_usb() is False or _headset_display_state() != "fallback":
            return []
        return [
            "The headset's display wasn't recognised: it came up with a blank placeholder "
            "EDID (\"NVD\", 640x480) instead of the Index's own, so Monado can't drive it. "
            "Power-cycle the headset (link box) -- a DisplayPort replug isn't enough -- "
            "then (re)start the service."
        ]

    def _warnings(self, devices, late) -> list:
        out = self._display_warnings()
        if any(d.role == "head" and d.name == SIMULATED_HMD_NAME for d in devices):
            msg = ("Monado didn't find your headset and is using a simulated HMD instead, "
                   "so no real devices (controllers included) will show up.")
            on_usb = _index_hmd_on_usb()
            if on_usb is False:
                msg += (" The Index HMD (USB 28de:2300) isn't connected over USB right now -- "
                        "power-cycle the link box, then restart the service.")
            else:
                msg += " Check the headset's power and USB connection, then restart the service."
            out.append(msg)
        if late:
            out.append(f"{len(late)} device(s) showed up too late to be added (placeholder rows below). "
                       "Increase the lighthouse discovery wait and restart the service.")
        return out

    def _startup_snapshot(self):
        """While the service we launched is still starting up, a snapshot of
        placeholder rows built from its log -- or None once it's past
        lighthouse discovery (or isn't ours), meaning: go connect.

        Connecting before then is pointless: libmonado can't talk to the
        service until it has finished creating its devices, and a connect
        attempt may sit blocked until it does."""
        if not self.owns_running_service():
            return None
        self._log.poll(SERVICE_LOG_PATH)
        if self._log.phase == PHASE_DONE:
            return None

        wait_ms = self._options["lh_discover_wait_ms"]
        if self._log.phase == PHASE_STARTING:
            status = "Starting Monado service..."
        else:
            status = f"Discovering lighthouse devices (at least {wait_ms / 1000:g} s)..."
        devices = [
            DeviceStatus(
                id=f"found-{serial}",
                name=f"{KIND_NAMES[kind]} (setting up...)",
                kind=kind,
                serial=serial,
                placeholder=True,
                placeholder_status="Found",
                note="Found during lighthouse discovery; Monado is still setting it up.",
            )
            for serial, kind in self._log.found.items()
        ]
        devices.append(DeviceStatus(
            id="discovering",
            name="Searching for devices...",
            kind=DeviceKind.OTHER,
            placeholder=True,
            placeholder_status="Discovering",
            note=(
                "Monado is waiting for lighthouse devices to report in. Devices that "
                "only show up after this window closes won't be added until the next "
                f"restart (lighthouse discovery wait: {wait_ms} ms)."
            ),
        ))
        return BackendSnapshot(self.name, connected=False, devices=devices, busy=status,
                               warnings=self._display_warnings())

    def _late_device_placeholders(self) -> list:
        # Only our own service's log describes the running service; if it
        # was started some other way, that file is from an older run.
        if not self.owns_running_service():
            return []
        self._log.poll(SERVICE_LOG_PATH)
        n = self._log.late
        return [
            DeviceStatus(
                id=f"late-{i}",
                name="Unidentified lighthouse device",
                kind=DeviceKind.OTHER,
                tracking_ok=False,
                placeholder=True,
                placeholder_status="Not added",
                note=(
                    "Monado saw this device but it arrived after lighthouse discovery "
                    "had finished (or was powered on after the service started), so it "
                    "wasn't added. Monado doesn't log which device it was.\n"
                    "Increase the lighthouse discovery wait and restart the service."
                ),
            )
            for i in range(n)
        ]

    # ---- service lifecycle: workaround for Monado not handling hotplug ----

    def service_options(self) -> list:
        return [ServiceOption(
            key="lh_discover_wait_ms",
            label="Lighthouse discovery wait",
            default=LH_DISCOVER_WAIT_DEFAULT_MS,
            minimum=500,
            maximum=60000,
            step=500,
            suffix=" ms",
            tooltip=(
                "How long Monado waits for lighthouse devices (HMD, controllers, trackers, "
                "base stations) to show up at startup (LH_DISCOVER_WAIT_MS). Devices that "
                "arrive later are dropped until the next restart."
            ),
        )]

    def set_service_option(self, key, value):
        if key in self._options:
            self._options[key] = int(value)

    def supports_service_restart(self) -> bool:
        return True

    def _find_running_pid(self):
        try:
            pid = int(PIDFILE_PATH.read_text().strip())
            # The pidfile outlives a crashed service, and its PID can be reused
            # by an unrelated process -- only trust it if it's still monado.
            if Path(f"/proc/{pid}/comm").read_text().strip() == "monado-service":
                return pid
        except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
            pass
        try:
            out = subprocess.run(
                ["pgrep", "-x", "monado-service"], capture_output=True, text=True, timeout=3
            ).stdout.strip()
            if out:
                return int(out.splitlines()[0])
        except Exception:
            pass
        return None

    def _stop_running_service(self, timeout_s=8.0):
        pid = self._find_running_pid()
        if pid is None:
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        own = MonadoAdapter._service_proc
        if own is not None and own.pid == pid:
            # Our own child: it stays a zombie (still "alive" to kill(pid, 0))
            # until reaped, so wait() on it rather than polling.
            try:
                own.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                own.kill()
                own.wait()
            return
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.2)
        # Some builds hang briefly in a vkWaitForFences loop after a display
        # drop -- seen repeatedly during testing. Escalate if it didn't exit.
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def restart_service(self) -> bool:
        # Drop our own client handle before killing the server it's talking to.
        self.disconnect()
        self._stop_running_service()

        STATE_DIR.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env["STEAMVR_LH_ENABLE"] = "true"
        env["XRT_COMPOSITOR_USE_PRESENT_WAIT"] = "1"
        env["U_PACING_LIVE_STATS"] = "1"
        env["LH_DISCOVER_WAIT_MS"] = str(self._options["lh_discover_wait_ms"])
        steamvr_path = _default_steamvr_path()
        if steamvr_path:
            env["STEAMVR_PATH"] = steamvr_path

        # The log is about to be truncated; the new one may outgrow our old
        # read offset before the next poll, so don't rely on noticing that.
        self._log.reset()
        try:
            log_f = open(SERVICE_LOG_PATH, "w")
            # stdin=PIPE (left open, never written to) rather than DEVNULL --
            # monado-service's epoll(stdin) setup fails against /dev/null.
            MonadoAdapter._service_proc = subprocess.Popen(
                ["/usr/bin/monado-service"],
                env=env,
                stdin=subprocess.PIPE,
                stdout=log_f,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except Exception as e:
            print(f"vr-companion: failed to launch monado-service: {e}")
            return False
        return True

    def is_service_running(self) -> bool:
        return self._find_running_pid() is not None

    def owns_running_service(self) -> bool:
        # Our child's stdin is a pipe held by this process; monado-service
        # shuts down as soon as it closes, i.e. when this app exits.
        proc = MonadoAdapter._service_proc
        return proc is not None and proc.poll() is None

    # ---- performance graph data ----

    def supports_frame_timing(self) -> bool:
        return True

    def get_frame_timing_log_path(self):
        return str(SERVICE_LOG_PATH) if SERVICE_LOG_PATH.exists() else None
