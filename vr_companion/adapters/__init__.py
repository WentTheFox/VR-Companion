from .base import BackendSnapshot, ClientStatus, DeviceKind, DeviceStatus, VRAdapter
from .monado_adapter import MonadoAdapter
from .steamvr_adapter import SteamVRAdapter
from .wivrn_adapter import WiVRnAdapter

ALL_ADAPTERS = {
    "monado": MonadoAdapter,
    "steamvr": SteamVRAdapter,
    "wivrn": WiVRnAdapter,
}


def make_adapter(key: str) -> VRAdapter:
    return ALL_ADAPTERS[key]()


def detect_available() -> list[str]:
    """Keys of adapters whose backend runtime looks installed."""
    out = []
    for key, cls in ALL_ADAPTERS.items():
        try:
            if cls().is_available():
                out.append(key)
        except Exception:
            pass
    return out
