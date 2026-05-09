"""Minimal wlroots-based Wayland compositor that spawns a terminal on startup.

Just enough to bring up an output, accept xdg-shell clients, render a scene,
and exec alacritty. No input handling, no window management.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

from bindings import ffi, lib, add_listener, remove_listener


WLR_DEBUG = 3  # enum wlr_log_importance


def main(startup_cmd: str = "alacritty") -> int:
    lib.wlr_log_init(WLR_DEBUG, ffi.NULL)

    display = lib.wl_display_create()
    loop = lib.wl_display_get_event_loop(display)

    backend = lib.wlr_backend_autocreate(loop, ffi.NULL)
    if backend == ffi.NULL:
        sys.stderr.write("failed to create wlr_backend\n")
        return 1

    renderer = lib.wlr_renderer_autocreate(backend)
    lib.wlr_renderer_init_wl_display(renderer, display)

    allocator = lib.wlr_allocator_autocreate(backend, renderer)

    lib.wlr_compositor_create(display, 5, renderer)
    lib.wlr_subcompositor_create(display)
    lib.wlr_data_device_manager_create(display)

    output_layout = lib.wlr_output_layout_create(display)
    scene = lib.wlr_scene_create()
    scene_layout = lib.wlr_scene_attach_output_layout(scene, output_layout)

    xdg_shell = lib.wlr_xdg_shell_create(display, 3)

    # First (and assumed only) output, populated on the new_output event.
    primary_output = [ffi.NULL]

    # --- Output handling ----------------------------------------------------
    def on_frame_for(wlr_output):
        ts = ffi.new("struct timespec *")
        def _on_frame(_data):
            scene_output = lib.wlr_scene_get_scene_output(scene, wlr_output)
            lib.wlr_scene_output_commit(scene_output, ffi.NULL)
            ns = time.clock_gettime_ns(time.CLOCK_MONOTONIC)
            ts.tv_sec = ns // 1_000_000_000
            ts.tv_nsec = ns % 1_000_000_000
            lib.wlr_scene_output_send_frame_done(scene_output, ts)
        return _on_frame

    def on_new_output(data):
        wlr_output = ffi.cast("struct wlr_output *", data)
        if primary_output[0] == ffi.NULL:
            primary_output[0] = wlr_output
        lib.wlr_output_init_render(wlr_output, allocator, renderer)
        state = lib.pywl_output_state_new()
        lib.wlr_output_state_set_enabled(state, True)
        mode = lib.wlr_output_preferred_mode(wlr_output)
        if mode != ffi.NULL:
            lib.wlr_output_state_set_mode(state, mode)
        lib.wlr_output_commit_state(wlr_output, state)
        lib.pywl_output_state_free(state)
        layout_output = lib.wlr_output_layout_add_auto(
            output_layout, wlr_output)
        scene_output = lib.wlr_scene_output_create(scene, wlr_output)
        lib.wlr_scene_output_layout_add_output(
            scene_layout, layout_output, scene_output)
        add_listener(
            lib.pywl_output_frame(wlr_output), on_frame_for(wlr_output))

    add_listener(lib.pywl_backend_new_output(backend), on_new_output)

    # --- Input handling -----------------------------------------------------
    seat = lib.wlr_seat_create(display, b"seat0")
    has_keyboard = [False]

    def attach_keyboard(device):
        kb = lib.wlr_keyboard_from_input_device(device)
        # Default xkb keymap ("us").
        ctx = lib.xkb_context_new(0)
        keymap = lib.xkb_keymap_new_from_names(ctx, ffi.NULL, 0)
        lib.wlr_keyboard_set_keymap(kb, keymap)
        lib.xkb_keymap_unref(keymap)
        lib.xkb_context_unref(ctx)
        lib.wlr_keyboard_set_repeat_info(kb, 25, 600)

        def on_modifiers(_data):
            lib.wlr_seat_set_keyboard(seat, kb)
            lib.wlr_seat_keyboard_notify_modifiers(seat,
                lib.pywl_keyboard_modifiers_ptr(kb))

        def on_key(data):
            ev = ffi.cast("struct wlr_keyboard_key_event *", data)
            lib.wlr_seat_set_keyboard(seat, kb)
            lib.wlr_seat_keyboard_notify_key(seat, ev.time_msec,
                ev.keycode, ev.state)

        add_listener(lib.pywl_keyboard_modifiers_signal(kb), on_modifiers)
        add_listener(lib.pywl_keyboard_key_signal(kb), on_key)
        lib.wlr_seat_set_keyboard(seat, kb)
        has_keyboard[0] = True

    def on_new_input(data):
        device = ffi.cast("struct wlr_input_device *", data)
        if lib.pywl_input_device_type(device) == lib.WLR_INPUT_DEVICE_KEYBOARD:
            attach_keyboard(device)
        caps = lib.WL_SEAT_CAPABILITY_POINTER
        if has_keyboard[0]:
            caps |= lib.WL_SEAT_CAPABILITY_KEYBOARD
        lib.wlr_seat_set_capabilities(seat, caps)

    add_listener(lib.pywl_backend_new_input(backend), on_new_input)

    def focus_surface(surface):
        # NULL keycodes/modifiers means "no keys held, no modifiers active"
        # at focus-enter time; subsequent key/modifier events refresh state.
        lib.wlr_seat_keyboard_notify_enter(seat, surface, ffi.NULL, 0, ffi.NULL)

    # --- xdg-shell handling -------------------------------------------------
    def commit_handler_for(xdg_toplevel, scene_tree):
        base = lib.pywl_toplevel_base(xdg_toplevel)
        def _on_commit(_data):
            # Initial commit requires a configure so the client can map.
            # Place the window centered at 80% of the output's size.
            if lib.pywl_xdg_surface_initial_commit(base):
                out = primary_output[0]
                if out != ffi.NULL:
                    ow = lib.pywl_output_width(out)
                    oh = lib.pywl_output_height(out)
                    w, h = int(ow * 0.8), int(oh * 0.8)
                    lib.wlr_xdg_toplevel_set_size(xdg_toplevel, w, h)
                    lib.wlr_scene_node_set_position(
                        lib.pywl_scene_tree_node(scene_tree),
                        (ow - w) // 2, (oh - h) // 2,
                    )
                else:
                    lib.wlr_xdg_toplevel_set_size(xdg_toplevel, 0, 0)
        return _on_commit

    def on_new_xdg_toplevel(data):
        xdg_toplevel = ffi.cast("struct wlr_xdg_toplevel *", data)
        base = lib.pywl_toplevel_base(xdg_toplevel)
        scene_tree = lib.wlr_scene_xdg_surface_create(
            lib.pywl_scene_tree(scene), base)
        surface = lib.pywl_xdg_surface_surface(base)

        # Listeners attached to this toplevel's surface signals must be
        # removed before wlroots frees the surface, otherwise libwayland
        # aborts on the dangling listener_list. Track them and tear them
        # down on the toplevel's destroy event.
        keys = [
            add_listener(lib.pywl_surface_commit(surface),
                         commit_handler_for(xdg_toplevel, scene_tree)),
            add_listener(lib.pywl_surface_map(surface),
                         lambda _d: focus_surface(surface)),
        ]

        def on_destroy(_data):
            for k in keys:
                remove_listener(k)
            remove_listener(destroy_key)

        destroy_key = add_listener(
            lib.pywl_xdg_toplevel_destroy(xdg_toplevel), on_destroy)

    add_listener(
        lib.pywl_xdg_shell_new_toplevel(xdg_shell), on_new_xdg_toplevel)

    socket = lib.wl_display_add_socket_auto(display)
    if socket == ffi.NULL:
        lib.wlr_backend_destroy(backend)
        return 1
    socket_str = ffi.string(socket).decode()

    if not lib.wlr_backend_start(backend):
        lib.wlr_backend_destroy(backend)
        lib.wl_display_destroy(display)
        return 1

    os.environ["WAYLAND_DISPLAY"] = socket_str
    sys.stderr.write(f"Running on WAYLAND_DISPLAY={socket_str}\n")

    if startup_cmd:
        subprocess.Popen(startup_cmd, shell=True)

    lib.wl_display_run(display)

    lib.wl_display_destroy_clients(display)
    lib.wl_display_destroy(display)
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "alacritty"
    sys.exit(main(cmd))
