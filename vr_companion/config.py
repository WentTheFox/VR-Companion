import json
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "vr-companion"
CONFIG_PATH = CONFIG_DIR / "config.json"

DEFAULTS = {
    "audio": {
        "volume_pct": 10,
        "device_match": "index",
        "enabled": True,
    },
    "backend": "monado",
    # backend key -> {ServiceOption.key: value}
    "service_options": {},
    # device serial -> SteamVR TrackerRole_* key (see roles.py)
    "device_roles": {},
}


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy
    try:
        loaded = json.loads(CONFIG_PATH.read_text())
        cfg.update(loaded)
        if "audio" in loaded:
            cfg["audio"] = {**DEFAULTS["audio"], **loaded["audio"]}
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return cfg


def save_config(cfg: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
