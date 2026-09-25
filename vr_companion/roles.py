"""Body roles for trackers (waist, feet, ...), assigned per device serial in
this app and persisted in its config under "device_roles".

Keys are SteamVR's own ETrackerRole names -- the same values SteamVR writes
to steamvr.vrsettings' "trackers" section -- so the mapping can later be fed
to something that actually consumes roles (e.g. a patched xrizer) or
exported to SteamVR as-is. Neither Monado nor xrizer reads roles today, so on
Linux these are informational only.
"""
import json
import sys
from pathlib import Path

# (key, label), in the order SteamVR's "Manage Trackers" lists them.
TRACKER_ROLES = [
    ("TrackerRole_None", "—"),
    ("TrackerRole_Handed", "Held in hand"),
    ("TrackerRole_LeftFoot", "Left foot"),
    ("TrackerRole_RightFoot", "Right foot"),
    ("TrackerRole_LeftShoulder", "Left shoulder"),
    ("TrackerRole_RightShoulder", "Right shoulder"),
    ("TrackerRole_LeftElbow", "Left elbow"),
    ("TrackerRole_RightElbow", "Right elbow"),
    ("TrackerRole_LeftKnee", "Left knee"),
    ("TrackerRole_RightKnee", "Right knee"),
    ("TrackerRole_LeftWrist", "Left wrist"),
    ("TrackerRole_RightWrist", "Right wrist"),
    ("TrackerRole_LeftAnkle", "Left ankle"),
    ("TrackerRole_RightAnkle", "Right ankle"),
    ("TrackerRole_Waist", "Waist"),
    ("TrackerRole_Chest", "Chest"),
    ("TrackerRole_Camera", "Camera"),
    ("TrackerRole_Keyboard", "Keyboard"),
]
NO_ROLE = "TrackerRole_None"


def get_role(cfg, serial) -> str:
    return cfg.get("device_roles", {}).get(serial, NO_ROLE)


def set_role(cfg, serial, role):
    roles = cfg.setdefault("device_roles", {})
    if role == NO_ROLE:
        roles.pop(serial, None)
    else:
        roles[serial] = role


# ---- importing from SteamVR's own settings ----

# Where steamvr.vrsettings lives relative to a Windows drive root / Steam dir.
_STEAM_SETTINGS = Path("Program Files (x86)") / "Steam" / "config" / "steamvr.vrsettings"


def find_steamvr_settings() -> list:
    """Candidate steamvr.vrsettings files holding SteamVR's tracker roles: the
    local Steam install, plus Windows installs on mounted drives (dual boot)."""
    candidates = [Path.home() / ".local" / "share" / "Steam" / "config" / "steamvr.vrsettings"]
    if sys.platform == "win32":
        candidates.append(Path("C:/") / _STEAM_SETTINGS)
    else:
        mounts = list(Path("/mnt").glob("*")) + list(Path("/run/media").glob("*/*"))
        candidates += [m / _STEAM_SETTINGS for m in mounts]
    out = []
    for p in candidates:
        try:
            if p.is_file() and read_steamvr_roles(p):
                out.append(p)
        except (OSError, ValueError):
            continue
    return out


def read_steamvr_roles(path) -> dict:
    """serial -> TrackerRole_* from a steamvr.vrsettings "trackers" section,
    whose keys look like "/devices/htc/vive_trackerLHR-92DD1F67" (trackers)
    or "/devices/lighthouse/LHR-C43FE57D" (other devices used as trackers)."""
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    known = {key for key, _ in TRACKER_ROLES}
    out = {}
    for device_path, role in (data.get("trackers") or {}).items():
        serial = device_path.rsplit("/", 1)[-1].removeprefix("vive_tracker")
        if serial and role in known:
            out[serial] = role
    return out
