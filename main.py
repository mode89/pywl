"""Minimal wlroots-based Wayland compositor that spawns a terminal on startup.

Just enough to bring up an output, accept xdg-shell clients, render a scene,
and exec alacritty. No input handling, no window management.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
import signal
import subprocess
import sys
import time

from bindings import ffi, lib, listen


WLR_DEBUG = 3  # enum wlr_log_importance


@dataclass
class Rect:
    x: int
    y: int
    width: int
    height: int


@dataclass
class Output:
    wlr_output: object
    width: int
    height: int
    enabled: bool = True
    listeners: list[object] = field(default_factory=list)


@dataclass
class Window:
    xdg_toplevel: object
    scene_tree: object
    surface: object
    output: Output | None
    geometry: Rect
    handle: object = None  # ffi.new_handle keepalive for scene_tree data
    listeners: list[object] = field(default_factory=list)


@dataclass
class Keyboard:
    wlr_keyboard: object
    listeners: list[object] = field(default_factory=list)


@dataclass
class Cursor:
    wlr_cursor: object
    xcursor_mgr: object
    listeners: list[object]


@dataclass
class MoveGrab:
    window: Window
    grab_dx: float
    grab_dy: float


@dataclass
class Server:
    display: object
    loop: object
    backend: object
    renderer: object
    allocator: object
    output_layout: object
    scene: object
    scene_layout: object
    xdg_shell: object
    seat: object
    outputs: list[Output]
    windows: list[Window]
    keyboards: list[Keyboard]
    listeners: list[object]
    cursor: Cursor | None = None
    keyboard_focus: Window | None = None
    move_grab: MoveGrab | None = None
    primary_output: Output | None = None


def main(startup_cmd: str = "alacritty") -> int:
    server = create_server()
    if server is None:
        return 1

    socket = lib.wl_display_add_socket_auto(server.display)
    if socket == ffi.NULL:
        lib.wlr_backend_destroy(server.backend)
        return 1
    socket_str = ffi.string(socket).decode()

    server.listeners = [
        listen(
            lib.pywl_backend_new_output(server.backend),
            lambda data: on_output_new(server, data)),
        listen(
            lib.pywl_backend_new_input(server.backend),
            lambda data: on_input_new(server, data)),
        listen(
            lib.pywl_xdg_shell_new_toplevel(server.xdg_shell),
            lambda data: on_window_new(server, data)),
    ]

    server.cursor = create_cursor(server)

    if not lib.wlr_backend_start(server.backend):
        lib.wlr_backend_destroy(server.backend)
        lib.wl_display_destroy(server.display)
        return 1

    os.environ["WAYLAND_DISPLAY"] = socket_str
    sys.stderr.write(f"Running on WAYLAND_DISPLAY={socket_str}\n")

    if startup_cmd:
        subprocess.Popen(startup_cmd, shell=True)

    run_event_loop(server)

    destroy_server(server)

    print(f"listeners remaining: {len(listen.listeners)}", file=sys.stderr)
    return 0


def create_server() -> Server | None:
    lib.wlr_log_init(WLR_DEBUG, ffi.NULL)

    display = lib.wl_display_create()
    loop = lib.wl_display_get_event_loop(display)

    backend = lib.wlr_backend_autocreate(loop, ffi.NULL)
    if backend == ffi.NULL:
        sys.stderr.write("failed to create wlr_backend\n")
        return None

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


    return Server(
        display=display,
        loop=loop,
        backend=backend,
        renderer=renderer,
        allocator=allocator,
        output_layout=output_layout,
        scene=scene,
        scene_layout=scene_layout,
        xdg_shell=xdg_shell,
        seat=lib.wlr_seat_create(display, b"seat0"),
        outputs=[],
        windows=[],
        keyboards=[],
        listeners=[],
    )


def run_event_loop(server: Server) -> None:
    stop = False

    def on_stop(_sig, _frm):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, on_stop)
    signal.signal(signal.SIGTERM, on_stop)
    while not stop:
        lib.wl_display_flush_clients(server.display)
        lib.wl_event_loop_dispatch(server.loop, 100)


def destroy_server(server: Server) -> None:
    lib.wl_display_destroy_clients(server.display)
    for handle in server.listeners:
        handle.remove()
    lib.wlr_scene_node_destroy(ffi.addressof(server.scene.tree.node))
    destroy_cursor(server.cursor)
    lib.wlr_allocator_destroy(server.allocator)
    lib.wlr_renderer_destroy(server.renderer)
    lib.wl_display_destroy(server.display)


def on_output_new(server: Server, data) -> Output:
    wlr_output = ffi.cast("struct wlr_output *", data)
    lib.wlr_output_init_render(wlr_output, server.allocator, server.renderer)
    output_state = lib.pywl_output_state_new()
    lib.wlr_output_state_set_enabled(output_state, True)
    mode = lib.wlr_output_preferred_mode(wlr_output)
    if mode != ffi.NULL:
        lib.wlr_output_state_set_mode(output_state, mode)
    lib.wlr_output_commit_state(wlr_output, output_state)
    lib.pywl_output_state_free(output_state)

    output = Output(
        wlr_output,
        wlr_output.width,
        wlr_output.height,
    )
    server.outputs.append(output)
    if server.primary_output is None:
        server.primary_output = output

    layout_output = lib.wlr_output_layout_add_auto(
        server.output_layout, wlr_output)
    scene_output = lib.wlr_scene_output_create(server.scene, wlr_output)
    lib.wlr_scene_output_layout_add_output(
        server.scene_layout, layout_output, scene_output)

    timestamp = ffi.new("struct timespec *")
    def on_frame(data):
        ns = time.clock_gettime_ns(time.CLOCK_MONOTONIC)
        timestamp.tv_sec = ns // 1_000_000_000
        timestamp.tv_nsec = ns % 1_000_000_000
        on_output_frame(server, output, timestamp, data)

    output.listeners = [
        listen(lib.pywl_output_frame(wlr_output), on_frame),
        listen(
            lib.pywl_output_request_state(wlr_output),
            lambda data: on_output_request_state(server, output, data)),
        listen(
            lib.pywl_output_destroy_signal(wlr_output),
            lambda data: on_output_destroy(server, output, data)),
    ]

    return output


def on_output_request_state(server: Server, output: Output, data):
    ev = ffi.cast("struct wlr_output_event_request_state *", data)
    lib.wlr_output_commit_state(output.wlr_output, ev.state)


def on_output_frame(server: Server, output: Output, timestamp, _data):
    scene_output = lib.wlr_scene_get_scene_output(
        server.scene, output.wlr_output)
    lib.wlr_scene_output_commit(scene_output, ffi.NULL)
    lib.wlr_scene_output_send_frame_done(scene_output, timestamp)


def on_output_destroy(server: Server, output: Output, _data) -> None:
    for listener in output.listeners:
        listener.remove()
    if output in server.outputs:
        server.outputs.remove(output)
    if server.primary_output is output:
        server.primary_output = None


def on_input_new(server: Server, data) -> None:
    device = ffi.cast("struct wlr_input_device *", data)
    dtype = device.type
    if dtype == lib.WLR_INPUT_DEVICE_KEYBOARD:
        on_keyboard_new(server, device)
    elif dtype == lib.WLR_INPUT_DEVICE_POINTER:
        lib.wlr_cursor_attach_input_device(server.cursor.wlr_cursor, device)
    caps = lib.WL_SEAT_CAPABILITY_POINTER
    if server.keyboards:
        caps |= lib.WL_SEAT_CAPABILITY_KEYBOARD
    lib.wlr_seat_set_capabilities(server.seat, caps)


def on_keyboard_new(server: Server, device) -> Keyboard:
    wlr_keyboard = lib.wlr_keyboard_from_input_device(device)
    ctx = lib.xkb_context_new(0)
    keymap = lib.xkb_keymap_new_from_names(ctx, ffi.NULL, 0)
    lib.wlr_keyboard_set_keymap(wlr_keyboard, keymap)
    lib.xkb_keymap_unref(keymap)
    lib.xkb_context_unref(ctx)
    lib.wlr_keyboard_set_repeat_info(wlr_keyboard, 25, 600)

    keyboard = Keyboard(wlr_keyboard)
    server.keyboards.append(keyboard)

    keyboard.listeners = [
        listen(
            lib.pywl_keyboard_modifiers_signal(wlr_keyboard),
            lambda data: on_keyboard_modifiers(server, keyboard, data)),
        listen(
            lib.pywl_keyboard_key_signal(wlr_keyboard),
            lambda data: on_keyboard_key(server, keyboard, data)),
        listen(
            lib.pywl_input_device_destroy_signal(device),
            lambda data: on_keyboard_destroy(server, keyboard, data)),
    ]

    lib.wlr_seat_set_keyboard(server.seat, wlr_keyboard)
    return keyboard


def on_keyboard_modifiers(server: Server, keyboard: Keyboard, _data) -> None:
    lib.wlr_seat_set_keyboard(server.seat, keyboard.wlr_keyboard)
    lib.wlr_seat_keyboard_notify_modifiers(
        server.seat, ffi.addressof(keyboard.wlr_keyboard.modifiers))


def on_keyboard_key(server: Server, keyboard: Keyboard, data) -> None:
    ev = ffi.cast("struct wlr_keyboard_key_event *", data)
    lib.wlr_seat_set_keyboard(server.seat, keyboard.wlr_keyboard)
    lib.wlr_seat_keyboard_notify_key(
        server.seat,
        ev.time_msec,
        ev.keycode,
        ev.state,
    )


def on_keyboard_destroy(server: Server, keyboard: Keyboard, _data) -> None:
    for listener in keyboard.listeners:
        listener.remove()
    if keyboard in server.keyboards:
        server.keyboards.remove(keyboard)


def create_cursor(server: Server) -> Cursor:
    wlr_cursor = lib.wlr_cursor_create()
    lib.wlr_cursor_attach_output_layout(wlr_cursor, server.output_layout)
    xcursor_mgr = lib.wlr_xcursor_manager_create(ffi.NULL, 24)
    lib.wlr_xcursor_manager_load(xcursor_mgr, 1.0)
    return Cursor(
        wlr_cursor,
        xcursor_mgr,
        [
            listen(
                lib.pywl_cursor_motion(wlr_cursor),
                lambda data: on_cursor_motion(server, data)),
            listen(
                lib.pywl_cursor_motion_absolute(wlr_cursor),
                lambda data: on_cursor_motion_absolute(server, data)),
            listen(
                lib.pywl_cursor_button(wlr_cursor),
                lambda data: on_cursor_button(server, data)),
            listen(
                lib.pywl_cursor_axis(wlr_cursor),
                lambda data: on_cursor_axis(server, data)),
            listen(
                lib.pywl_cursor_frame(wlr_cursor),
                lambda data: on_cursor_frame(server, data)),
        ]
    )


def destroy_cursor(cursor: Cursor) -> None:
    for listener in cursor.listeners:
        listener.remove()
    lib.wlr_xcursor_manager_destroy(cursor.xcursor_mgr)
    lib.wlr_cursor_destroy(cursor.wlr_cursor)


def on_cursor_motion(server: Server, data) -> None:
    ev = ffi.cast("struct wlr_pointer_motion_event *", data)
    lib.wlr_cursor_move(
        server.cursor.wlr_cursor, ffi.NULL,
        ev.delta_x, ev.delta_y)
    process_cursor_motion(server, ev.time_msec)


def on_cursor_motion_absolute(server: Server, data) -> None:
    ev = ffi.cast("struct wlr_pointer_motion_absolute_event *", data)
    lib.wlr_cursor_warp_absolute(
        server.cursor.wlr_cursor, ffi.NULL,
        ev.x, ev.y)
    process_cursor_motion(server, ev.time_msec)


def on_cursor_button(server: Server, data) -> None:
    handle_cursor_button(server, data)


def on_cursor_axis(server: Server, data) -> None:
    ev = ffi.cast("struct wlr_pointer_axis_event *", data)
    lib.wlr_seat_pointer_notify_axis(
        server.seat,
        ev.time_msec,
        ev.orientation,
        ev.delta,
        ev.delta_discrete,
        ev.source,
        ev.relative_direction,
    )


def on_cursor_frame(server: Server, _data) -> None:
    lib.wlr_seat_pointer_notify_frame(server.seat)


def process_cursor_motion(server: Server, time_msec: int) -> None:
    cx = server.cursor.wlr_cursor.x
    cy = server.cursor.wlr_cursor.y
    if server.move_grab is not None:
        grab = server.move_grab
        x = int(cx - grab.grab_dx)
        y = int(cy - grab.grab_dy)
        lib.wlr_scene_node_set_position(
            ffi.addressof(grab.window.scene_tree.node), x, y)
        grab.window.geometry.x = x
        grab.window.geometry.y = y
        return

    hit = surface_at(server, cx, cy)
    if hit is None:
        lib.wlr_cursor_set_xcursor(
            server.cursor.wlr_cursor, server.cursor.xcursor_mgr, b"default")
        lib.wlr_seat_pointer_clear_focus(server.seat)
        return
    _window, surface, sx, sy = hit
    lib.wlr_seat_pointer_notify_enter(server.seat, surface, sx, sy)
    lib.wlr_seat_pointer_notify_motion(server.seat, time_msec, sx, sy)


def handle_cursor_button(server: Server, data) -> None:
    ev = ffi.cast("struct wlr_pointer_button_event *", data)
    button_state = ev.state
    button = ev.button
    time_msec = ev.time_msec
    pressed = button_state == lib.WL_POINTER_BUTTON_STATE_PRESSED

    if pressed:
        kb = lib.wlr_seat_get_keyboard(server.seat)
        mods = lib.wlr_keyboard_get_modifiers(kb) if kb != ffi.NULL else 0
        hit = surface_at(
            server,
            server.cursor.wlr_cursor.x,
            server.cursor.wlr_cursor.y)
        if hit is not None:
            window, surface, _sx, _sy = hit
            if (mods & lib.WLR_MODIFIER_ALT) and button == lib.BTN_LEFT:
                start_move_grab(server, window)
                return
            raise_window(server, window)
            focus_window(server, window, surface)
        else:
            clear_keyboard_focus(server)
    elif server.move_grab is not None:
        server.move_grab = None
        return

    lib.wlr_seat_pointer_notify_button(
        server.seat, time_msec, button, button_state)


def surface_at(server: Server, lx, ly):
    """Return (window, surface, sx, sy), or None if there is no hit."""
    sx = ffi.new("double *")
    sy = ffi.new("double *")
    node = lib.wlr_scene_node_at(
        ffi.addressof(server.scene.tree.node), lx, ly, sx, sy)
    if node == ffi.NULL:
        return None
    if node.type != lib.WLR_SCENE_NODE_BUFFER:
        return None
    buf = lib.wlr_scene_buffer_from_node(node)
    ss = lib.wlr_scene_surface_try_from_buffer(buf)
    if ss == ffi.NULL:
        return None

    tree = node.parent
    while tree != ffi.NULL and tree.node.data == ffi.NULL:
        tree = tree.node.parent
    if tree == ffi.NULL:
        return None

    window = ffi.from_handle(tree.node.data)
    if window not in server.windows:
        return None
    return window, ss.surface, sx[0], sy[0]


def on_window_new(server: Server, data) -> Window:
    xdg_toplevel = ffi.cast("struct wlr_xdg_toplevel *", data)
    base = xdg_toplevel.base
    scene_tree = lib.wlr_scene_xdg_surface_create(
        ffi.addressof(server.scene.tree), base)
    surface = base.surface

    window = Window(
        xdg_toplevel, scene_tree, surface,
        server.primary_output, Rect(0, 0, 0, 0),
    )
    window.handle = ffi.new_handle(window)
    server.windows.append(window)

    scene_tree.node.data = window.handle

    window.listeners = [
        listen(
            lib.pywl_surface_commit(surface),
            lambda data: on_window_commit(server, window, data)),
        listen(
            lib.pywl_surface_map(surface),
            lambda data: on_window_map(server, window, data)),
        listen(
            lib.pywl_xdg_toplevel_destroy(xdg_toplevel),
            lambda data: on_window_destroy(server, window, data)),
    ]

    return window


def on_window_destroy(server: Server, window: Window, _data) -> None:
    """Called when an app closes one of its windows."""
    for listener in window.listeners:
        listener.remove()
    if window in server.windows:
        server.windows.remove(window)
    if server.keyboard_focus is window:
        server.keyboard_focus = None
    if server.move_grab is not None and server.move_grab.window is window:
        server.move_grab = None


def on_window_commit(server: Server, window: Window, _data) -> None:
    """Called whenever an app updates a window."""
    base = window.xdg_toplevel.base
    if not base.initial_commit:
        return

    out = current_output_ptr(server)
    if out != ffi.NULL:
        ow = out.width
        oh = out.height
        w, h = int(ow * 0.8), int(oh * 0.8)
        geometry = Rect((ow - w) // 2, (oh - h) // 2, w, h)
        lib.wlr_xdg_toplevel_set_size(
            window.xdg_toplevel, geometry.width, geometry.height)
        lib.wlr_scene_node_set_position(
            ffi.addressof(window.scene_tree.node),
            geometry.x, geometry.y)
    else:
        geometry = Rect(0, 0, 0, 0)
        lib.wlr_xdg_toplevel_set_size(window.xdg_toplevel, 0, 0)

    window.geometry = geometry
    window.output = server.primary_output


def on_window_map(server: Server, window: Window, _data) -> None:
    """Called the moment a window first has pixels to show on screen."""
    focus_window(server, window, window.surface)


def current_output_ptr(server: Server):
    output = server.primary_output
    return output.wlr_output if output is not None else ffi.NULL


def focus_surface(server: Server, surface) -> None:
    lib.wlr_seat_keyboard_notify_enter(
        server.seat, surface, ffi.NULL, 0, ffi.NULL)


def focus_window(server: Server, window: Window, surface) -> None:
    focus_surface(server, surface)
    for w in server.windows:
        lib.wlr_xdg_toplevel_set_activated(w.xdg_toplevel, w is window)
    server.keyboard_focus = window


def clear_keyboard_focus(server: Server) -> None:
    lib.wlr_seat_keyboard_clear_focus(server.seat)
    for window in server.windows:
        lib.wlr_xdg_toplevel_set_activated(window.xdg_toplevel, False)
    server.keyboard_focus = None


def start_move_grab(server: Server, window: Window) -> None:
    node = window.scene_tree.node
    gx = server.cursor.wlr_cursor.x - node.x
    gy = server.cursor.wlr_cursor.y - node.y
    server.move_grab = MoveGrab(window, gx, gy)


def raise_window(server: Server, window: Window) -> None:
    lib.wlr_scene_node_raise_to_top(ffi.addressof(window.scene_tree.node))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "alacritty"
    sys.exit(main(cmd))
