"""Owns the PyAudio stream lifecycle: finds the configured output device,
opens/moves/reopens a stream as it comes and goes, and generates the noise.

On Linux, output is routed through PipeWire's Pulse-compat layer (the ALSA
"pulse" device) and then explicitly moved to the matching sink via
`pactl move-sink-input` -- opening the raw ALSA hardware device directly
would fight PipeWire for exclusive access to the card.

On Windows, output devices are real distinct WASAPI endpoints, so normal
PortAudio device selection by name is used instead.
"""
import json
import os
import subprocess
import sys
import threading
import time

import pyaudio

from .noise import PinkNoise, pct_to_gain

IS_LINUX = sys.platform.startswith("linux")
SAMPLE_RATE = 48000
CHUNK = 1024


def pactl_list_sinks():
    try:
        out = subprocess.run(
            ["pactl", "-f", "json", "list", "sinks"],
            capture_output=True, text=True, timeout=3, check=True,
        ).stdout
        data = json.loads(out)
        return [(s["name"], s.get("description", s["name"])) for s in data]
    except Exception as e:
        print(f"vr-companion audio: pactl list sinks failed: {e}")
        return []


def pactl_find_sink(match: str):
    match = match.lower()
    for name, desc in pactl_list_sinks():
        if match in name.lower() or match in desc.lower():
            return name
    return None


def pactl_move_our_stream_to_sink(sink_name, retries=10, delay=0.15):
    pid = os.getpid()
    for _ in range(retries):
        try:
            out = subprocess.run(
                ["pactl", "-f", "json", "list", "sink-inputs"],
                capture_output=True, text=True, timeout=3, check=True,
            ).stdout
            for si in json.loads(out):
                props = si.get("properties", {})
                if str(props.get("application.process.id")) == str(pid):
                    subprocess.run(
                        ["pactl", "move-sink-input", str(si["index"]), sink_name],
                        capture_output=True, text=True, timeout=3,
                    )
                    return True
        except Exception as e:
            print(f"vr-companion audio: move-sink-input failed: {e}")
        time.sleep(delay)
    return False


class NoiseEngine:
    def __init__(self, volume_pct=10, device_match="index"):
        self.volume_pct = volume_pct
        self.device_match = device_match
        self.enabled = True

        self.pa = pyaudio.PyAudio()
        self.noise = PinkNoise()
        self.stream = None
        self.current_target = None
        self._lock = threading.Lock()
        self._ticker = None
        self._ticker_stop = threading.Event()

    # ---- device discovery ----

    def find_target(self):
        if not self.device_match:
            return None
        if IS_LINUX:
            return pactl_find_sink(self.device_match)
        match = self.device_match.lower()
        for i in range(self.pa.get_device_count()):
            info = self.pa.get_device_info_by_index(i)
            if info.get("maxOutputChannels", 0) > 0 and match in info["name"].lower():
                return i
        return None

    def list_candidate_devices(self):
        keywords = ("index", "vr", "hmd", "valve", "vive", "oculus", "wmr")
        out = []
        if IS_LINUX:
            for name, desc in pactl_list_sinks():
                if any(k in desc.lower() or k in name.lower() for k in keywords):
                    out.append((name, desc))
            return out
        for i in range(self.pa.get_device_count()):
            info = self.pa.get_device_info_by_index(i)
            if info.get("maxOutputChannels", 0) <= 0:
                continue
            if any(k in info["name"].lower() for k in keywords):
                out.append((i, info["name"]))
        return out

    def list_all_output_devices(self):
        if IS_LINUX:
            return pactl_list_sinks()
        out = []
        for i in range(self.pa.get_device_count()):
            info = self.pa.get_device_info_by_index(i)
            if info.get("maxOutputChannels", 0) > 0:
                out.append((i, info["name"]))
        return out

    def _get_pulse_device_index(self):
        for i in range(self.pa.get_device_count()):
            if self.pa.get_device_info_by_index(i)["name"] == "pulse":
                return i
        return None

    # ---- stream lifecycle ----

    def _audio_callback(self, in_data, frame_count, time_info, status):
        gain = pct_to_gain(self.volume_pct)
        chunk = self.noise.next_chunk(frame_count) * gain
        return (chunk.tobytes(), pyaudio.paContinue)

    def _start_stream(self, target):
        with self._lock:
            self._stop_stream_locked()
            try:
                device_index = self._get_pulse_device_index() if IS_LINUX else target
                self.stream = self.pa.open(
                    format=pyaudio.paFloat32,
                    channels=2,
                    rate=SAMPLE_RATE,
                    output=True,
                    output_device_index=device_index,
                    frames_per_buffer=CHUNK,
                    stream_callback=self._audio_callback,
                )
                self.stream.start_stream()
                self.current_target = target
            except Exception as e:
                print(f"vr-companion audio: failed to open stream: {e}")
                self.current_target = None
                return
        if IS_LINUX:
            threading.Thread(
                target=pactl_move_our_stream_to_sink, args=(target,), daemon=True
            ).start()

    def _stop_stream_locked(self):
        if self.stream is not None:
            try:
                self.stream.stop_stream()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        self.current_target = None

    def stop(self):
        with self._lock:
            self._stop_stream_locked()

    def shutdown(self):
        self._ticker_stop.set()
        if self._ticker is not None:
            self._ticker.join(timeout=5)
        self.stop()
        self.pa.terminate()

    # ---- background presence polling ----

    def start_ticker(self, interval_s=2.0):
        """Run tick() every interval_s on a background thread. It shells out
        to pactl and opens PortAudio streams, which must not stall the UI."""
        def loop():
            while True:
                try:
                    self.tick()
                except Exception as e:
                    print(f"vr-companion audio: tick failed: {e}")
                if self._ticker_stop.wait(interval_s):
                    return
        self._ticker = threading.Thread(target=loop, name="noise-engine-tick", daemon=True)
        self._ticker.start()

    def tick(self) -> bool:
        """Check device presence and (re)start/stop as needed.
        Returns True if something changed (for the UI to refresh on)."""
        if self.enabled:
            target = self.find_target()
            if target is not None and target != self.current_target:
                self._start_stream(target)
                return True
            elif target is None and self.current_target is not None:
                self.stop()
                return True
        else:
            if self.current_target is not None:
                self.stop()
                return True
        return False

    @property
    def is_active(self) -> bool:
        return self.current_target is not None
