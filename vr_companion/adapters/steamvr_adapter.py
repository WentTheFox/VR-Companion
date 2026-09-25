"""SteamVR backend -- not yet implemented.

To fill this in: either the `openvr` PyPI package (pyopenvr, not currently
packaged on this system -- would need `pip install --user openvr` or a venv),
or a direct ctypes binding against SteamVR's own libopenvr_api.so, calling
vr::IVRSystem::GetTrackedDeviceClass()/IsTrackedDeviceConnected() etc. This
stub exists so the adapter architecture is real and selectable in the UI
even before that work is done.
"""
from .base import BackendSnapshot, VRAdapter


class SteamVRAdapter(VRAdapter):
    name = "SteamVR"

    def is_available(self) -> bool:
        return False  # flip to a real check once implemented

    def connect(self) -> bool:
        return False

    def disconnect(self):
        pass

    def poll(self) -> BackendSnapshot:
        return BackendSnapshot(
            self.name, connected=False,
            error="SteamVR adapter not implemented yet (see module docstring)",
        )
