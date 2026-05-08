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
- Pointers: own a single `Cursor(output_layout)` + `XCursorManager(None, 24)` for the whole compositor. On each `InputDeviceType.POINTER` from `new_input_event`, `cursor.attach_input_device(device)` — don't make a Cursor per device.
- Cursor signals (`motion_event`, `motion_absolute_event`, `button_event`, `axis_event`, `frame_event`) are on the `Cursor` instance, not on the input device. Attaching the device routes its raw events through them.
- Relative motion: `cursor.move(event.delta_x, event.delta_y, input_device=event.pointer.base)`. Absolute: `cursor.warp(WarpMode.AbsoluteClosest, event.x, event.y, input_device=event.pointer.base)`. `event.pointer.base` is the `InputDevice` (needed so per-device mapping is respected).
- Hit-testing: `scene.tree.node.node_at(cursor.x, cursor.y)` → `(node, sx, sy)`. To get the surface: check `node.type == SceneNodeType.BUFFER`, then `SceneSurface.from_buffer(SceneBuffer.from_node(node))` — may still return None for non-surface buffers.
- Forward to seat: on motion, `pointer_notify_enter(surface, sx, sy)` (idempotent — wlroots no-ops if already focused) followed by `pointer_notify_motion(time, sx, sy)`; on no surface, `pointer_notify_clear_focus()` and `cursor.set_xcursor(manager, "default")` so the root area gets the default arrow. Buttons/axis pass straight through; emit `pointer_notify_frame()` from `frame_event`.
- Call `cursor_manager.load(output.scale)` from `_on_new_output` to ensure the theme is rasterized at that output's scale.

## Focus

- Tag each toplevel's `scene_tree.node.data` with a Python `View` object at xdg-surface creation time. Click hit-test: `scene.tree.node.node_at(x, y)` returns a leaf (a buffer/surface node); walk `node.parent.node` upward until `node.data` is a `View`. That's how you map any subsurface/decoration hit back to the owning toplevel.
- `SceneNode.data` setter stashes the value in a `WeakValueDictionary` keyed on the value, so the value must be hashable. `@dataclass` defaults to `eq=True` → `__hash__ = None` → `TypeError: unhashable type`. Use `@dataclass(eq=False)` (or a plain class) for anything you store via `node.data = ...`.
- Focus transfer: `prev.set_activated(False)`, `view.scene_tree.node.raise_to_top()`, `view.xdg_surface.set_activated(True)`, then `seat.keyboard_notify_enter(surface, keyboard)` (handles leave on the previous focus). `XdgSurface.set_activated` works directly — no need to dig into `xdg_surface.toplevel`.
- Maintain a `views: list[View]` ordered bottom-to-top (focus moves the entry to the end). On unmap, drop the view; if it was focused, refocus `views[-1]` (or `keyboard_clear_focus()` if empty). Without this fallback, closing the focused window leaves the seat focused on a destroyed surface.
- Click-to-focus belongs in the **press** branch of `button_event` (`ButtonState.PRESSED`); always forward the button to the seat afterwards regardless. Focusing on release feels laggy and breaks drag-to-select inside clients.

## Compositor key bindings

- Canonical wlroots pattern is **keysym matching, not raw keycode comparison** (see `tinywl.c` and qtile's wayland backend). `wlr_keyboard_key_event.keycode` is the libinput/evdev keycode; convert to xkb keycode with `+ 8` (X11 offset) and feed it through `xkb_state_key_get_one_sym(keyboard._ptr.xkb_state, xkb_keycode)` to get a layout-aware keysym.
- pywlroots doesn't wrap `wlr_keyboard.xkb_state` as a Python property, but the C field is reachable as `keyboard._ptr.xkb_state`. Likewise the libxkbcommon functions (`xkb_keysym_from_name`, `xkb_state_key_get_one_sym`, `xkb_keysym_to_lower`, …) are already present on `from wlroots import lib`. No separate cffi build needed.
- Lowercase the resulting keysym with `xkb_keysym_to_lower` so Shift+letter compares equal to the unshifted form (`XKB_KEY_Q` → `XKB_KEY_q`). Match against `xkb_keysym_from_name(b"q", XKB_KEYSYM_NO_FLAGS)` — don't pass `XKB_KEYSYM_CASE_INSENSITIVE`, that just lowercases the lookup, not the runtime sym.
- `keyboard.modifier` returns a `KeyboardModifier` IntFlag with the currently active modifiers. To allow extra modifiers (CapsLock, NumLock) while requiring at least Alt+Shift, mask: `(mods & required) == required` rather than `mods == required`.
- Filter on `event.state == WlKeyboard.key_state.pressed` (imported from `pywayland.protocol.wayland`); acting on release feels laggy and double-fires repeats. `WlKeyboard.key_state` also has a `repeated` value — don't accidentally trigger on key repeat.
- Intercepting a binding means **not forwarding it to the focused client**. Return early from the key handler before `seat.keyboard_notify_key(event)` so the client never sees the press; otherwise apps will also see the chord and might react (e.g. close a tab).
- Can't test bindings via `wtype` against this compositor: wtype needs `wp_virtual_keyboard_v1`, which we don't expose. Direct unit-style calls to the binding handler with mocked event/keyboard (and `mock.patch` over `event_keysym`) are the path of least resistance.

## Testing discipline

- Always bound external commands with `timeout`, and inner test loops with explicit `kill -9` fallbacks. A stuck compositor will otherwise hang the harness indefinitely.
- `wait $PID; echo $?` after `kill -INT $PID` returns the status of `kill`, not the python process, when the process was already killed by something else. Don't trust that as success — verify with `kill -0 $PID` first.
