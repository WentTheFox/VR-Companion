"""Common contract every VR runtime backend (Monado, SteamVR, WiVRn, ...)
implements, so the UI never needs to know which one is actually active."""
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional


class DeviceKind(Enum):
    HMD = auto()
    CONTROLLER = auto()
    TRACKER = auto()
    BASE_STATION = auto()
    OTHER = auto()


@dataclass
class DeviceStatus:
    id: str
    name: str
    kind: DeviceKind
    tracking_ok: Optional[bool] = None       # None = unknown/unsupported by this backend
    battery_percent: Optional[float] = None  # 0-100, None = unsupported/no data
    charging: Optional[bool] = None
    role: Optional[str] = None               # "head", "left", "right", etc.
    # A device the backend saw but couldn't add (identity unknown) -- shown
    # as a placeholder row so it isn't silently missing from the list.
    placeholder: bool = False
    note: Optional[str] = None


@dataclass
class ServiceOption:
    """An integer setting passed to the backend's service when it's
    (re)launched via restart_service(). Rendered generically by the UI."""
    key: str
    label: str
    default: int
    minimum: int
    maximum: int
    step: int = 1
    suffix: str = ""
    tooltip: str = ""


@dataclass
class ClientStatus:
    id: str
    name: str
    active: bool = False
    focused: bool = False


@dataclass
class BackendSnapshot:
    backend_name: str
    connected: bool
    devices: list = field(default_factory=list)   # list[DeviceStatus]
    clients: list = field(default_factory=list)    # list[ClientStatus]
    error: Optional[str] = None


class VRAdapter:
    """Base class for a runtime backend adapter. Subclasses should be cheap
    to construct and safe to poll repeatedly -- `poll()` is called on a timer
    from the UI thread's event loop and must not block for long."""

    name = "base"

    def is_available(self) -> bool:
        """Whether this backend's runtime/library is even installed, before
        trying to connect. Used to decide what to offer in the UI."""
        raise NotImplementedError

    def connect(self) -> bool:
        """Attempt to connect to a running service. Returns whether it
        succeeded; safe to call repeatedly (e.g. service not started yet)."""
        raise NotImplementedError

    def disconnect(self):
        raise NotImplementedError

    def poll(self) -> BackendSnapshot:
        """Return current devices/clients. Should not raise -- put failures
        in BackendSnapshot.error instead, so the UI can show it inline."""
        raise NotImplementedError

    # ---- optional capabilities: default to "not supported" ----

    def supports_service_restart(self) -> bool:
        """Whether this backend can (re)launch its own service process, as a
        workaround for backends that don't handle device hotplug."""
        return False

    def restart_service(self) -> bool:
        """Stop then relaunch the backend's service. Called from a worker
        thread, not the UI thread -- may block for a few seconds."""
        return False

    def service_options(self) -> list:
        """list[ServiceOption] this backend's restart_service() honours."""
        return []

    def set_service_option(self, key: str, value: int):
        """Takes effect on the next restart_service()."""

    def owns_running_service(self) -> bool:
        """Whether the backend's service is currently running as a child of
        this app -- i.e. quitting the app will also stop it."""
        return False

    def supports_frame_timing(self) -> bool:
        """Whether get_frame_timing_log_path() returns something parseable."""
        return False

    def get_frame_timing_log_path(self):
        """Path to a log file this backend's service writes live per-frame
        compositor timing to, or None. Used to feed a performance graph."""
        return None
