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
  (`~/.config/vr-noise-mask/config.json`, since deleted).
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
5. ~~Retire the old standalone app~~ -- done 2026-09-25: config confirmed
   identical to the migrated one, then `~/.local/share/vr-noise-mask` and
   `~/.config/vr-noise-mask` deleted by the user.
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
- **Late lighthouse devices** (`Cannot add device after setup; consider
  increasing LH_DISCOVER_WAIT_MS`): steamvr_lh drops any device arriving
  after its discovery window (default 3000 ms, `steamvr_lh.cpp`), and also
  any powered on after the service started -- and logs *no* serial/class
  for it, so it can't be identified. `MonadoAdapter` counts these warnings
  in its own service log and appends placeholder `DeviceStatus` rows
  (`placeholder=True`); only when `owns_running_service()`, since otherwise
  that log is from an older run. The wait is user-settable in the Devices
  tab (generic `service_options()` hook, saved under
  `cfg["service_options"][backend]`, applied as `LH_DISCOVER_WAIT_MS` on
  restart). Verified live: 500 ms -> only the HMD added, 5 placeholders;
  6000 ms -> everything, including a **third base station
  `LHB-BC26C101`** that the 3000 ms default missed.
- A late *base station* still yields a placeholder row, but once added it
  doesn't appear in the table (libmonado doesn't expose base stations), so
  the row count can shrink after a fix.
- **Startup/discovery placeholders:** while the service *we* launched is
  still starting, `MonadoAdapter.poll()` doesn't connect (libmonado can't
  talk to it until device creation is done, and may block trying) and
  instead returns placeholder rows parsed from its log by
  `_ServiceLogScanner`: phase markers "Lighthouse initialization complete"
  -> "Device search time complete", plus each "Found lighthouse <kind>:
  <serial>" line. `BackendSnapshot.busy` carries the neutral status text.
- **Body roles can be imported from SteamVR** ("Import body roles from
  SteamVR..." button; `roles.find_steamvr_settings()` checks the local
  Steam install and `<drive>/Program Files (x86)/Steam/config/` under
  `/mnt/*` and `/run/media/*/*`). Import is one-shot into
  `cfg["device_roles"]`, never a live link -- the user wanted in-app
  assignment persisted locally by serial. Must happen inside the app: it
  holds cfg in memory and would overwrite external edits on its next save.
- **Headset display seen as plain "NVIDIA" (DP-4)** on one 2026-09-25 run
  instead of "Valve Corporation Index HMD": Monado's NVIDIA allowlist
  didn't match, so no direct mode ("Found no connectors available for
  direct mode") and swapchain errors. The connector had the NVIDIA
  driver's blank fallback EDID ("NVD", product 0, 640x480 only). A DP
  replug did NOT fix it (real hotplug events, still blank); **power-cycling
  the headset did** (then "VLV" "Index HMD", 2880x1600). No software reset
  exists for the Index display (Monado's HID power report is Vive/Vive
  Pro-only; lighthouse_console "reboot" looks like ISP/bootloader mode --
  don't). `MonadoAdapter` warns on this (`_headset_display_state()`, sysfs
  EDIDs), also before the service starts. **Boundary decided with the
  user:** VR Companion only detects/warns and must not drive Home
  Assistant; the user's vr-ha-agent session was asked to expose the display
  state as an HA sensor so an HA automation can cycle the link box's smart
  plug. NVIDIA write-up: the user's "SteamVR setup" session.
- **"Simulated HMD" = Monado didn't find the headset.** With no Index on
  USB, no builder claims a head device and Monado silently falls back to
  the "legacy" builder's Simulated HMD -- no lighthouse driver, so no
  controllers and no late-device warnings either. Seen live 2026-09-25: the
  Index's internal hub (`28de:2613`, sysfs "USB2744") stuck in a reset loop
  with `28de:2300` HMD + `28de:2102` radios never enumerating; fix is a
  link-box power-cycle. `MonadoAdapter` now adds a `BackendSnapshot.warnings`
  entry for this (checking sysfs for `28de:2300` on Linux), shown above the
  Devices table alongside the late-device warning. Coincided with a
  monado-git r844 -> r849 upgrade at 14:27, but the USB drop was 16s
  *earlier* and none of those 5 commits touch builders/steamvr_lh.
- **Tracker body roles (waist/feet/...) aren't consumed by anything on
  Linux** (checked 2026-09-25 against current upstream): libmonado only
  *reports* head/left/right/gamepad/eyes roles and has no setter; Monado's
  `XR_HTCX_vive_tracker_interaction` is `ALWAYS_DISABLED`
  (`oxr_extension_support.py`); xrizer (0989a7f = upstream HEAD) exposes all
  trackers as generic `vive_tracker_handheld_object` and its IVRSettings is
  a stub; Monado's new built-in OpenVR state tracker (MR 2862) is also
  role-less. VRChat doesn't need roles (calibration assigns by position).
  So the Devices tab's body-role dropdown (in the merged "Role" column, only for trackers the runtime gave no role; `roles.py`, saved per serial in
  `cfg["device_roles"]`, keyed by SteamVR's `TrackerRole_*` names) is
  informational for now. Getting roles to games would mean patching xrizer
  to read that mapping and report e.g. `vive_tracker_waist`.
  The user's Windows roles live in
  `/mnt/c/Program Files (x86)/Steam/config/steamvr.vrsettings` ("trackers"
  section, e.g. `LHR-92DD1F67` = Waist); the user chose to assign in-app
  rather than import. `XRIZER_TRACKER_SERIALS` (`;`-separated serials) is
  xrizer's only tracker knob: it makes any device (e.g. a controller) a
  generic tracker, and must be in the *game's* env, not monado-service's.
- `/proc/<pid>/environ` of `monado-service` isn't readable (it runs with
  extra capabilities for its realtime threads) -- verify env effects via
  its log instead.

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

- **Nothing blocking runs on the UI thread.** All adapter calls (poll,
  is_service_running, restart, disconnect) go through `AdapterWorker` on
  DevicesTab's single QThread via queued signals -- which also serialises
  polls against restarts. The audio engine ticks on its own thread
  (`NoiseEngine.start_ticker()`). Reason: the user saw the whole app freeze
  briefly right after "Start service" (libmonado blocks while the new
  service initialises; the engine's pactl/PortAudio work also ran on the UI
  thread). `MainWindow` has a heartbeat watchdog printing
  "UI thread stalled for N ms" to stdout if this ever regresses.

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
