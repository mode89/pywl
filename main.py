"""Minimal wlroots-based Wayland compositor that spawns a terminal on startup.

Just enough to bring up an output, accept xdg-shell clients, render a scene,
and exec alacritty. No input handling, no view management.
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
class Output:
    wlr_output: object
    width: int
    height: int
    enabled: bool = True
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
class Rect:
    x: int
    y: int
    width: int
    height: int


@dataclass
class View:
    scene_tree: object
    surface: object
    geometry: Rect
    handle: object = None  # ffi.new_handle keepalive for scene_tree data
    listeners: list[object] = field(default_factory=list)


@dataclass
class XdgView(View):
    xdg_toplevel: object = None


@dataclass
class Popup:
    xdg_popup: object
    listeners: list[object] = field(default_factory=list)


@dataclass
class MoveGrab:
    view: XdgView
    grab_dx: float
    grab_dy: float


@dataclass
class ResizeGrab:
    view: XdgView
    start_cx: float
    start_cy: float
    start_w: int
    start_h: int


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
    views: list[XdgView]
    keyboards: list[Keyboard]
    popups: list[Popup]
    listeners: list[object]
    cursor: Cursor | None = None
    focused_view: XdgView | None = None
    move_grab: MoveGrab | None = None
    resize_grab: ResizeGrab | None = None
    primary_output: Output | None = None
    terminal_cmd: str = "xterm"
    stop: bool = False


def main(startup_cmd: str | None = None) -> int:
    server = create_server()
    if server is None:
        return 1
    server.terminal_cmd = os.environ.get("TERMINAL", "xterm")

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
            lambda data: xdg_view_on_new(server, data)),
        listen(
            lib.pywl_xdg_shell_new_popup(server.xdg_shell),
            lambda data: on_popup_new(server, data)),
        listen(
            lib.pywl_seat_request_set_selection(server.seat),
            lambda data: on_seat_request_set_selection(server, data)),
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
        views=[],
        keyboards=[],
        popups=[],
        listeners=[],
    )


def run_event_loop(server: Server) -> None:
    def on_stop(_sig, _frm):
        server.stop = True

    signal.signal(signal.SIGINT, on_stop)
    signal.signal(signal.SIGTERM, on_stop)
    while not server.stop:
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
    lib.wlr_backend_destroy(server.backend)
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


def keysym(name: str) -> int:
    """Resolve an xkb key name (e.g. "Return") to a keysym."""
    sym = lib.xkb_keysym_from_name(name.encode(), 0)
    if sym == 0:
        raise ValueError(f"unknown key name: {name!r}")
    return sym


def on_keyboard_key(server: Server, keyboard: Keyboard, data) -> None:
    ev = ffi.cast("struct wlr_keyboard_key_event *", data)
    lib.wlr_seat_set_keyboard(server.seat, keyboard.wlr_keyboard)
    if ev.state == lib.WL_KEYBOARD_KEY_STATE_PRESSED:
        mods = lib.wlr_keyboard_get_modifiers(keyboard.wlr_keyboard)
        sym = lib.pywl_keyboard_keysym(keyboard.wlr_keyboard, ev.keycode)
        if handle_keybinding(server, mods, sym):
            return
    lib.wlr_seat_keyboard_notify_key(
        server.seat,
        ev.time_msec,
        ev.keycode,
        ev.state,
    )


def handle_keybinding(server: Server, mods: int, sym: int) -> bool:
    """Return True if `sym` was consumed as a compositor keybinding."""
    alt = bool(mods & lib.WLR_MODIFIER_ALT)
    shift = bool(mods & lib.WLR_MODIFIER_SHIFT)
    if not alt:
        return False
    if sym == keysym("Return"):
        subprocess.Popen(server.terminal_cmd, shell=True)
        return True
    if sym == keysym("Tab"):
        xdg_view_cycle_focus(server)
        return True
    if shift and sym in (keysym("q"), keysym("Q")):
        if server.focused_view is not None:
            lib.wlr_xdg_toplevel_send_close(server.focused_view.xdg_toplevel)
        return True
    if shift and sym in (keysym("e"), keysym("E")):
        server.stop = True
        return True
    return False


def xdg_view_cycle_focus(server: Server) -> None:
    if len(server.views) < 2:
        return
    # server.views is MRU-sorted (xdg_view_focus appends), so [0] is LRU.
    nxt = server.views[0]
    xdg_view_raise(server, nxt)
    xdg_view_focus(server, nxt, nxt.surface)


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
            ffi.addressof(grab.view.scene_tree.node), x, y)
        grab.view.geometry.x = x
        grab.view.geometry.y = y
        return
    if server.resize_grab is not None:
        grab = server.resize_grab
        w = max(40, int(grab.start_w + (cx - grab.start_cx)))
        h = max(20, int(grab.start_h + (cy - grab.start_cy)))
        lib.wlr_xdg_toplevel_set_size(grab.view.xdg_toplevel, w, h)
        grab.view.geometry.width = w
        grab.view.geometry.height = h
        return

    hit = surface_at(server, cx, cy)
    if hit is None:
        lib.wlr_cursor_set_xcursor(
            server.cursor.wlr_cursor, server.cursor.xcursor_mgr, b"default")
        lib.wlr_seat_pointer_clear_focus(server.seat)
        return
    _view, surface, sx, sy = hit
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
            view, surface, _sx, _sy = hit
            if (mods & lib.WLR_MODIFIER_ALT) and button == lib.BTN_LEFT:
                xdg_view_start_move_grab(server, view)
                return
            if (mods & lib.WLR_MODIFIER_ALT) and button == lib.BTN_RIGHT:
                xdg_view_start_resize_grab(server, view)
                return
            xdg_view_raise(server, view)
            xdg_view_focus(server, view, surface)
        else:
            xdg_view_clear_focus(server)
    elif server.move_grab is not None or server.resize_grab is not None:
        server.move_grab = None
        server.resize_grab = None
        return

    lib.wlr_seat_pointer_notify_button(
        server.seat, time_msec, button, button_state)


def surface_at(server: Server, lx, ly):
    """Return (view, surface, sx, sy), or None if there is no hit."""
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

    view = ffi.from_handle(tree.node.data)
    if view not in server.views:
        return None
    return view, ss.surface, sx[0], sy[0]


def xdg_view_on_new(server: Server, data) -> XdgView:
    xdg_toplevel = ffi.cast("struct wlr_xdg_toplevel *", data)
    base = xdg_toplevel.base
    scene_tree = lib.wlr_scene_xdg_surface_create(
        ffi.addressof(server.scene.tree), base)
    surface = base.surface

    view = XdgView(
        scene_tree=scene_tree,
        surface=surface,
        geometry=Rect(0, 0, 0, 0),
        xdg_toplevel=xdg_toplevel,
    )
    view.handle = ffi.new_handle(view)
    scene_tree.node.data = view.handle
    # Used by popups to find their parent's scene tree.
    base.data = ffi.cast("void *", scene_tree)

    view.listeners = [
        listen(
            lib.pywl_surface_commit(surface),
            lambda data: xdg_view_on_commit(server, view, data)),
        listen(
            lib.pywl_surface_map(surface),
            lambda data: xdg_view_on_map(server, view, data)),
        listen(
            lib.pywl_surface_unmap(surface),
            lambda data: xdg_view_on_unmap(server, view, data)),
        listen(
            lib.pywl_xdg_toplevel_destroy(xdg_toplevel),
            lambda data: xdg_view_on_destroy(server, view, data)),
    ]

    return view


def xdg_view_on_destroy(server: Server, view: XdgView, _data) -> None:
    """Called when an app closes one of its views."""
    for listener in view.listeners:
        listener.remove()
    # In case the view is destroyed without an unmap firing first.
    xdg_view_detach(server, view)


def xdg_view_on_unmap(server: Server, view: XdgView, _data) -> None:
    """Called when a view becomes hidden (but not destroyed)."""
    xdg_view_detach(server, view)


def xdg_view_detach(server: Server, view: XdgView) -> None:
    was_focused = server.focused_view is view
    if view in server.views:
        server.views.remove(view)
    if was_focused:
        if server.views:
            successor = server.views[-1]  # most-recently-focused survivor
            xdg_view_raise(server, successor)
            xdg_view_focus(server, successor, successor.surface)
        else:
            xdg_view_clear_focus(server)
    if server.move_grab is not None and server.move_grab.view is view:
        server.move_grab = None
    if server.resize_grab is not None and server.resize_grab.view is view:
        server.resize_grab = None


def xdg_view_on_commit(server: Server, view: XdgView, _data) -> None:
    """Called whenever an app updates a view."""
    base = view.xdg_toplevel.base
    if not base.initial_commit:
        xdg_view_apply_clip(view)
        return

    output = server.primary_output
    if output is not None:
        out = output.wlr_output
        ow = out.width
        oh = out.height
        w, h = int(ow * 0.8), int(oh * 0.8)
        geometry = Rect((ow - w) // 2, (oh - h) // 2, w, h)
        lib.wlr_xdg_toplevel_set_size(
            view.xdg_toplevel, geometry.width, geometry.height)
        lib.wlr_scene_node_set_position(
            ffi.addressof(view.scene_tree.node),
            geometry.x, geometry.y)
    else:
        geometry = Rect(0, 0, 0, 0)
        lib.wlr_xdg_toplevel_set_size(view.xdg_toplevel, 0, 0)

    view.geometry = geometry
    xdg_view_apply_clip(view)


def xdg_view_apply_clip(view: XdgView) -> None:
    clip = ffi.new("struct wlr_box *")
    clip.x = view.xdg_toplevel.base.geometry.x
    clip.y = view.xdg_toplevel.base.geometry.y
    clip.width = view.geometry.width
    clip.height = view.geometry.height
    lib.wlr_scene_subsurface_tree_set_clip(
        ffi.addressof(view.scene_tree.node), clip)


def xdg_view_on_map(server: Server, view: XdgView, _data) -> None:
    """Called the moment a view first has pixels to show on screen."""
    server.views.append(view)
    xdg_view_focus(server, view, view.surface)


def xdg_view_focus(server: Server, view: XdgView, surface) -> None:
    # Move to end of server.views so it's the MRU; xdg_view_detach and
    # xdg_view_cycle_focus rely on this ordering.
    if server.views and server.views[-1] is not view:
        server.views.remove(view)
        server.views.append(view)
    lib.wlr_seat_keyboard_notify_enter(
        server.seat, surface, ffi.NULL, 0, ffi.NULL)
    for w in server.views:
        lib.wlr_xdg_toplevel_set_activated(w.xdg_toplevel, w is view)
    server.focused_view = view


def xdg_view_clear_focus(server: Server) -> None:
    lib.wlr_seat_keyboard_clear_focus(server.seat)
    for view in server.views:
        lib.wlr_xdg_toplevel_set_activated(view.xdg_toplevel, False)
    server.focused_view = None


def xdg_view_start_move_grab(server: Server, view: XdgView) -> None:
    node = view.scene_tree.node
    gx = server.cursor.wlr_cursor.x - node.x
    gy = server.cursor.wlr_cursor.y - node.y
    server.move_grab = MoveGrab(view, gx, gy)


def xdg_view_start_resize_grab(server: Server, view: XdgView) -> None:
    server.resize_grab = ResizeGrab(
        view,
        server.cursor.wlr_cursor.x,
        server.cursor.wlr_cursor.y,
        view.geometry.width,
        view.geometry.height,
    )


def xdg_view_raise(server: Server, view: XdgView) -> None:
    lib.wlr_scene_node_raise_to_top(ffi.addressof(view.scene_tree.node))


def on_popup_new(server: Server, data) -> None:
    xdg_popup = ffi.cast("struct wlr_xdg_popup *", data)
    parent_xdg = lib.wlr_xdg_surface_try_from_wlr_surface(xdg_popup.parent)
    if parent_xdg == ffi.NULL or parent_xdg.data == ffi.NULL:
        return  # parent isn't an xdg_surface we track; skip
    parent_tree = ffi.cast("struct wlr_scene_tree *", parent_xdg.data)
    popup_tree = lib.wlr_scene_xdg_surface_create(parent_tree, xdg_popup.base)
    xdg_popup.base.data = ffi.cast("void *", popup_tree)

    popup = Popup(xdg_popup)
    server.popups.append(popup)

    surface = xdg_popup.base.surface
    popup.listeners = [
        listen(
            lib.pywl_surface_commit(surface),
            lambda data: on_popup_commit(server, popup, data)),
        listen(
            lib.pywl_xdg_popup_destroy(xdg_popup),
            lambda data: on_popup_destroy(server, popup, data)),
    ]


def on_popup_commit(server: Server, popup: Popup, _data) -> None:
    if popup.xdg_popup.base.initial_commit:
        lib.wlr_xdg_surface_schedule_configure(popup.xdg_popup.base)


def on_popup_destroy(server: Server, popup: Popup, _data) -> None:
    for listener in popup.listeners:
        listener.remove()
    if popup in server.popups:
        server.popups.remove(popup)


def on_seat_request_set_selection(server: Server, data) -> None:
    ev = ffi.cast("struct wlr_seat_request_set_selection_event *", data)
    lib.wlr_seat_set_selection(server.seat, ev.source, ev.serial)


if __name__ == "__main__":
    startup = sys.argv[1] if len(sys.argv) > 1 else None
    sys.exit(main(startup))
