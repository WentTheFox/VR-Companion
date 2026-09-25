"""Monado backend, via a ctypes binding to libmonado (monado.h).

libmonado talks to a running `monado-service` over its own IPC socket
(the same one `monado-ctl`/`monado-gui`'s "Remote" tooling use), so this
works whether Monado was started by Envision, a systemd unit, or by hand.
"""
import ctypes
import ctypes.util
import os
import signal
import subprocess
import time
from ctypes import c_bool, c_char_p, c_float, c_int32, c_uint32, POINTER, byref
from pathlib import Path

from .base import BackendSnapshot, ClientStatus, DeviceKind, DeviceStatus, VRAdapter

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
# (ghosting/lag) than the tearing they were meant to fix.
def _default_steamvr_path():
    p = Path.home() / ".local" / "share" / "Steam" / "steamapps" / "common" / "SteamVR"
    return str(p) if p.exists() else None

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
            if not self.connect():
                return BackendSnapshot(self.name, connected=False, error="No Monado service running")

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

            return BackendSnapshot(self.name, connected=True, devices=devices, clients=clients)
        except Exception as e:
            # The service likely went away mid-poll -- drop our handle so the
            # next poll() call retries connect() from scratch.
            self._root = None
            return BackendSnapshot(self.name, connected=False, error=str(e))

    # ---- service lifecycle: workaround for Monado not handling hotplug ----

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
        steamvr_path = _default_steamvr_path()
        if steamvr_path:
            env["STEAMVR_PATH"] = steamvr_path

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
