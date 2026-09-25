# vr-companion -- handoff notes

Started 2026-09-25 in a long Claude Code session mostly spent debugging a
Linux VR setup (Valve Index, Monado + xrizer + libsurvive replacing
SteamVR). This app grew out of two things the user asked for mid-session:
a small tray app to mask an NVIDIA DisplayPort DAC idle whine with
background noise, then a request to expand that into a fuller "SteamVR
status window" style companion app with pluggable VR-runtime backends.

**This work was paused before live headset testing or the final two
features (restart button UI, performance graph UI) were wired into the
window.** The user asked to switch focus back to writing up NVIDIA/Monado
testing feedback in the same session; pick this back up fresh here.

## What's actually built and known to work

- `vr_companion/adapters/` -- pluggable backend interface (`base.py`).
  `MonadoAdapter` is real: a ctypes binding against the system package's
  `/usr/include/monado/monado.h`, talks to a running `monado-service` over
  its IPC socket. Tested and confirmed working: device list, roles,
  battery status, client list (was verified against a live session showing
  the Index HMD + both Knuckles controllers correctly).
  `SteamVRAdapter` / `WiVRnAdapter` are intentionally stubs (`is_available()`
  returns False) -- see each module's docstring for what real
  implementations would need (pyopenvr isn't packaged here; WiVRn has no
  known libmonado-equivalent, would need its own protocol client).
- `vr_companion/audio/` -- the noise-masking engine, migrated verbatim (in
  behavior) from an earlier standalone `vr_noise_mask.py` tray app that
  this project fully absorbs. Confirmed working end-to-end today: detects
  the Index's PipeWire sink by name match, opens a stream via PortAudio's
  "pulse" device (NOT the raw ALSA hw device -- see Gotchas), then uses
  `pactl move-sink-input` to land it on the actual target sink, exactly
  like `paplay --device=<sink>` does.
  - Volume is a **dB-scaled** dial (`noise.py: pct_to_gain`), not linear
    amplitude -- a linear 0.30 multiplier sounded much louder than "30%"
    should, since hearing is roughly logarithmic. Confirmed by the user
    mid-session; don't regress this back to linear.
  - The whine itself was empirically confirmed to be a **continuous,
    always-present** low-level noise (coil whine / DAC self-noise /
    interference), not a "silence vs signal" on/off thing -- louder
    masking noise made it progressively less audible rather than an
    on/off fix. Don't expect a config to make it truly zero.
- `vr_companion/config.py` -- config at `~/.config/vr-companion/config.json`.
  User's tuned values were migrated in from the old standalone app's config
  (`~/.config/vr-noise-mask/config.json`, now unused/stoppable).
- `vr_companion/ui/` -- `DevicesTab` (live table + backend picker) and
  `AudioTab` (volume presets, device-match field, candidate picker) both
  built and confirmed rendering/working live via
  `python3 -m vr_companion.app`.
- `vr_companion/app.py` -- entrypoint, tray icon (hides to tray on window
  close rather than quitting), `QTimer`-driven polling (2s) for both the
  devices tab and the audio engine's device-presence check.

## What's built but NOT yet wired into the UI (do this first)

- `adapters/base.py` gained four new optional-capability methods:
  `supports_service_restart()` / `restart_service()` and
  `supports_frame_timing()` / `get_frame_timing_log_path()`.
- `MonadoAdapter` implements all four for real:
  - `restart_service()`: finds & stops any running `monado-service` (via
    `/run/user/<uid>/monado.pid`, falling back to `pgrep -x`), SIGTERM then
    SIGKILL after an 8s timeout (this build has repeatedly been slow/stuck
    exiting after a display drop -- confirmed multiple times today), then
    relaunches it with a known-good env (see the module for exactly which
    vars and why each one is there -- worked out over hours of live testing
    today, don't re-derive it from scratch).
  - `get_frame_timing_log_path()` points at
    `~/.local/state/vr-companion/monado-service.log`, which
    `restart_service()` sets `monado-service` to write to (with
    `U_PACING_LIVE_STATS=1` in its env), truncated fresh on each restart.
- `vr_companion/perf/frame_log.py`: `FrameTimingTailer` incrementally parses
  that log's periodic "Compositor frame timing:" blocks (median/mean/worst
  per pipeline stage: cpu/draw/submit/gpu/gpu_delay/total_frame) into
  `FrameTimingSample` objects. Handles the log file being replaced/truncated
  across a restart (compares inode, reopens from the top).

**Still to do**, roughly in order:
1. `ui/devices_tab.py`: add a "Restart service" button, shown only when
   `adapter.supports_service_restart()`. Must run `restart_service()` on a
   background thread (it blocks for up to several seconds) -- use a
   `QThread`/worker-with-signal pattern, not a raw Python thread touching
   Qt widgets directly. Disable the button and show a status message while
   in progress; re-enable + trigger a `refresh()` on completion.
