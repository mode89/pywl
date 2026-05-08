Findings from building a pywlroots compositor.

## Maintaining this file

- Append-only mindset: when something non-obvious comes up (API quirk, version-specific behavior, race, workaround), add a bullet to the relevant section.
- Bullets must be **non-obvious** — facts you'd otherwise re-discover by trial and error. Don't restate the pywlroots README or wayland tutorials.
- Each bullet: state the fact and, if relevant, *why* / what the symptom was. Symptoms matter more than mechanism — they're how you'll re-find the entry by grep.
- Keep it terse. If a bullet grows past ~3 lines, it probably belongs in code comments, not here.
- When a workaround becomes obsolete (pywlroots fixes the binding, wlroots changes behavior), strike it through or delete it; don't leave stale advice.
- Group by area (Wiring, Backends, Signals, Teardown, Input, …); add a new section if a new area emerges.

## Wiring

- `wlroots.helper.build_compositor(display)` returns `(compositor, allocator, renderer, backend, subcompositor)`. Use it; manual setup is more code.
- `OutputLayout` + `Scene` + `Scene.attach_output_layout(layout)` gives you automatic rendering. Per-output: `SceneOutput.create(scene, output)`, then in `frame_event` call `scene_output.commit()` and `send_frame_done(Timespec.get_monotonic_time())`.
- xdg toplevels appear by `Scene.xdg_surface_create(scene.tree, xdg_surface)` from the `xdg_shell.new_surface_event`. Filter on `XdgSurfaceRole.TOPLEVEL`.
- Globals foot (and most clients) require: `wl_compositor` (auto), `xdg_wm_base` (`XdgShell`), `wl_data_device_manager` (`DataDeviceManager`), `wl_seat` (`Seat`). Missing any of these → client errors out at startup.

## Listeners (gotcha)

- `signal.add(callback)` does not work — pywayland sets `_signal` on the callback object. Bound methods reject attribute writes. **Always wrap: `signal.add(Listener(callback))`.**
- pywayland holds only weak refs through cffi cdata. Keep strong references to every `Listener` (and `Keyboard`, etc.) on `self`, or they get GC'd and the signal goes silent.

## Backends

- `BackendType.AUTO` returns a multi-backend. Detection helpers like `wlr_backend_is_wl` are not exposed by pywlroots; reach via `ctypes.CDLL("libwlroots.so.12")` if needed.
- The wlroots **`wl` backend creates 1 output by itself on this build** (no env var needed). Calling `wlr_wl_output_create()` adds a second one. We don't need to call it.
- Headless/wl outputs return `None` from `output.preferred_mode()`. Skipping `enable()/commit()` in that branch was the bug behind "window appears in Hyprland but stays empty" — the output was created but never enabled, so no frames were committed and clients never got a buffer.
  Fix: always set a mode (`set_custom_mode(CustomMode(1280, 720, 0))` if no preferred), then `enable()` + `commit()`.
- **Subscribe to `output.request_state_event`** and `output.commit(event.state)` — that's how the wl/x11 backend propagates host resize / scale / monitor-change. Without it, the nested output renders at the initial size/scale forever, which is what caused the "wrong pixel ratio".

## Signals & Ctrl-C

- `display.run()` blocks in C, so Python signal handlers queued during dispatch never fire — Ctrl-C does nothing.
- libwayland's `wl_event_loop_add_signal` (signalfd) appears to work in isolation but raced with backend startup in our integration; ~10% of runs ignored SIGINT entirely. Did not investigate root cause.
- What works reliably: drive the loop ourselves with `loop.dispatch(200)` in a Python `while _running:` loop, and install Python `signal.signal(SIGINT/SIGTERM, ...)` handlers that flip `_running`. The 200ms timeout bounds shutdown latency.
- **Install handlers at module top before importing pywlroots.** Otherwise SIGINT during `build_compositor` hits Python's default `KeyboardInterrupt` handler mid-init, gets swallowed somewhere in cffi, and leaves the process running with no handler. Verified: 30/30 trials clean after this fix; ~1/10 fail before it.

## Teardown

- Adding a `Seat` introduced a reliable segfault on shutdown. Python's GC destroys pywlroots wrappers in an order libwayland doesn't accept (Seat freed after display globals). `display.destroy_clients()` + `display.destroy()` first didn't help.
- Pragmatic fix: **`os._exit(0)` at end of `run()`** — skip Python finalization. The OS reclaims everything.

## Input

- Listen on `backend.new_input_event` (wraps `InputDevice`). Filter `device.type == InputDeviceType.KEYBOARD`.
- `Keyboard.from_input_device(device)`, then `set_keymap(xkb.Context().keymap_new_from_names())` and `set_repeat_info(25, 600)`.
- Forward keyboard signals manually: `key_event` → `seat.keyboard_notify_key(event)`, `modifiers_event` → `seat.keyboard_notify_modifiers(keyboard.modifiers)`. wlroots does not auto-pump them.
- Call `seat.set_keyboard(kbd)` and `seat.set_capabilities(WlSeat.capability.keyboard | .pointer)` so clients see the seat.
- **Focus on surface map, not on xdg-surface creation.** XdgSurface in 0.17 has no `map_event`; the map/unmap events live on the underlying `Surface` (`xdg_surface.surface.map_event`). Hook that and call `seat.keyboard_notify_enter(surface, keyboard)` there.
- `seat.keyboard` is not a property — it's `seat.get_keyboard()`.

## Testing discipline

- Always bound external commands with `timeout`, and inner test loops with explicit `kill -9` fallbacks. A stuck compositor will otherwise hang the harness indefinitely.
- `wait $PID; echo $?` after `kill -INT $PID` returns the status of `kill`, not the python process, when the process was already killed by something else. Don't trust that as success — verify with `kill -0 $PID` first.
