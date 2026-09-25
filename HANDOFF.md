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

## Restart button + performance graph (wired in 2026-09-25, second session)

- `adapters/base.py` has four optional-capability methods:
  `supports_service_restart()` / `restart_service()` and
  `supports_frame_timing()` / `get_frame_timing_log_path()`.
- `MonadoAdapter` implements all four for real:
  - `restart_service()`: finds & stops any running `monado-service` (via
    `/run/user/<uid>/monado.pid`, falling back to `pgrep -x`), SIGTERM then
    SIGKILL after an 8s timeout (this build has repeatedly been slow/stuck
    exiting after a display drop), then relaunches it with a known-good env
    (see the module for exactly which vars and why each one is there --
    worked out over hours of live testing, don't re-derive it from scratch).
  - `get_frame_timing_log_path()` points at
    `~/.local/state/vr-companion/monado-service.log`, which
    `restart_service()` sets `monado-service` to write to (with
    `U_PACING_LIVE_STATS=1` in its env), truncated fresh on each restart.
    It returns None until the first restart via this app creates the file.
- `perf/frame_log.py`: `FrameTimingTailer` incrementally parses that log's
  "Compositor frame timing:" blocks into `FrameTimingSample`s. Reopens on
  inode change *or* in-place truncation (file smaller than read offset --
  `restart_service()` opens with `"w"`, which keeps the inode). Uses
  `readline()` + `tell()` rather than file iteration (iteration disables
  `tell()`), and rewinds over a half-written trailing line.
- `ui/devices_tab.py`: "Restart service" button, visible only when
  `adapter.supports_service_restart()`. Confirms first via a dialog that
  warns connected VR apps will crash (lists current clients). Runs
  `restart_service()` in a `RestartWorker` QObject on a `QThread`;
  `refresh()` is skipped while it runs so the UI-thread poll doesn't race the
  worker on the same adapter. Emits `adapter_changed` / `service_restarted`.
- `ui/performance_tab.py`: hand-painted rolling bar graph (no new
  dependency) of `total_frame` -- solid bar to median, faded to worst,
  red when the median exceeds the chosen budget (80/90/120/144 Hz picker,
  default 90, not persisted). Table below shows median/mean/worst for all
  stages from the latest sample. Polled every 500ms from `MainWindow`.
  Shows a message page instead when the backend lacks frame timing or no
  log exists yet. Resets its tailer + history on `service_restarted`.
- Verified only headless (`QT_QPA_PLATFORM=offscreen`, fake adapter + synthetic
  log): restart thread round-trip, button visibility per backend, tailer
  truncation/partial-line handling, graph render. **Not yet run against a
  real `monado-service`.**

**Still to do**, roughly in order:
4. **Live test, partly done 2026-09-25:** `restart_service()` + Devices
   poll verified against the real Index: HMD + both Knuckles (with battery)
   listed within ~6s of launch. Still unverified: the Performance tab with
   real data (Monado only emits "Compositor frame timing" blocks while an
   OpenXR app is actually rendering -- none were, so the log had none), and
   Restart around a real headset power-cycle. Launch with
   `python3 -m vr_companion.app` from the repo root
   (`~/git/WentTheFox/VR-Companion`; the old `~/.local/share/vr-companion`
   location no longer exists), start a VR app, then check both.
5. Retire the old standalone app: its config was confirmed identical to
   the migrated one and nothing references it (no autostart/systemd/
   .desktop entry), but deleting it was blocked by Claude Code's auto-mode
   permission check -- the user needs to run
   `rm -r ~/.local/share/vr-noise-mask ~/.config/vr-noise-mask` themselves.
6. ~~systemd autostart~~ -- dropped: the user is happy for the companion
   app to own `monado-service` (see Design decisions), so there's no
   separate always-running service to autostart.
7. Real `SteamVRAdapter` / `WiVRnAdapter` implementations, and Windows-side
   testing (this whole app was scoped as cross-platform; only ever run on
   Linux so far).

## Gotchas worth not re-discovering the hard way

- **`/run/user/<uid>/monado.pid` outlives a crashed service** (and so does
  the `monado_comp_ipc` socket file). `_find_running_pid()` only trusts the
  pidfile if `/proc/<pid>/comm` is `monado-service`, so a reused PID can't
  get an unrelated process killed. Don't simplify that back to `kill(pid, 0)`.
- libmonado's client list includes every **status-only libmonado client**
  (this app's own poll, and the user's separate `vr-ha-agent`), each shown
  as `libmonado`. They aren't VR apps and won't crash on restart.
- Base stations are found by Monado (visible in the service log) but are
  not exposed as devices through libmonado, so they never appear in the
  Devices table.
- Seen once live: `Cannot add device after setup; consider increasing
  LH_DISCOVER_WAIT_MS` -- one lighthouse device showed up after the
  discovery window (4 Watchman dongles, only 2 controllers). Unknown which;
  if a tracker goes missing, try setting that var in `restart_service()`.

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

- **VR Companion owns `monado-service`'s lifetime.** A service started by
  `restart_service()` gets its stdin from a pipe held by this process, and
  shuts down cleanly as soon as that pipe closes -- i.e. quitting (or
  crashing) VR Companion stops the service and any connected VR app.
  Confirmed live 2026-09-25 and explicitly accepted by the user ("one less
  thing running in the background"). Don't detach it (e.g. a
  `sleep infinity` stdin feeder) without asking first.
  Supporting pieces: `MonadoAdapter._service_proc` is **class-level** (a
  backend switch replaces the adapter instance; a GC'd Popen would close
  the pipe and stop the service), `owns_running_service()` exposes it, and
  the tray's Quit asks for confirmation when it's true, listing connected
  VR apps (`vr_app_clients()` filters out `libmonado` status connections).
  `_stop_running_service()` reaps our own child with `wait()` -- polling
  `kill(pid, 0)` on an unreaped zombie would always hit the 8s SIGKILL path.

- Pluggable adapters for Monado/SteamVR/WiVRn, explicitly because the user
  doesn't want to hard-code one backend.
- PySide6/Qt over tkinter, for a genuinely cross-platform, modern-looking
  window.
- The noise-mask tray app is **fully absorbed** into this app (one process,
  one tray icon), not kept as a separate always-running thing.