2. `ui/performance_tab.py` (new file): a live frame-timing graph, styled
   like SteamVR's. No graphing library is installed (no pyqtgraph/
   matplotlib) -- either add one or (leaning towards this, no new
   dependency) paint a rolling bar/line graph manually in a `QWidget`
   subclass's `paintEvent`, fed by a `QTimer` calling
   `FrameTimingTailer.poll_new_samples()` every ~500ms. Show current
   median/mean/worst numerically too. Gate on
   `adapter.supports_frame_timing()`; show a plain "not available for this
   backend" message otherwise.
3. Wire both into `ui/main_window.py` as additional tabs.
4. **Live test with the real headset** (this is what got deferred): launch
   `python3 -m vr_companion.app` from
   `/home/went/.local/share/vr-companion`, with the Index/base
   stations/controllers powered on, and check the Devices tab against real
   hardware (it's only been tested against an empty/disconnected state so
   far), plus exercise the restart button around an actual power-cycle.
5. Retire `~/.local/share/vr-noise-mask/vr_noise_mask.py` (the old
   standalone tray app) for real -- currently just stopped, not deleted,
   in case anything needs to be diffed against it.
6. systemd autostart wiring was asked for early on but not done: the user
   wants it tied to `monado.service` (a user-level systemd unit, socket-
   activated, "indirect" state) via a drop-in
   (`~/.config/systemd/user/monado.service.d/*.conf`), never editing the
   package's own unit file. Given this app now manages `monado-service`'s
   lifecycle itself via `restart_service()`, reconsider whether a systemd
   trigger is even still wanted, or whether "launch vr-companion, hit
   Restart" replaces that need -- ask the user, don't just build it blind.
7. Real `SteamVRAdapter` / `WiVRnAdapter` implementations, and Windows-side
   testing (this whole app was scoped as cross-platform; only ever run on
   Linux so far).

## Gotchas worth not re-discovering the hard way

- **Never open the raw ALSA hw device for the headset directly** (e.g.
  `hw:NVidia,7`, which is what PortAudio's device enumeration calls
  `"HDA NVidia: Index HMD"`). PipeWire already owns it; opening it directly
  fights PipeWire for exclusive access. Always go through PortAudio's
  `"pulse"` device, then `pactl move-sink-input` your own stream (matched
  by `application.process.id`) onto the real target sink by name. This is
  Linux-only; Windows exposes real distinct WASAPI endpoints per device, no
  such trick needed there (see the `IS_LINUX` branches in
  `audio/engine.py`).
- **`monado-service` launched with `stdin` pointed at `/dev/null` fails** at
  its own `epoll_ctl(stdin)` setup and refuses to start. Use
  `stdin=subprocess.PIPE` (left open, never written/closed) instead --
  confirmed to work, mirrors a `sleep N | monado-service` shell trick used
  earlier in the debugging session.
- Killing a live `monado-service` **immediately kills whatever OpenXR/
  OpenVR client is connected to it** (e.g. VRChat crashes with
  `ERROR_INSTANCE_LOST`) -- that's expected collateral of `restart_service()`,
  not a bug to chase. Surface this to the user in the UI rather than
  hiding it (e.g. a warning before/around the restart button).
- The Index's `driver_lighthouse` support (`STEAMVR_LH_ENABLE=true`) needs
  an actual SteamVR install present on disk purely to supply that one
  shared-library driver file -- SteamVR itself is never launched. If
  `STEAMVR_PATH` isn't set, `steamvr_lh` falls back to parsing
  `~/.steam/root/steamapps/libraryfolders.vdf` itself, which can be stale
  immediately after a fresh SteamVR install (confirmed this exact race
  today) -- hence always passing `STEAMVR_PATH` explicitly in
  `restart_service()`.
- Monado does **not** support hotplugging devices (headset, controllers, or
  base stations) into an already-running service -- this is the entire
  reason the restart button exists. A power-cycle of the headset requires a
  full service restart to be picked up again.
- `pactl`'s `sink-input`/`sink` listings support `-f json`, which this
  codebase relies on throughout (`audio/engine.py`) -- much less fragile
  than parsing plain-text `pactl list` output.

## Design decisions already made with the user (don't re-litigate)

- Pluggable adapters for Monado/SteamVR/WiVRn, explicitly because the user
  doesn't want to hard-code one backend.
- PySide6/Qt over tkinter, for a genuinely cross-platform, modern-looking
  window.
- The noise-mask tray app is **fully absorbed** into this app (one process,
  one tray icon), not kept as a separate always-running thing.
