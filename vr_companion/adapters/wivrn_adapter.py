"""WiVRn backend -- not yet implemented.

WiVRn exposes its own control interface (a dashboard app / D-Bus service on
recent versions) rather than libmonado, since it's a separate OpenXR runtime
aimed at standalone/wireless headsets. Would need its own protocol client
written against whatever WiVRn's dashboard uses. Stub kept for architectural
parity with the other backends.
"""
from .base import BackendSnapshot, VRAdapter


class WiVRnAdapter(VRAdapter):
    name = "WiVRn"

    def is_available(self) -> bool:
        return False

    def connect(self) -> bool:
        return False

    def disconnect(self):
        pass

    def poll(self) -> BackendSnapshot:
        return BackendSnapshot(
            self.name, connected=False,
            error="WiVRn adapter not implemented yet (see module docstring)",
        )
