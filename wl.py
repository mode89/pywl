"""pywl: python port of dwl."""

# pylint: disable=too-many-lines

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Callable
from types import SimpleNamespace

import bindings


ffi = None  # pylint: disable=invalid-name
lib = None  # pylint: disable=invalid-name
listen = None  # pylint: disable=invalid-name
config = None  # populated by main() after bindings.build()  # pylint: disable=invalid-name


# --- Constants --------------------------------------------------------------

TAG_COUNT = 9
TAG_ALL = (1 << TAG_COUNT) - 1
# wlroots ignores caps/num/scroll-lock when matching keybindings.
# Populated by main() once bindings.build() has run.
_IGNORE_MODS = 0
_TRACE = True


# --- Enums ------------------------------------------------------------------

class Layer(IntEnum):
    """Top-level scene trees, bottom-up."""
    BACKGROUND = 0
    BOTTOM = 1
    TILE = 2
    FLOAT = 3
    TOP = 4
    FULLSCREEN = 5
    OVERLAY = 6
    BLOCK = 7


class CursorMode(Enum):
    """What the mouse is currently doing. `Pressed` means a button is held
    down on a window, so we keep sending mouse moves to that window even
    if the pointer drifts off it. `Move` and `Resize` are when the user
    is dragging a window around or resizing it."""
    NORMAL = "normal"
    PRESSED = "pressed"
    MOVE = "move"
    RESIZE = "resize"


class ClientType(Enum):
    """What kind of window this is. Regular app windows are `XdgShell`;
    things like panels or wallpapers are `LayerShell`. `X11` is for
    legacy X apps (not implemented yet)."""
    XDG_SHELL = "xdg"
    LAYER_SHELL = "layer"
    X11 = "x11"


# --- Dataclasses ------------------------------------------------------------

# Box = (x, y, w, h), matching wlr_box.

@dataclass
class Monitor:  # pylint: disable=too-many-instance-attributes
    """State for one physical screen."""
    wlr_output: object
    scene_output: object
    name: str
    m: tuple[int, int, int, int] = (0, 0, 0, 0)   # full output box
    w: tuple[int, int, int, int] = (0, 0, 0, 0)   # working area
    # Two slots so view() can swap between current and previous selection.
    tags: list[int] = field(default_factory=lambda: [1, 1])
    seltags: int = 0
    # indexes into LAYOUTS
    layouts: list[int] = field(default_factory=lambda: [0, 1])
    sellt: int = 0
    master_factor: float = 0.55
    num_master: int = 1
    # Layer-shell surfaces on this screen, split by layer
    # (background / bottom / top / overlay).
    layer_surfaces: list[list] = field(
        default_factory=lambda: [[], [], [], []])
    listeners: list = field(default_factory=list)

    @property
    def selected_tags(self) -> int:
        """Bitmask of currently visible tags."""
        return self.tags[self.seltags]

    @property
    def layout_name(self) -> str:
        """Name of the active layout."""
        return config.layouts[self.layouts[self.sellt]].name

    @property
    def layout_symbol(self) -> str:
        """Short symbol for the active layout, for status bars."""
        return config.layouts[self.layouts[self.sellt]].symbol


@dataclass
class Client:  # pylint: disable=too-many-instance-attributes
    """State for one app window we're managing."""
    client_type: ClientType
    xdg_toplevel: object       # wlr_xdg_toplevel * (or None for non-xdg)
    surface: object            # wlr_surface *
    # wlr_scene_tree * — our wrapper tree (parent of borders + xdg tree)
    scene_tree: object
    # wlr_scene_tree * returned by wlr_scene_xdg_surface_create
    scene_surface: object
    # 4 × wlr_scene_rect *
    border_rects: list = field(default_factory=list)
    geometry: tuple[int, int, int, int] = (0, 0, 0, 0)
    # saved on fullscreen-enter
    prev_geometry: tuple[int, int, int, int] = (0, 0, 0, 0)
    tags: int = 0
    floating: bool = False
    fullscreen: bool = False
    urgent: bool = False
    monitor: Monitor | None = None
    border_width: int = 0
    mapped: bool = False
    resize_serial: int = 0
    handle: object = None
    decoration: object = None
    listeners: list = field(default_factory=list)

    @property
    def app_id(self) -> str:
        """Client-supplied app id, or "" if unset."""
        xdg = self.xdg_toplevel
        if xdg is None or xdg.app_id == ffi.NULL:  # pylint: disable=no-member
            return ""
        return ffi.string(xdg.app_id).decode("utf-8", "replace")  # pylint: disable=no-member

    @property
    def title(self) -> str:
        """Client-supplied title, or "" if unset."""
        xdg = self.xdg_toplevel
        if xdg is None or xdg.title == ffi.NULL:  # pylint: disable=no-member
            return ""
        return ffi.string(xdg.title).decode("utf-8", "replace")  # pylint: disable=no-member


@dataclass
class Popup:
    """A small transient window like a menu or tooltip. We keep a record
    of it so we can clean up our event hooks when it goes away."""
    xdg_popup: object
    scene_tree: object
    listeners: list = field(default_factory=list)


@dataclass
class LayerSurface:  # pylint: disable=too-many-instance-attributes
    """A panel, wallpaper or lock screen — windows that aren't regular
    app windows and live above or below them."""
    wlr: object               # wlr_layer_surface_v1 *
    scene_layer: object       # wlr_scene_layer_surface_v1 *
    scene_tree: object        # wlr_scene_tree * (== scene_layer->tree)
    monitor: Monitor | None
    popups: object = None     # wlr_scene_tree * for child xdg-popups
    handle: object = None     # ffi.new_handle(self), kept alive here
    mapped: bool = False
    listeners: list = field(default_factory=list)


@dataclass
class Grab:
    """Where the mouse and the window were when the user started dragging.
    We need this so we can move/resize relative to the start, not the
    current pointer position."""
    client: Client
    mode: CursorMode      # Move or Resize
    cursor_x: float
    cursor_y: float
    geometry: tuple[int, int, int, int]


@dataclass
class Server:  # pylint: disable=too-many-instance-attributes
    """All of the compositor's state in one place. There's exactly one of
    these; event handlers receive it so they can see and modify
    everything."""
    display: object
    loop: object
    backend: object
    renderer: object
    allocator: object
    compositor: object
    output_layout: object
    scene: object
    scene_layout: object
    layers: dict
    root_bg: object
    locked_bg: object
    drag_icon: object
    xdg_shell: object
    seat: object
    cursor: object
    xcursor_mgr: object
    keyboard_group: object
    monitors: list[Monitor] = field(default_factory=list)
    selected_monitor: Monitor | None = None
    clients: list[Client] = field(default_factory=list)   # all mapped clients
    fstack: list[Client] = field(default_factory=list)    # MRU, head = focused
    popups: list[Popup] = field(default_factory=list)
    listeners: list = field(default_factory=list)
    grab: Grab | None = None
    cursor_mode: CursorMode = CursorMode.NORMAL
    stop: bool = False
    # Layer surface currently grabbing keyboard focus exclusively, if any.
    exclusive_focus: object = None
    # While locked, only the lock surface may hold keyboard focus.
    locked: bool = False


@dataclass(frozen=True)
class LayoutConfig:
    """How a layout (e.g. tiled, floating) is described to the user."""
    name: str       # "tile" | "floating" | "monocle"
    symbol: str


@dataclass(frozen=True)
class MonitorRule:  # pylint: disable=too-many-instance-attributes
    """User-configured defaults for a screen, applied when it's plugged in.
    `name=None` means "applies to any screen that didn't match a more
    specific rule"."""
    name: str | None = None
    master_factor: float = 0.55
    num_master: int = 1
    layout_index: int = 0
    rotation: int = 0
    scale: float = 1.0
    x: int = -1
    y: int = -1


@dataclass(frozen=True)
class AppRule:
    """User-configured defaults for a particular app: which workspace it
    starts on, whether it floats, which screen it lands on. Matched by
    looking for `app_id`/`title` as substrings of the window's own."""
    app_id: str | None = None
    title: str | None = None
    tags: int = 0
    floating: bool = False
    monitor: int = -1


@dataclass(frozen=True)
class KeyBinding:
    """A keyboard shortcut: a combination of modifiers (Alt, Ctrl, ...)
    plus a key, bound to one of the named actions in `ACTIONS`."""
    mod: int
    sym: str
    action: str
    arg: object = None


@dataclass(frozen=True)
class ButtonBinding:
    """A mouse shortcut: modifiers plus a mouse button, bound to one of
    the named actions in `ACTIONS`."""
    mod: int
    button: int
    action: str
    arg: object = None


# Held so signal handlers can reach the server.
_server_ref: list[Server] = []


# --- Entry point ------------------------------------------------------------

def main(startup_cmd: str | None = None) -> int:
    """Program entry point. Builds the C bindings, sets everything up,
    runs until told to quit, then tears it all down. Returns a process
    exit code."""
    global ffi, lib, listen, config, _IGNORE_MODS  # pylint: disable=global-statement
    ffi, lib, listen = bindings.build()
    for _name in (
            "WLR_MODIFIER_CAPS", "WLR_MODIFIER_MOD2", "WLR_MODIFIER_MOD3"):
        _IGNORE_MODS |= getattr(lib, _name, 0)
    config = _default_config()

    _trace("main: enter")
    install_signal_handlers()
    _trace("main: signal handlers installed")
    server = setup()
    _trace("main: setup() returned")
    _server_ref.append(server)

    socket = lib.wl_display_add_socket_auto(server.display)
    if socket == ffi.NULL:
        cleanup(server)
        return 1
    socket_str = ffi.string(socket).decode()

    if not lib.wlr_backend_start(server.backend):
        cleanup(server)
        return 1

    os.environ["WAYLAND_DISPLAY"] = socket_str
    sys.stderr.write(f"Running on WAYLAND_DISPLAY={socket_str}\n")

    cmd = startup_cmd or config.startup_cmd
    if cmd:
        spawn(cmd)

    run(server)
    cleanup(server)

    _trace(f"remaining listeners: {len(listen.listeners)}")

    return 0


def _default_config() -> SimpleNamespace:
    """All compositor defaults. Anything overridden by user config wins."""
    MOD = lib.WLR_MODIFIER_ALT  # pylint: disable=invalid-name
    SUPER = lib.WLR_MODIFIER_LOGO  # pylint: disable=invalid-name
    SHIFT = lib.WLR_MODIFIER_SHIFT  # pylint: disable=invalid-name
    CTRL = lib.WLR_MODIFIER_CTRL  # pylint: disable=invalid-name
    term_cmd = "alacritty"

    def tag_keys(sym, i):
        bit = 1 << i
        return [
            KeyBinding(MOD, sym, "view", bit),
            KeyBinding(MOD | CTRL, sym, "toggle_view", bit),
            KeyBinding(MOD | SHIFT, sym, "tag", bit),
            KeyBinding(MOD | CTRL | SHIFT, sym, "toggle_tag", bit),
        ]

    keys = [
        KeyBinding(MOD,         "Return", "spawn", term_cmd),
        KeyBinding(MOD,         "j",      "focus_stack", +1),
        KeyBinding(MOD,         "k",      "focus_stack", -1),
        KeyBinding(MOD,         "i",      "inc_num_master", +1),
        KeyBinding(MOD,         "d",      "inc_num_master", -1),
        KeyBinding(MOD,         "h",      "set_master_factor", -0.05),
        KeyBinding(MOD,         "l",      "set_master_factor", +0.05),
        KeyBinding(MOD,         "z",      "zoom", None),
        KeyBinding(MOD,         "Tab",    "view", 0),
        KeyBinding(MOD | SHIFT, "c",      "kill_client", None),
        KeyBinding(MOD | SHIFT, "space",  "toggle_floating", None),
        KeyBinding(MOD,         "f",      "toggle_fullscreen", None),
        KeyBinding(MOD,         "0",      "view", TAG_ALL),
        KeyBinding(MOD | SHIFT, "0",      "tag", TAG_ALL),
        KeyBinding(MOD,         "t",      "set_layout", 0),
        KeyBinding(MOD,         "s",      "set_layout", 1),
        KeyBinding(MOD,         "m",      "set_layout", 2),
        KeyBinding(MOD,         "space",  "set_layout", None),
        KeyBinding(MOD,         "comma",  "focus_monitor", -1),
        KeyBinding(MOD,         "period", "focus_monitor", +1),
        KeyBinding(MOD | SHIFT, "comma",  "tag_monitor", -1),
        KeyBinding(MOD | SHIFT, "period", "tag_monitor", +1),
        KeyBinding(MOD | SHIFT, "q",      "quit", None),
    ]
    for i in range(TAG_COUNT):
        keys.extend(tag_keys(str(i + 1), i))

    return SimpleNamespace(
        MOD=MOD, SUPER=SUPER, SHIFT=SHIFT, CTRL=CTRL,
        log_level=3,
        xkb_rules={"rules": None, "model": None, "layout": None,
                   "variant": None, "options": None},
        repeat_rate=25,
        repeat_delay=600,
        sloppy_focus=False,
        root_color=(0x22 / 255, 0x22 / 255, 0x22 / 255, 1.0),
        locked_color=(0x1a / 255, 0x1a / 255, 0x1a / 255, 1.0),
        fullscreen_color=(0.0, 0.0, 0.0, 1.0),
        border_width=3,
        border_color=(0x44 / 255, 0x44 / 255, 0x44 / 255, 1.0),
        focus_color=(0x00 / 255, 0x55 / 255, 0x77 / 255, 1.0),
        urgent_color=(1.0, 0.0, 0.0, 1.0),
        cursor_theme=None,
        cursor_size=24,
        term_cmd=term_cmd,
        menu_cmd=None,
        startup_cmd=None,
        layouts=[
            LayoutConfig("tile", "[]="),
            LayoutConfig("floating", "><>"),
            LayoutConfig("monocle", "[M]"),
        ],
        master_factor=0.55,
        num_master=1,
        monitor_rules=[MonitorRule(name=None)],
        rules=[],
        keys=keys,
        buttons=[
            ButtonBinding(MOD, lib.BTN_LEFT,   "move_resize", "move"),
            ButtonBinding(MOD, lib.BTN_RIGHT,  "move_resize", "resize"),
            ButtonBinding(MOD, lib.BTN_MIDDLE, "toggle_floating", None),
        ],
    )


def install_signal_handlers() -> None:
    """Set up Unix signal handlers: Ctrl-C/kill ask us to exit cleanly,
    finished child processes get reaped so they don't pile up as
    zombies, and we ignore broken-pipe errors so a crashed app can't
    take us down."""
    def on_quit(*_):
        if _server_ref:
            _server_ref[0].stop = True

    def on_chld(*_):
        try:
            while True:
                pid, _status = os.waitpid(-1, os.WNOHANG)
                if pid == 0:
                    return
        except ChildProcessError:
            return

    signal.signal(signal.SIGINT, on_quit)
    signal.signal(signal.SIGTERM, on_quit)
    signal.signal(signal.SIGCHLD, on_chld)
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)


# --- setup ------------------------------------------------------------------

def setup() -> Server:  # pylint: disable=too-many-locals,too-many-statements
    """Create everything the compositor needs (renderer, input, scene
    graph, etc.) and hook up event handlers. The backend isn't started
    here — `main` does that once the Wayland socket is ready."""
    _trace("setup: log_init")
    lib.wlr_log_init(config.log_level, ffi.NULL)

    _trace("setup: display + loop")
    display = lib.wl_display_create()
    loop = lib.wl_display_get_event_loop(display)

    _trace("setup: backend")
    backend = lib.wlr_backend_autocreate(loop, ffi.NULL)
    if backend == ffi.NULL:
        die("couldn't create backend")

    _trace("setup: renderer")
    renderer = lib.wlr_renderer_autocreate(backend)
    if renderer == ffi.NULL:
        die("couldn't create renderer")
    lib.wlr_renderer_init_wl_shm(renderer, display)

    _trace("setup: allocator")
    allocator = lib.wlr_allocator_autocreate(backend, renderer)
    if allocator == ffi.NULL:
        die("couldn't create allocator")

    _trace("setup: compositor + subcompositor + ddm")
    compositor = lib.wlr_compositor_create(display, 6, renderer)
    lib.wlr_subcompositor_create(display)
    lib.wlr_data_device_manager_create(display)

    scene = lib.wlr_scene_create()
    scene_tree_ptr = ffi.addressof(scene.tree)
    root_bg = lib.wlr_scene_rect_create(
        scene_tree_ptr, 0, 0, _float_color(config.root_color))
    layers = {
        layer: lib.wlr_scene_tree_create(scene_tree_ptr) for layer in Layer}
    drag_icon = lib.wlr_scene_tree_create(scene_tree_ptr)
    lib.wlr_scene_node_place_below(
        ffi.addressof(drag_icon.node),
        ffi.addressof(layers[Layer.BLOCK].node))
    locked_bg = lib.wlr_scene_rect_create(
        layers[Layer.BLOCK], 0, 0, _float_color(config.locked_color))
    lib.wlr_scene_node_set_enabled(lib.pywl_scene_rect_node(locked_bg), False)

    output_layout = lib.wlr_output_layout_create(display)
    scene_layout = lib.wlr_scene_attach_output_layout(scene, output_layout)

    xdg_shell = lib.wlr_xdg_shell_create(display, 6)
    _trace(f"setup: xdg_shell={xdg_shell}")
    # Tell apps (GTK/Qt) not to draw their own titlebar — we draw the
    # window decorations.
    xdg_decoration_mgr = lib.wlr_xdg_decoration_manager_v1_create(display)
    _trace(f"setup: xdg_decoration_mgr={xdg_decoration_mgr}")
    server_decoration_mgr = lib.wlr_server_decoration_manager_create(display)
    lib.wlr_server_decoration_manager_set_default_mode(
        server_decoration_mgr,
        lib.WLR_SERVER_DECORATION_MANAGER_MODE_SERVER)
    _trace(f"setup: server_decoration_mgr={server_decoration_mgr}")
    layer_shell = lib.wlr_layer_shell_v1_create(display, 3)
    _trace(f"setup: layer_shell={layer_shell}")
    seat = lib.wlr_seat_create(display, b"seat0")
    _trace(f"setup: seat={seat}")

    cursor = lib.wlr_cursor_create()
    lib.wlr_cursor_attach_output_layout(cursor, output_layout)
    theme = config.cursor_theme.encode() if config.cursor_theme else ffi.NULL
    xcursor_mgr = lib.wlr_xcursor_manager_create(theme, config.cursor_size)

    server = Server(
        display=display, loop=loop, backend=backend,
        renderer=renderer, allocator=allocator, compositor=compositor,
        output_layout=output_layout, scene=scene, scene_layout=scene_layout,
        layers=layers, root_bg=root_bg, locked_bg=locked_bg,
        drag_icon=drag_icon, xdg_shell=xdg_shell, seat=seat,
        cursor=cursor, xcursor_mgr=xcursor_mgr, keyboard_group=ffi.NULL,
    )
    server.keyboard_group = create_keyboard_group(server)

    server.listeners.extend([
        listen(lib.pywl_layer_shell_new_surface(layer_shell),
               lambda d: on_new_layer_surface(server, d)),
        listen(lib.pywl_xdg_decoration_manager_new(xdg_decoration_mgr),
               on_new_xdg_decoration),
        listen(lib.pywl_renderer_lost_signal(renderer),
               lambda d: on_gpu_reset(server, d)),
        listen(lib.pywl_backend_new_output(backend),
               lambda d: on_new_output(server, d)),
        listen(lib.pywl_backend_new_input(backend),
               lambda d: on_new_input(server, d)),
        listen(lib.pywl_output_layout_change(output_layout),
               lambda d: update_monitors(server)),
        listen(lib.pywl_xdg_shell_new_toplevel(xdg_shell),
               lambda d: on_new_xdg_toplevel(server, d)),
        listen(lib.pywl_xdg_shell_new_popup(xdg_shell),
               lambda d: on_new_xdg_popup(server, d)),
        listen(lib.pywl_seat_request_set_cursor(seat),
               lambda d: on_request_set_cursor(server, d)),
        listen(lib.pywl_seat_request_set_selection(seat),
               lambda d: on_request_set_selection(server, d)),
        listen(lib.pywl_seat_request_set_primary_selection(seat),
               lambda d: on_request_set_primary_selection(server, d)),
        listen(lib.pywl_cursor_motion(cursor),
               lambda d: on_cursor_motion(server, d)),
        listen(lib.pywl_cursor_motion_absolute(cursor),
               lambda d: on_cursor_motion_absolute(server, d)),
        listen(lib.pywl_cursor_button(cursor),
               lambda d: on_cursor_button(server, d)),
        listen(lib.pywl_cursor_axis(cursor),
               lambda d: on_cursor_axis(server, d)),
        listen(lib.pywl_cursor_frame(cursor),
               lambda d: on_cursor_frame(server, d)),
    ])

    return server


def run(server: Server) -> None:
    """Main loop. Each pass sends queued messages out to apps, then waits
    up to 100 ms for new events. The short timeout means a Ctrl-C is
    noticed quickly even if nothing is happening."""
    while not server.stop:
        lib.wl_display_flush_clients(server.display)
        lib.wl_event_loop_dispatch(server.loop, 100)


def cleanup(server: Server) -> None:
    """Shut everything down in the reverse of the order we built it.
    Event handlers have to be removed before the things they listen to
    are freed, otherwise an event could fire on already-freed memory."""
    lib.wl_display_destroy_clients(server.display)
    for handle in server.listeners:
        handle.remove()
    server.listeners.clear()
    if server.keyboard_group != ffi.NULL:
        lib.wlr_keyboard_group_destroy(server.keyboard_group)
    lib.wlr_xcursor_manager_destroy(server.xcursor_mgr)
    lib.wlr_cursor_destroy(server.cursor)
    lib.wlr_scene_node_destroy(ffi.addressof(server.scene.tree.node))
    lib.wlr_allocator_destroy(server.allocator)
    lib.wlr_renderer_destroy(server.renderer)
    lib.wlr_backend_destroy(server.backend)
    lib.wl_display_destroy(server.display)


# --- Keyboard group ---------------------------------------------------------

def create_keyboard_group(server: Server) -> object:
    """Make all physical keyboards behave as one. This way we don't have to
    track which key is pressed on which keyboard — wlroots merges them
    for us."""
    group = lib.wlr_keyboard_group_create()
    kb = lib.pywl_keyboard_group_keyboard(group)

    context = lib.xkb_context_new(0)
    keymap = _new_keymap(context)
    if keymap == ffi.NULL:
        die("failed to compile xkb keymap")
    lib.wlr_keyboard_set_keymap(kb, keymap)
    lib.xkb_keymap_unref(keymap)
    lib.xkb_context_unref(context)
    lib.wlr_keyboard_set_repeat_info(
        kb, config.repeat_rate, config.repeat_delay)

    server.listeners.append(
        listen(lib.pywl_keyboard_key_signal(kb),
               lambda d: on_keyboard_key(server, d)))
    server.listeners.append(
        listen(lib.pywl_keyboard_modifiers_signal(kb),
               lambda d: on_keyboard_modifiers(server, d)))
    lib.wlr_seat_set_keyboard(server.seat, kb)
    return group


def _new_keymap(context):
    """Build the keyboard layout from the user's settings (layout, variant,
    options...). If nothing is configured, use the system defaults."""
    rules = config.xkb_rules
    if all(v is None for v in rules.values()):
        return lib.xkb_keymap_new_from_names(context, ffi.NULL, 0)
    encoded = {k: (v.encode() if v else None) for k, v in rules.items()}
    bufs = {k: (ffi.new("char[]", b) if b else ffi.NULL)
            for k, b in encoded.items()}
    names = ffi.new("struct xkb_rule_names *", {
        "rules": bufs["rules"], "model": bufs["model"],
        "layout": bufs["layout"], "variant": bufs["variant"],
        "options": bufs["options"],
    })
    return lib.xkb_keymap_new_from_names(context, names, 0)


# --- Output / Monitor -------------------------------------------------------

def on_new_output(server: Server, data) -> None:
    """A new screen was plugged in (or detected at startup). Pick a
    resolution/refresh rate, place it in our virtual screen layout,
    register it for drawing, and tile any windows that should appear on
    it."""
    wlr_output = ffi.cast("struct wlr_output *", data)
    lib.wlr_output_init_render(wlr_output, server.allocator, server.renderer)

    name = ffi.string(wlr_output.name).decode() if wlr_output.name != ffi.NULL \
        else "unknown"
    rule = _monitor_rule_for(name)

    output_state = lib.pywl_output_state_new()
    lib.wlr_output_state_set_enabled(output_state, True)
    mode = lib.wlr_output_preferred_mode(wlr_output)
    if mode != ffi.NULL:
        lib.wlr_output_state_set_mode(output_state, mode)
    lib.wlr_output_commit_state(wlr_output, output_state)
    lib.pywl_output_state_free(output_state)

    scene_output = lib.wlr_scene_output_create(server.scene, wlr_output)
    if rule.x >= 0 and rule.y >= 0:
        layout_output = lib.wlr_output_layout_add_auto(
            server.output_layout, wlr_output)
    else:
        layout_output = lib.wlr_output_layout_add_auto(
            server.output_layout, wlr_output)
    lib.wlr_scene_output_layout_add_output(
        server.scene_layout, layout_output, scene_output)

    monitor = Monitor(
        wlr_output=wlr_output, scene_output=scene_output, name=name,
        master_factor=rule.master_factor, num_master=rule.num_master,
    )
    monitor.layouts[0] = rule.layout_index
    server.monitors.append(monitor)
    if server.selected_monitor is None:
        server.selected_monitor = monitor

    _refresh_monitor_box(server, monitor)

    monitor.listeners = [
        listen(lib.pywl_output_frame(wlr_output),
               lambda d: render_monitor(server, monitor)),
        listen(lib.pywl_output_request_state(wlr_output),
               lambda d: _on_output_request_state(monitor, d)),
        listen(lib.pywl_output_destroy_signal(wlr_output),
               lambda d: cleanup_monitor(server, monitor)),
    ]
    arrange_layers(server, monitor)
    arrange(server, monitor)
    print_status(server)


def render_monitor(server: Server, monitor: Monitor) -> None:
    """Render one frame for `monitor`. Invoked from the output's `frame`
    signal, fired by the backend at the output's refresh rate."""
    # Layout transitions should appear atomically: until every tile we
    # just resized has caught up, painting would flash a half-resized
    # frame. Stalling this output is the lesser evil.
    for c in server.clients:
        if (c.resize_serial and not c.floating
                and _visible(c, monitor)):
            break
    else:
        lib.wlr_scene_output_commit(monitor.scene_output, ffi.NULL)

    # Skipping frame_done too would freeze clients' own paint loops,
    # turning our one-frame stall into an indefinite stall.
    timestamp = ffi.new("struct timespec *")
    ns = time.clock_gettime_ns(time.CLOCK_MONOTONIC)
    timestamp.tv_sec = ns // 1_000_000_000
    timestamp.tv_nsec = ns % 1_000_000_000
    lib.wlr_scene_output_send_frame_done(monitor.scene_output, timestamp)


def _monitor_rule_for(name: str) -> config.MonitorRule:
    """Find the user's rule for the screen with this name. The first match
    wins; a rule with `name=None` is the catch-all default."""
    for rule in config.monitor_rules:
        if rule.name is None or rule.name == name:
            return rule
    return config.MonitorRule()


def _refresh_monitor_box(server: Server, monitor: Monitor) -> None:
    """Re-read the screen's position and size from the layout. The full
    box `m` is the whole screen; the working area `w` starts equal but
    will be shrunk later to make room for panels."""
    box = ffi.new("struct wlr_box *")
    lib.wlr_output_layout_get_box(server.output_layout, monitor.wlr_output, box)
    monitor.m = (box.x, box.y, box.width, box.height)
    monitor.w = monitor.m   # arrange_layers subtracts exclusive zones


def _on_output_request_state(monitor: Monitor, data) -> None:
    """The backend is asking us to change the screen's state — for
    example, the user resized the QEMU window when we're running inside
    a VM, or rotated their laptop screen. Just accept it."""
    ev = ffi.cast("struct wlr_output_event_request_state *", data)
    lib.wlr_output_commit_state(monitor.wlr_output, ev.state)


def cleanup_monitor(server: Server, monitor: Monitor) -> None:
    """Migrate clients off this monitor, then tear it down."""
    for handle in monitor.listeners:
        handle.remove()
    monitor.listeners.clear()
    if monitor in server.monitors:
        server.monitors.remove(monitor)

    survivor = server.monitors[0] if server.monitors else None
    for client in server.clients:
        if client.monitor is monitor:
            client.monitor = survivor
            if client.fullscreen and survivor is not None:
                set_fullscreen(server, client, True)

    if server.selected_monitor is monitor:
        server.selected_monitor = survivor

    lib.wlr_output_layout_remove(server.output_layout, monitor.wlr_output)
    if survivor is not None:
        arrange(server, survivor)
    focus_client(server, top_client(server, server.selected_monitor), lift=True)
    print_status(server)


def update_monitors(server: Server) -> None:
    """Layout-change hook: refresh every monitor's box and arrange."""
    for monitor in server.monitors:
        _refresh_monitor_box(server, monitor)
        arrange_layers(server, monitor)
        arrange(server, monitor)
    print_status(server)


def monitor_at(server: Server, x: float, y: float) -> Monitor | None:
    """Which screen contains the point `(x, y)`, or None if none does."""
    for monitor in server.monitors:
        mx, my, mw, mh = monitor.m
        if mx <= x < mx + mw and my <= y < my + mh:
            return monitor
    return None


def monitor_in_direction(
    server: Server, base: Monitor, direction: int
) -> Monitor | None:
    """+1 = next monitor by layout order, -1 = previous; wraps."""
    if not server.monitors:
        return None
    idx = server.monitors.index(base)
    return server.monitors[(idx + direction) % len(server.monitors)]


# --- Layouts ----------------------------------------------------------------

LayoutFn = Callable[["Server", Monitor], None]


def layout_tile(server: Server, monitor: Monitor) -> None:
    """Classic tiling: a few main windows share a left column whose width
    is set by `master_factor`, and any remaining windows stack on the
    right."""
    tiled = [c for c in server.clients
             if c.monitor is monitor and _visible(c, monitor)
             and not c.floating and not c.fullscreen]
    if not tiled:
        return
    wx, wy, ww, wh = monitor.w
    n = len(tiled)
    n_master = min(monitor.num_master, n)
    if n > n_master:
        master_w = int(ww * monitor.master_factor) if n_master > 0 else 0
    else:
        master_w = ww

    my = ty = 0
    for i, client in enumerate(tiled):
        if i < n_master:
            slot_h = (wh - my) // (n_master - i)
            resize(server, client, (wx, wy + my, master_w, slot_h))
            my += slot_h
        else:
            slot_h = (wh - ty) // (n - i)
            resize(server, client,
                   (wx + master_w, wy + ty, ww - master_w, slot_h))
            ty += slot_h


def layout_floating(_server: Server, _monitor: Monitor) -> None:
    """Don't tile anything — windows stay wherever they were placed."""


def layout_monocle(server: Server, monitor: Monitor) -> None:
    """Show every visible window full-size, stacked on top of each other
    (only the top one is actually seen)."""
    for client in server.clients:
        if (client.monitor is monitor and _visible(client, monitor)
                and not client.floating and not client.fullscreen):
            resize(server, client, monitor.w)


LAYOUTS: dict[str, LayoutFn] = {
    "tile": layout_tile,
    "floating": layout_floating,
    "monocle": layout_monocle,
}


def arrange(server: Server, monitor: Monitor | None) -> None:
    """Lay out all windows on this screen. Fullscreen ones cover the whole
    screen, the rest go through the chosen layout (tile/float/monocle),
    and finally we hide windows whose workspace isn't currently
    visible."""
    if monitor is None:
        return
    _trace(f"arrange: monitor={monitor.name} layout={monitor.layout_name} "
           f"tags={monitor.selected_tags:#x}")
    # Fullscreen windows first — they always cover the whole screen.
    for client in server.clients:
        if (client.monitor is monitor and client.fullscreen
                and _visible(client, monitor)):
            resize(server, client, monitor.m)

    LAYOUTS[monitor.layout_name](server, monitor)

    # Show or hide each window based on whether its workspace is selected.
    for client in server.clients:
        if client.monitor is monitor:
            lib.wlr_scene_node_set_enabled(
                ffi.addressof(client.scene_tree.node),
                _visible(client, monitor))


def _visible(client: Client, monitor: Monitor) -> bool:
    """True if this window belongs to this screen and any of its workspace
    tags is currently selected."""
    return (client.monitor is monitor
            and bool(client.tags & monitor.selected_tags))


# --- Resize pipeline --------------------------------------------------------

def resize(  # pylint: disable=unused-argument
    _server: Server, client: Client,
    geometry: tuple[int, int, int, int], *, interactive: bool = False,
) -> None:
    """Position/size a client. Also sizes its 4 border rects and clips
    the surface to the inner area. dwl-style single entry point."""
    x, y, width, height = geometry
    bw = client.border_width
    _trace(
        f"resize: client={client.app_id!r} "
        f"geom=({x},{y},{width},{height}) bw={bw}")
    client.geometry = geometry

    lib.wlr_scene_node_set_position(
        ffi.addressof(client.scene_tree.node), x, y)
    lib.wlr_scene_node_set_position(
        ffi.addressof(client.scene_surface.node), bw, bw)

    inner_w = max(0, width - 2 * bw)
    inner_h = max(0, height - 2 * bw)

    # Borders: top, bottom, left, right.
    top, bottom, left, right = client.border_rects
    lib.wlr_scene_rect_set_size(top, width, bw)
    lib.wlr_scene_node_set_position(lib.pywl_scene_rect_node(top), 0, 0)
    lib.wlr_scene_rect_set_size(bottom, width, bw)
    lib.wlr_scene_node_set_position(
        lib.pywl_scene_rect_node(bottom), 0, height - bw)
    lib.wlr_scene_rect_set_size(left, bw, inner_h)
    lib.wlr_scene_node_set_position(lib.pywl_scene_rect_node(left), 0, bw)
    lib.wlr_scene_rect_set_size(right, bw, inner_h)
    lib.wlr_scene_node_set_position(
        lib.pywl_scene_rect_node(right), width - bw, bw)

    _update_clip(client)

    if client.xdg_toplevel is not None:
        # Once the app confirmed the new size, don't ask again — otherwise
        # the per-frame wait in render_monitor would never finish.
        cur = client.xdg_toplevel.current
        if cur.width == inner_w and cur.height == inner_h:
            client.resize_serial = 0
        else:
            client.resize_serial = lib.wlr_xdg_toplevel_set_size(
                client.xdg_toplevel, inner_w, inner_h)
        _trace(
            f"resize: set_size({inner_w},{inner_h}) "
            f"serial={client.resize_serial}")


def _update_clip(client: Client) -> None:
    """Clip the surface tree to the inner box, offset by xdg geometry
    so the CSD shadow is cropped out."""
    if client.scene_surface is None:
        return
    _x, _y, width, height = client.geometry
    bw = client.border_width
    inner_w = max(0, width - 2 * bw)
    inner_h = max(0, height - 2 * bw)
    gx, gy = 0, 0
    if client.xdg_toplevel is not None:
        g = client.xdg_toplevel.base.geometry
        gx, gy = g.x, g.y
    clip = ffi.new("struct wlr_box *", {
        "x": gx, "y": gy, "width": inner_w, "height": inner_h,
    })
    lib.wlr_scene_subsurface_tree_set_clip(
        ffi.addressof(client.scene_surface.node), clip)


def _set_border_color(client: Client, rgba) -> None:
    """Repaint a window's four border edges (e.g. when focus changes)."""
    color = _float_color(rgba)
    for rect in client.border_rects:
        lib.wlr_scene_rect_set_color(rect, color)


# --- xdg-shell --------------------------------------------------------------

def on_new_xdg_toplevel(server: Server, data) -> None:
    """An app just asked for a new window. Set up our drawing nodes for
    it (border rectangles sit *under* the window's own drawing, so apps
    that draw their own decorations can't paint over them), wrap it in
    a `Client`, and subscribe to its events. The window doesn't actually
    appear until it gets mapped (see `on_xdg_toplevel_map`)."""
    _trace("on_new_xdg_toplevel: enter")
    xdg_toplevel = ffi.cast("struct wlr_xdg_toplevel *", data)
    base = xdg_toplevel.base

    scene_tree = lib.wlr_scene_tree_create(server.layers[Layer.TILE])
    scene_surface = lib.wlr_scene_xdg_surface_create(scene_tree, base)
    border_rects = [
        lib.wlr_scene_rect_create(
            scene_tree, 0, 0, _float_color(config.border_color))
        for _ in range(4)
    ]
    # Put borders below the app's own drawing so apps that draw their
    # own decorations can't paint over them.
    for rect in border_rects:
        lib.wlr_scene_node_place_below(
            lib.pywl_scene_rect_node(rect),
            ffi.addressof(scene_surface.node))

    client = Client(
        client_type=ClientType.XDG_SHELL,
        xdg_toplevel=xdg_toplevel,
        surface=base.surface,
        scene_tree=scene_tree,
        scene_surface=scene_surface,
        border_rects=border_rects,
        border_width=config.border_width,
    )
    client.handle = ffi.new_handle(client)
    scene_tree.node.data = client.handle
    base.data = ffi.cast("void *", scene_surface)
    # Stash the popup-host drawing tree here so on_new_xdg_popup can find
    # it the same way for both regular windows and panels.
    base.surface.data = ffi.cast("void *", scene_surface)

    client.listeners = [
        listen(lib.pywl_surface_commit(base.surface),
               lambda d: on_xdg_toplevel_commit(server, client, d)),
        listen(lib.pywl_surface_map(base.surface),
               lambda d: on_xdg_toplevel_map(server, client, d)),
        listen(lib.pywl_surface_unmap(base.surface),
               lambda d: on_xdg_toplevel_unmap(server, client, d)),
        listen(lib.pywl_xdg_toplevel_destroy(xdg_toplevel),
               lambda d: on_xdg_toplevel_destroy(server, client, d)),
        listen(lib.pywl_xdg_toplevel_set_title(xdg_toplevel),
               lambda d: on_xdg_toplevel_set_title(server, client, d)),
        listen(lib.pywl_xdg_toplevel_set_app_id(xdg_toplevel),
               lambda d: print_status(server)),
        listen(lib.pywl_xdg_toplevel_request_maximize(xdg_toplevel),
               lambda d: on_xdg_toplevel_request_maximize(server, client, d)),
        listen(lib.pywl_xdg_toplevel_request_fullscreen(xdg_toplevel),
               lambda d: on_xdg_toplevel_request_fullscreen(server, client, d)),
    ]


def on_xdg_toplevel_commit(server: Server, client: Client, _data) -> None:
    """The app handed us new window content. The very first time, do
    one-time setup (some calls only become legal once the window has
    initialised); on later commits, re-apply our size so our clipping
    follows any change the app made to its own content size."""
    base = client.xdg_toplevel.base
    if not base.initial_commit:
        # Re-apply our size on every update: re-tightens the clipping if
        # the app changed its content size, and re-pins apps that try to
        # shrink themselves (asking for the same size is a no-op).
        if client.mapped and client.scene_surface is not None:
            resize(server, client, client.geometry)
        return
    _trace("commit: initial_commit")
    # These setup calls only become legal once the window is initialised,
    # which happens during this first-commit handler.
    lib.wlr_xdg_toplevel_set_wm_capabilities(
        client.xdg_toplevel, lib.WLR_XDG_TOPLEVEL_WM_CAPABILITIES_FULLSCREEN)
    # Without a "tiled" hint, GTK shrinks the window to fit its widgets
    # and leaves gaps when some widgets (e.g. a menu bar) are hidden.
    lib.wlr_xdg_toplevel_set_tiled(
        client.xdg_toplevel,
        lib.WLR_EDGE_TOP | lib.WLR_EDGE_BOTTOM
        | lib.WLR_EDGE_LEFT | lib.WLR_EDGE_RIGHT)
    monitor = server.selected_monitor
    if monitor is not None:
        lib.wlr_xdg_toplevel_set_bounds(
            client.xdg_toplevel, monitor.w[2], monitor.w[3])
    if client.decoration is not None:
        request_decoration_mode(client)
    # (0, 0) lets the app pick a default size; arrange() resizes it once
    # the window actually appears.
    lib.wlr_xdg_toplevel_set_size(client.xdg_toplevel, 0, 0)


def on_xdg_toplevel_map(server: Server, client: Client, _data) -> None:
    """The window is ready to be shown. Decide which screen and workspace
    it belongs on (using the user's app rules; child windows like
    dialogs default to floating), add it to our window list, lay
    everything out, and give it focus."""
    _trace(f"map: app_id={client.app_id!r} title={client.title!r}")
    target_monitor = server.selected_monitor
    target_tags = target_monitor.selected_tags if target_monitor else 1

    # Dialogs (windows with a parent) default to floating.
    if client.xdg_toplevel.parent != ffi.NULL:
        client.floating = True

    rule = _rule_for(client)
    if rule is not None:
        if rule.tags:
            target_tags = rule.tags & TAG_ALL
        if rule.floating:
            client.floating = True
        if 0 <= rule.monitor < len(server.monitors):
            target_monitor = server.monitors[rule.monitor]

    client.monitor = target_monitor
    client.tags = target_tags or (target_monitor.selected_tags
                                  if target_monitor else 1)
    client.mapped = True
    server.clients.insert(0, client)
    # focus_client owns focus-order bookkeeping; inserting here would
    # skip the keyboard-focus handoff it does.

    if client.floating and target_monitor is not None:
        client.geometry = _initial_float_geometry(target_monitor, client)
        lib.wlr_scene_node_reparent(
            ffi.addressof(client.scene_tree.node),
            server.layers[Layer.FLOAT])

    if target_monitor is not None:
        arrange(server, target_monitor)

    if client.floating and target_monitor is not None:
        # arrange() skips floating windows, so size this one ourselves.
        resize(server, client, client.geometry)

    focus_client(server, client, lift=True)
    print_status(server)


def on_xdg_toplevel_unmap(server: Server, client: Client, _data) -> None:
    """The window stopped showing content (e.g. minimised or about to
    close). Remove it from our lists, throw away its drawing nodes,
    and focus whatever's now on top."""
    if not client.mapped:
        return
    client.mapped = False
    monitor = client.monitor
    if client in server.clients:
        server.clients.remove(client)
    if client in server.fstack:
        server.fstack.remove(client)
    if server.grab is not None and server.grab.client is client:
        end_grab(server)
    lib.wlr_scene_node_destroy(ffi.addressof(client.scene_tree.node))
    client.scene_tree = None
    client.scene_surface = None
    client.border_rects = []
    if monitor is not None:
        arrange(server, monitor)
    focus_client(server, top_client(server, server.selected_monitor), lift=True)
    print_status(server)


def on_xdg_toplevel_destroy(_server: Server, client: Client, _data) -> None:
    """The window object is being freed. Drop all our event subscriptions
    so we don't try to react to events on something that no longer
    exists."""
    for handle in client.listeners:
        handle.remove()
    client.listeners.clear()


def on_xdg_toplevel_set_title(server: Server, client: Client, _data) -> None:
    """If the focused window's title changed, refresh the status output
    so a status bar can pick up the new text."""
    if client.mapped and _is_focused(server, client):
        print_status(server)


def on_xdg_toplevel_request_maximize(
    _server: Server, _client: Client, _data,
) -> None:
    """App asked to be maximised. We don't expose maximise as a feature
    (the tiling layout takes care of sizing), so we just ignore it."""
    # Apps are only allowed to ask for capabilities we advertised, and
    # we only advertised fullscreen, so this never needs handling.


def on_xdg_toplevel_request_fullscreen(
    server: Server, client: Client, _data
) -> None:
    """App asked to enter or leave fullscreen (e.g. a video player). Honour
    it."""
    if not client.mapped:
        return
    set_fullscreen(
        server, client, bool(client.xdg_toplevel.requested.fullscreen))


def request_decoration_mode(client: Client) -> None:
    """Tell the app to let us draw the window borders rather than drawing
    its own. Some toolkits (GTK/Qt) otherwise paint their own titlebar,
    which would clash with our borders."""
    # Asking for a decoration mode triggers a window reconfigure, which
    # is only allowed after the window has finished initialising.
    if client.decoration is None or not client.xdg_toplevel.base.initialized:
        return
    lib.wlr_xdg_toplevel_decoration_v1_set_mode(
        client.decoration,
        lib.WLR_XDG_TOPLEVEL_DECORATION_V1_MODE_SERVER_SIDE)


def on_new_xdg_decoration(data) -> None:
    """App opted into the decoration protocol, so we can negotiate who
    draws the titlebar/borders. We always pick "server draws" — see
    `request_decoration_mode`."""
    decoration = ffi.cast("struct wlr_xdg_toplevel_decoration_v1 *", data)
    # The window's drawing tree was stashed on `base.data` earlier; its
    # parent tree's `node.data` holds our Client handle. Walk to it.
    scene_surface = ffi.cast(
        "struct wlr_scene_tree *", decoration.toplevel.base.data)
    client_tree = scene_surface.node.parent
    # pylint: disable-next=consider-using-in
    if client_tree == ffi.NULL or client_tree.node.data == ffi.NULL:
        return
    client = ffi.from_handle(client_tree.node.data)
    if not isinstance(client, Client):
        return
    client.decoration = decoration

    def on_destroy(_d):
        for handle in listeners:
            handle.remove()
        client.decoration = None

    listeners = [
        listen(lib.pywl_xdg_decoration_request_mode(decoration),
               lambda _d: request_decoration_mode(client)),
        listen(lib.pywl_xdg_decoration_destroy(decoration), on_destroy),
    ]
    client.listeners.extend(listeners)
    request_decoration_mode(client)


def _initial_float_geometry(
    monitor: Monitor, _client: Client,
) -> tuple[int, int, int, int]:
    """Where to place a brand-new floating window: half the screen,
    centred."""
    mx, my, mw, mh = monitor.w
    w, h = int(mw * 0.5), int(mh * 0.5)
    return (mx + (mw - w) // 2, my + (mh - h) // 2, w, h)


def _rule_for(client: Client) -> config.AppRule | None:
    """First user-configured `AppRule` whose app_id/title match this
    window, or None if no rule applies."""
    app_id, title = client.app_id, client.title
    for rule in config.rules:
        if rule.app_id and rule.app_id not in app_id:
            continue
        if rule.title and rule.title not in title:
            continue
        return rule
    return None


# --- Popups -----------------------------------------------------------------

def on_new_xdg_popup(server: Server, data) -> None:
    """An app opened a popup (menu, tooltip, autocomplete, ...). Attach it
    to its parent window's drawing tree so it appears next to the
    parent, and subscribe to its events."""
    xdg_popup = ffi.cast("struct wlr_xdg_popup *", data)
    # The parent (a window or a panel) stashed its popup-host drawing
    # tree on `surface->data`; same lookup works for either kind.
    # pylint: disable-next=consider-using-in
    if xdg_popup.parent == ffi.NULL or xdg_popup.parent.data == ffi.NULL:
        return
    parent_tree = ffi.cast("struct wlr_scene_tree *", xdg_popup.parent.data)
    popup_tree = lib.wlr_scene_xdg_surface_create(parent_tree, xdg_popup.base)
    xdg_popup.base.data = ffi.cast("void *", popup_tree)

    popup = Popup(xdg_popup=xdg_popup, scene_tree=popup_tree)
    server.popups.append(popup)
    surface = xdg_popup.base.surface
    popup.listeners = [
        listen(lib.pywl_surface_commit(surface),
               lambda d: on_popup_commit(server, popup, d)),
        listen(lib.pywl_xdg_popup_destroy(xdg_popup),
               lambda d: on_popup_destroy(server, popup, d)),
    ]


def on_popup_commit(_server: Server, popup: Popup, _data) -> None:
    """On the popup's first content update, ack its initial size so it can
    actually appear."""
    if popup.xdg_popup.base.initial_commit:
        lib.wlr_xdg_surface_schedule_configure(popup.xdg_popup.base)


def on_popup_destroy(server: Server, popup: Popup, _data) -> None:
    """Popup is gone (menu closed, tooltip dismissed): drop our event
    subscriptions and forget about it."""
    for handle in popup.listeners:
        handle.remove()
    if popup in server.popups:
        server.popups.remove(popup)


# --- Layer shell ------------------------------------------------------------

# ZWLR layer 0..3 (BG, BOTTOM, TOP, OVERLAY) maps to our Layer enum.
_LAYERMAP = (Layer.BACKGROUND, Layer.BOTTOM, Layer.TOP, Layer.OVERLAY)


def on_new_layer_surface(server: Server, data) -> None:
    """Something like a panel, wallpaper, notification or lock screen
    appeared. Place it on the right drawing layer (background/bottom/
    top/overlay) for the right screen, then subscribe to its events."""
    wlr = ffi.cast("struct wlr_layer_surface_v1 *", data)
    if wlr.output == ffi.NULL:
        if server.selected_monitor is None:
            lib.wlr_layer_surface_v1_destroy(wlr)
            return
        wlr.output = server.selected_monitor.wlr_output
    monitor = next(
        (m for m in server.monitors if m.wlr_output == wlr.output), None)
    if monitor is None:
        lib.wlr_layer_surface_v1_destroy(wlr)
        return

    scene_parent = server.layers[_LAYERMAP[wlr.pending.layer]]
    scene_layer = lib.wlr_scene_layer_surface_v1_create(scene_parent, wlr)
    # Popups on wallpaper/bottom panels need to appear above the windows,
    # so put them in the Top drawing layer; popups on top/overlay panels
    # just stay with their parent.
    popup_parent = (server.layers[Layer.TOP]
                    if wlr.current.layer < lib.ZWLR_LAYER_SHELL_V1_LAYER_TOP
                    else scene_layer.tree)
    popups = lib.wlr_scene_tree_create(popup_parent)
    layer = LayerSurface(
        wlr=wlr, scene_layer=scene_layer, scene_tree=scene_layer.tree,
        monitor=monitor, popups=popups)
    layer.handle = ffi.new_handle(layer)
    scene_layer.tree.node.data = layer.handle
    popups.node.data = layer.handle
    # Stash the popup-host drawing tree (see on_new_xdg_popup).
    wlr.surface.data = ffi.cast("void *", popups)
    monitor.layer_surfaces[wlr.pending.layer].append(layer)
    lib.wlr_surface_send_enter(wlr.surface, wlr.output)

    layer.listeners = [
        listen(lib.pywl_surface_commit(wlr.surface),
               lambda d: on_layer_commit(server, layer, d)),
        listen(lib.pywl_surface_map(wlr.surface),
               lambda d: on_layer_map(server, layer, d)),
        listen(lib.pywl_surface_unmap(wlr.surface),
               lambda d: on_layer_unmap(server, layer, d)),
        listen(lib.pywl_layer_surface_destroy(wlr),
               lambda d: on_layer_destroy(server, layer, d)),
    ]


def on_layer_commit(server: Server, layer: LayerSurface, _data) -> None:
    """Panel/wallpaper updated its content. On the first commit, size it.
    Later commits may move it to a different drawing layer (e.g. a panel
    asking to go on top instead of bottom); always re-run the layer
    layout so panels still fit and tiled windows step out of their way.
    """
    wlr = layer.wlr
    if layer.monitor is None:
        return
    if wlr.initial_commit:
        # Temporarily pretend the pending state is current so we can size
        # the panel right now, then put it back — actually promoting it
        # would make the protocol layer fire the change twice.
        old_current = wlr.current
        wlr.current = wlr.pending
        arrange_layers(server, layer.monitor)
        wlr.current = old_current
        return
    if wlr.current.committed == 0 and layer.mapped == wlr.surface.mapped:
        return
    layer.mapped = bool(wlr.surface.mapped)

    target_parent = server.layers[_LAYERMAP[wlr.current.layer]]
    if target_parent != layer.scene_tree.node.parent:
        lib.wlr_scene_node_reparent(
            ffi.addressof(layer.scene_tree.node), target_parent)
        # Move popups to the Top drawing layer too, unless this panel
        # already lives on top/overlay (above the windows).
        popup_parent = (server.layers[Layer.TOP]
                        if wlr.current.layer
                        < lib.ZWLR_LAYER_SHELL_V1_LAYER_TOP
                        else target_parent)
        lib.wlr_scene_node_reparent(
            ffi.addressof(layer.popups.node), popup_parent)
        for bucket in layer.monitor.layer_surfaces:
            if layer in bucket:
                bucket.remove(layer)
                break
        layer.monitor.layer_surfaces[wlr.current.layer].append(layer)
    arrange_layers(server, layer.monitor)


def on_layer_map(server: Server, layer: LayerSurface, _data) -> None:
    """Panel/wallpaper became visible: enable its drawing and re-run the
    layer layout so windows give it the room it asks for."""
    layer.mapped = True
    lib.wlr_scene_node_set_enabled(
        ffi.addressof(layer.scene_tree.node), True)
    if layer.monitor is not None:
        arrange_layers(server, layer.monitor)


def on_layer_unmap(server: Server, layer: LayerSurface, _data) -> None:
    """Panel/wallpaper hidden itself: stop drawing it, return its space
    to the windows, and refocus if it had keyboard input."""
    layer.mapped = False
    lib.wlr_scene_node_set_enabled(
        ffi.addressof(layer.scene_tree.node), False)
    if server.exclusive_focus is layer:
        server.exclusive_focus = None
    if layer.monitor is not None:
        arrange_layers(server, layer.monitor)
    if (layer.wlr.surface
            == lib.pywl_seat_keyboard_focused_surface(server.seat)):
        focus_client(
            server, top_client(server, server.selected_monitor), lift=True)
    # Cursor may have been over this surface; re-pick what's under it.
    process_cursor_motion(server, 0)


def on_layer_destroy(server: Server, layer: LayerSurface, _data) -> None:
    """Panel/wallpaper went away for good: drop event subscriptions,
    forget about it, and re-arrange."""
    for handle in layer.listeners:
        handle.remove()
    layer.listeners.clear()
    if layer.monitor is not None:
        for bucket in layer.monitor.layer_surfaces:
            if layer in bucket:
                bucket.remove(layer)
                break
    if server.exclusive_focus is layer:
        server.exclusive_focus = None
    # scene_layer.tree tears itself down with the layer surface; the popups
    # sibling tree is ours, so destroy it explicitly.
    if layer.popups is not None:
        lib.wlr_scene_node_destroy(ffi.addressof(layer.popups.node))
        layer.popups = None
    layer.handle = None
    monitor = layer.monitor
    layer.monitor = None
    if monitor is not None:
        arrange_layers(server, monitor)


def arrange_layer(
    monitor: Monitor,
    layer_list: list,
    usable_area,
    exclusive: bool,
) -> None:
    """Configure each layer surface against `usable_area`; mutates it via
    `wlr_scene_layer_surface_v1_configure` for positive exclusive zones."""
    full_area = ffi.new("struct wlr_box *")
    full_area.x, full_area.y, full_area.width, full_area.height = monitor.m
    for layer in layer_list:
        wlr = layer.wlr
        if not wlr.initialized:
            continue
        if exclusive != (wlr.current.exclusive_zone > 0):
            continue
        lib.wlr_scene_layer_surface_v1_configure(
            layer.scene_layer, full_area, usable_area)
        # Pin popups to the configured surface origin so they anchor right.
        lib.wlr_scene_node_set_position(
            ffi.addressof(layer.popups.node),
            layer.scene_tree.node.x, layer.scene_tree.node.y)


def arrange_layers(server: Server, monitor: Monitor) -> None:
    """Two-pass layout: exclusive surfaces first (top to bottom) so they
    shrink the usable area, then non-exclusive surfaces fill what's left.
    If the usable area changed, re-arrange the monitor's tiled clients.
    Finally, pick the topmost keyboard-interactive overlay/top surface."""
    if not monitor.wlr_output.enabled:
        return
    usable = ffi.new("struct wlr_box *")
    usable.x, usable.y, usable.width, usable.height = monitor.m

    for i in (3, 2, 1, 0):
        arrange_layer(monitor, monitor.layer_surfaces[i], usable, True)

    new_w = (usable.x, usable.y, usable.width, usable.height)
    if new_w != monitor.w:
        monitor.w = new_w
        arrange(server, monitor)

    for i in (3, 2, 1, 0):
        arrange_layer(monitor, monitor.layer_surfaces[i], usable, False)

    # The topmost top/overlay panel that wants keyboard input grabs focus
    # exclusively (e.g. a launcher) — unless the lock screen is up, which
    # always wins.
    if server.locked:
        return
    none = lib.ZWLR_LAYER_SURFACE_V1_KEYBOARD_INTERACTIVITY_NONE
    for i in (3, 2):
        for layer in reversed(monitor.layer_surfaces[i]):
            if (layer.wlr.current.keyboard_interactive == none
                    or not layer.mapped):
                continue
            focus_client(server, None, lift=False)
            server.exclusive_focus = layer
            kb = lib.wlr_seat_get_keyboard(server.seat)
            if kb != ffi.NULL:
                lib.wlr_seat_keyboard_notify_enter(
                    server.seat, layer.wlr.surface,
                    kb.keycodes, kb.num_keycodes,
                    ffi.addressof(kb.modifiers))
            else:
                lib.wlr_seat_keyboard_notify_enter(
                    server.seat, layer.wlr.surface, ffi.NULL, 0, ffi.NULL)
            return


# --- Focus ------------------------------------------------------------------

def focus_client(
    server: Server, client: Client | None, *, lift: bool
) -> None:
    """Make `client` the focused window: keyboard input goes to it, its
    border switches to the focus colour, and (if `lift`) it's raised to
    the top of its layer. Pass `None` to clear focus entirely."""
    old_surface = lib.pywl_seat_keyboard_focused_surface(server.seat)
    if client is not None and lift:
        lib.wlr_scene_node_raise_to_top(
            ffi.addressof(client.scene_tree.node))
    if client is not None and client.surface == old_surface:
        return

    # Deactivate and recolour the previously focused window.
    old = next((c for c in server.fstack if c.surface == old_surface), None)
    if old is not None and old is not client:
        if old.xdg_toplevel is not None:
            lib.wlr_xdg_toplevel_set_activated(old.xdg_toplevel, False)
        _set_border_color(
            old, config.urgent_color if old.urgent else config.border_color)

    if client is None:
        lib.wlr_seat_keyboard_clear_focus(server.seat)
        print_status(server)
        return

    # Mark this as the most-recently-focused window.
    if client in server.fstack:
        server.fstack.remove(client)
    server.fstack.insert(0, client)

    if client.monitor is not None:
        server.selected_monitor = client.monitor

    if lift:
        lib.wlr_scene_node_raise_to_top(
            ffi.addressof(client.scene_tree.node))

    if client.xdg_toplevel is not None:
        lib.wlr_xdg_toplevel_set_activated(client.xdg_toplevel, True)
    _set_border_color(client, config.focus_color)
    client.urgent = False

    # Pass the keys currently held down and the modifier state, so the
    # newly focused window sees keys the user is already pressing.
    kb = lib.wlr_seat_get_keyboard(server.seat)
    if kb != ffi.NULL:
        lib.wlr_seat_keyboard_notify_enter(
            server.seat, client.surface,
            kb.keycodes, kb.num_keycodes,
            ffi.addressof(kb.modifiers))
    else:
        lib.wlr_seat_keyboard_notify_enter(
            server.seat, client.surface, ffi.NULL, 0, ffi.NULL)
    print_status(server)


def top_client(server: Server, monitor: Monitor | None) -> Client | None:
    """Most-recently-focused window currently visible on this screen, or
    None if there isn't one."""
    if monitor is None:
        return None
    for client in server.fstack:
        if client.mapped and _visible(client, monitor):
            return client
    return None


def _is_focused(server: Server, client: Client) -> bool:
    """True if this is the currently focused window."""
    return bool(server.fstack) and server.fstack[0] is client


# --- Actions: focus / window ------------------------------------------------

def action_focus_stack(server: Server, direction: int) -> None:
    """Cycle focus to the next (`+1`) or previous (`-1`) visible window
    on the current screen."""
    monitor = server.selected_monitor
    if monitor is None:
        return
    visible = [c for c in server.clients if _visible(c, monitor)]
    if not visible:
        return
    current = top_client(server, monitor)
    idx = visible.index(current) if current in visible else -1
    nxt = visible[(idx + direction) % len(visible)]
    focus_client(server, nxt, lift=True)


def action_kill_client(server: Server, _arg) -> None:
    """Politely ask the focused window to close (the app may show a
    confirmation dialog)."""
    client = top_client(server, server.selected_monitor)
    if client is not None and client.xdg_toplevel is not None:
        lib.wlr_xdg_toplevel_send_close(client.xdg_toplevel)


def action_toggle_floating(server: Server, _arg) -> None:
    """Switch the focused window between tiled and floating."""
    client = top_client(server, server.selected_monitor)
    if client is None or client.fullscreen:
        return
    set_floating(server, client, not client.floating)


def set_floating(server: Server, client: Client, floating: bool) -> None:
    """Move a window between the tiled and floating drawing layers and
    re-tile the screen."""
    if client.floating == floating:
        return
    client.floating = floating
    new_parent = server.layers[Layer.FLOAT if floating else Layer.TILE]
    lib.wlr_scene_node_reparent(
        ffi.addressof(client.scene_tree.node), new_parent)
    if client.monitor is not None:
        arrange(server, client.monitor)
    print_status(server)


def action_toggle_fullscreen(server: Server, _arg) -> None:
    """Toggle fullscreen for the focused window."""
    client = top_client(server, server.selected_monitor)
    if client is None:
        return
    set_fullscreen(server, client, not client.fullscreen)


def set_fullscreen(server: Server, client: Client, fullscreen: bool) -> None:
    """Make a window fill the screen (or restore it). Saves the previous
    geometry so we can restore it on un-fullscreen, and hides borders
    while fullscreen."""
    if client.monitor is None:
        return
    client.fullscreen = fullscreen
    if client.xdg_toplevel is not None:
        lib.wlr_xdg_toplevel_set_fullscreen(client.xdg_toplevel, fullscreen)
    new_layer = Layer.FULLSCREEN if fullscreen else (
        Layer.FLOAT if client.floating else Layer.TILE)
    lib.wlr_scene_node_reparent(
        ffi.addressof(client.scene_tree.node), server.layers[new_layer])
    client.border_width = 0 if fullscreen else config.border_width
    if fullscreen:
        client.prev_geometry = client.geometry
        resize(server, client, client.monitor.m)
    else:
        resize(server, client, client.prev_geometry)
    arrange(server, client.monitor)
    print_status(server)


def action_zoom(server: Server, _arg) -> None:
    """Swap the focused tiled client into the master slot."""
    monitor = server.selected_monitor
    if monitor is None:
        return
    client = top_client(server, monitor)
    if client is None or client.floating or client.fullscreen:
        return
    tiled = [c for c in server.clients
             if c.monitor is monitor and _visible(c, monitor)
             and not c.floating and not c.fullscreen]
    if len(tiled) < 2 or tiled[0] is client:
        # Already in the main slot (or only one window): pull the next
        # window up instead.
        if len(tiled) >= 2 and tiled[0] is client:
            client = tiled[1]
        else:
            return
    server.clients.remove(client)
    # Put it where the previous main window was.
    first = tiled[0]
    server.clients.insert(server.clients.index(first), client)
    arrange(server, monitor)
    focus_client(server, client, lift=True)


# --- Actions: tags ----------------------------------------------------------

def action_view(server: Server, mask: int) -> None:
    """Switch this screen to show the given workspace tags. Remembers the
    previous selection so the user can toggle back."""
    monitor = server.selected_monitor
    if monitor is None:
        return
    mask &= TAG_ALL
    if mask == monitor.tags[monitor.seltags]:
        return
    monitor.seltags ^= 1
    if mask:
        monitor.tags[monitor.seltags] = mask
    arrange(server, monitor)
    focus_client(server, top_client(server, monitor), lift=True)


def action_toggle_view(server: Server, mask: int) -> None:
    """Show or hide the given workspace tags in addition to the current
    selection (lets the user view multiple workspaces at once)."""
    monitor = server.selected_monitor
    if monitor is None:
        return
    new_mask = (monitor.tags[monitor.seltags] ^ (mask & TAG_ALL)) & TAG_ALL
    if new_mask == 0:
        return
    monitor.tags[monitor.seltags] = new_mask
    arrange(server, monitor)
    focus_client(server, top_client(server, monitor), lift=True)


def action_tag(server: Server, mask: int) -> None:
    """Move the focused window to a specific workspace."""
    client = top_client(server, server.selected_monitor)
    if client is None:
        return
    new_tags = (mask & TAG_ALL) or client.tags
    if new_tags == client.tags:
        return
    client.tags = new_tags
    arrange(server, client.monitor)
    focus_client(server, top_client(server, server.selected_monitor), lift=True)


def action_toggle_tag(server: Server, mask: int) -> None:
    """Make the focused window appear on or disappear from the given
    workspace (in addition to the workspaces it's already on)."""
    client = top_client(server, server.selected_monitor)
    if client is None:
        return
    new_tags = (client.tags ^ (mask & TAG_ALL)) & TAG_ALL
    if new_tags == 0:
        return
    client.tags = new_tags
    arrange(server, client.monitor)
    focus_client(server, top_client(server, server.selected_monitor), lift=True)


# --- Actions: layout --------------------------------------------------------

def action_set_layout(server: Server, arg) -> None:
    """Switch this screen's layout. `arg=None` toggles between current
    and previous; an integer selects a layout by index."""
    monitor = server.selected_monitor
    if monitor is None:
        return
    if arg is None:
        monitor.sellt ^= 1
    elif 0 <= arg < len(config.layouts):
        if monitor.layouts[monitor.sellt] != arg:
            monitor.sellt ^= 1
            monitor.layouts[monitor.sellt] = arg
    arrange(server, monitor)
    print_status(server)


def action_set_master_factor(server: Server, delta: float) -> None:
    """Grow or shrink the master column in the tile layout, clamped so
    neither column disappears."""
    monitor = server.selected_monitor
    if monitor is None:
        return
    new_factor = monitor.master_factor + delta
    if not 0.05 <= new_factor <= 0.95:
        return
    monitor.master_factor = new_factor
    arrange(server, monitor)


def action_inc_num_master(server: Server, delta: int) -> None:
    """Add or remove a slot in the master column of the tile layout."""
    monitor = server.selected_monitor
    if monitor is None:
        return
    monitor.num_master = max(0, monitor.num_master + delta)
    arrange(server, monitor)


# --- Actions: monitors ------------------------------------------------------

def action_focus_monitor(server: Server, direction: int) -> None:
    """Move keyboard focus to the next or previous screen."""
    if server.selected_monitor is None:
        return
    target = monitor_in_direction(server, server.selected_monitor, direction)
    if target is None or target is server.selected_monitor:
        return
    server.selected_monitor = target
    focus_client(server, top_client(server, target), lift=True)


def action_tag_monitor(server: Server, direction: int) -> None:
    """Move the focused window to the next or previous screen."""
    client = top_client(server, server.selected_monitor)
    if client is None or server.selected_monitor is None:
        return
    target = monitor_in_direction(server, server.selected_monitor, direction)
    if target is None or target is server.selected_monitor:
        return
    old_monitor = client.monitor
    client.monitor = target
    client.tags = target.selected_tags
    arrange(server, old_monitor)
    arrange(server, target)
    focus_client(server, client, lift=True)


# --- Actions: misc ----------------------------------------------------------

def action_quit(server: Server, _arg) -> None:
    """Tell the main loop to exit on its next iteration."""
    server.stop = True


def action_spawn(_server: Server, arg) -> None:
    """Launch an external program (e.g. a terminal) from a keybinding."""
    if arg:
        spawn(arg)


def action_move_resize(server: Server, mode: str) -> None:
    """Start a keyboard-driven move or resize of the focused window
    (the window becomes floating if it wasn't already)."""
    client = top_client(server, server.selected_monitor)
    if client is None or client.fullscreen:
        return
    begin_grab(server, client,
               CursorMode.MOVE if mode == "move" else CursorMode.RESIZE)


# --- Registry --------------------------------------------------------------

ACTIONS: dict[str, Callable[[Server, object], None]] = {
    "spawn": action_spawn,
    "focus_stack": action_focus_stack,
    "kill_client": action_kill_client,
    "toggle_floating": action_toggle_floating,
    "toggle_fullscreen": action_toggle_fullscreen,
    "zoom": action_zoom,
    "view": action_view,
    "toggle_view": action_toggle_view,
    "tag": action_tag,
    "toggle_tag": action_toggle_tag,
    "set_layout": action_set_layout,
    "set_master_factor": action_set_master_factor,
    "inc_num_master": action_inc_num_master,
    "focus_monitor": action_focus_monitor,
    "tag_monitor": action_tag_monitor,
    "move_resize": action_move_resize,
    "quit": action_quit,
}


# --- Input: keyboard -------------------------------------------------------

def on_new_input(server: Server, data) -> None:
    """A new keyboard or mouse was plugged in. Attach it to our shared
    keyboard group (so it merges with the others) or to the cursor."""
    device = ffi.cast("struct wlr_input_device *", data)
    if device.type == lib.WLR_INPUT_DEVICE_KEYBOARD:
        kb = lib.wlr_keyboard_from_input_device(device)
        group_kb = lib.pywl_keyboard_group_keyboard(server.keyboard_group)
        lib.wlr_keyboard_set_keymap(kb, group_kb.keymap)
        lib.wlr_keyboard_group_add_keyboard(server.keyboard_group, kb)
    elif device.type == lib.WLR_INPUT_DEVICE_POINTER:
        lib.wlr_cursor_attach_input_device(server.cursor, device)
    caps = lib.WL_SEAT_CAPABILITY_POINTER | lib.WL_SEAT_CAPABILITY_KEYBOARD
    lib.wlr_seat_set_capabilities(server.seat, caps)


def on_keyboard_modifiers(server: Server, _data) -> None:
    """User pressed/released a modifier (Shift, Ctrl, ...): forward the
    new modifier state to whichever window has keyboard focus."""
    kb = lib.pywl_keyboard_group_keyboard(server.keyboard_group)
    lib.wlr_seat_set_keyboard(server.seat, kb)
    lib.wlr_seat_keyboard_notify_modifiers(
        server.seat, ffi.addressof(kb.modifiers))


def on_keyboard_key(server: Server, data) -> None:
    """A key was pressed or released. On press, check our keybindings
    first; if none match, forward the key to the focused window."""
    ev = ffi.cast("struct wlr_keyboard_key_event *", data)
    kb = lib.pywl_keyboard_group_keyboard(server.keyboard_group)
    handled = False
    if ev.state == lib.WL_KEYBOARD_KEY_STATE_PRESSED:
        sym = lib.pywl_keyboard_keysym(kb, ev.keycode)
        mods = lib.wlr_keyboard_get_modifiers(kb) & ~_IGNORE_MODS
        handled = dispatch_key(server, mods, sym)
    if not handled:
        lib.wlr_seat_set_keyboard(server.seat, kb)
        lib.wlr_seat_keyboard_notify_key(
            server.seat, ev.time_msec, ev.keycode, ev.state)


_keysym_cache: dict[str, int] = {}


def _keysym(name: str) -> int:
    """Look up an xkb keysym by name (e.g. "Return", "a"), with caching
    so we don't pay the lookup cost on every keypress."""
    sym = _keysym_cache.get(name)
    if sym is None:
        sym = lib.xkb_keysym_from_name(name.encode(), 0)
        _keysym_cache[name] = sym
    return sym


def dispatch_key(server: Server, mods: int, sym: int) -> bool:
    """If any user keybinding matches, run its action and return True;
    otherwise return False so the key is forwarded to the focused
    window."""
    for binding in config.keys:
        if (mods == (binding.mod & ~_IGNORE_MODS)
                and sym == _keysym(binding.sym)):
            ACTIONS[binding.action](server, binding.arg)
            return True
    return False


# --- Input: cursor / pointer -----------------------------------------------

def on_cursor_motion(server: Server, data) -> None:
    """Relative mouse movement (a normal mouse): nudge the cursor by the
    reported delta and re-pick what's under it."""
    ev = ffi.cast("struct wlr_pointer_motion_event *", data)
    lib.wlr_cursor_move(server.cursor, ffi.NULL, ev.delta_x, ev.delta_y)
    process_cursor_motion(server, ev.time_msec)


def on_cursor_motion_absolute(server: Server, data) -> None:
    """Absolute pointer position (e.g. a touchscreen or a tablet sending
    "the pointer is at this exact spot"): warp the cursor there."""
    ev = ffi.cast("struct wlr_pointer_motion_absolute_event *", data)
    lib.wlr_cursor_warp_absolute(server.cursor, ffi.NULL, ev.x, ev.y)
    process_cursor_motion(server, ev.time_msec)


def process_cursor_motion(server: Server, time_msec: int) -> None:
    """After any kind of cursor movement: update which screen is selected,
    feed an in-progress move/resize, or tell the window under the cursor
    that the pointer is over it."""
    cursor_x = server.cursor.x
    cursor_y = server.cursor.y

    # Update the selected screen as the cursor crosses between them.
    under = monitor_at(server, cursor_x, cursor_y)
    if under is not None and under is not server.selected_monitor:
        server.selected_monitor = under
        print_status(server)

    if server.grab is not None:
        _drag_grab(server, cursor_x, cursor_y)
        return

    hit = surface_at(server, cursor_x, cursor_y)
    if hit is None:
        lib.wlr_cursor_set_xcursor(
            server.cursor, server.xcursor_mgr, b"default")
        lib.wlr_seat_pointer_clear_focus(server.seat)
        return
    _client, surface, sx, sy = hit
    lib.wlr_seat_pointer_notify_enter(server.seat, surface, sx, sy)
    lib.wlr_seat_pointer_notify_motion(server.seat, time_msec, sx, sy)


def on_cursor_button(server: Server, data) -> None:
    """Mouse button pressed or released. On press, check our mouse
    bindings, then click-to-focus; otherwise forward the event to the
    window under the cursor. A release ends any in-progress drag."""
    ev = ffi.cast("struct wlr_pointer_button_event *", data)
    pressed = ev.state == lib.WL_POINTER_BUTTON_STATE_PRESSED

    if not pressed and server.grab is not None:
        end_grab(server)
        # Don't forward the button release that ended a drag.
        return

    if pressed:
        kb = lib.pywl_keyboard_group_keyboard(server.keyboard_group)
        mods = lib.wlr_keyboard_get_modifiers(kb) & ~_IGNORE_MODS
        for binding in config.buttons:
            if (mods == (binding.mod & ~_IGNORE_MODS)
                    and ev.button == binding.button):
                ACTIONS[binding.action](server, binding.arg)
                return

        # Click-to-focus.
        hit = surface_at(server, server.cursor.x, server.cursor.y)
        if hit is not None:
            client, _surface, _sx, _sy = hit
            if client is not None:
                focus_client(server, client, lift=True)
        else:
            focus_client(server, None, lift=False)

    lib.wlr_seat_pointer_notify_button(
        server.seat, ev.time_msec, ev.button, ev.state)


def on_cursor_axis(server: Server, data) -> None:
    """Scroll wheel event: forward to the focused window."""
    ev = ffi.cast("struct wlr_pointer_axis_event *", data)
    lib.wlr_seat_pointer_notify_axis(
        server.seat, ev.time_msec, ev.orientation, ev.delta,
        ev.delta_discrete, ev.source, ev.relative_direction)


def on_cursor_frame(server: Server, _data) -> None:
    """End-of-batch marker for a group of pointer events: tells the
    focused window "that's all for this tick"."""
    lib.wlr_seat_pointer_notify_frame(server.seat)


def surface_at(
    server: Server, lx: float, ly: float
) -> tuple[Client | None, object, float, float] | None:
    """Find what's at the global position `(lx, ly)`. Returns the window
    (or None for panels), the actual surface the pointer is over, and
    surface-local coordinates. Returns None if there's nothing there.
    """
    nx = ffi.new("double *")
    ny = ffi.new("double *")
    node = lib.wlr_scene_node_at(
        ffi.addressof(server.scene.tree.node), lx, ly, nx, ny)
    if node == ffi.NULL or node.type != lib.WLR_SCENE_NODE_BUFFER:
        return None
    buf = lib.wlr_scene_buffer_from_node(node)
    ss = lib.wlr_scene_surface_try_from_buffer(buf)
    if ss == ffi.NULL:
        return None
    # Walk up the drawing-tree hierarchy until we find a node tagged with
    # our Client handle. Panels aren't tagged (they're not Clients), so
    # we return None for those.
    tree = node.parent
    while tree != ffi.NULL and tree.node.data == ffi.NULL:
        tree = tree.node.parent
    client: Client | None = None
    if tree != ffi.NULL:
        handle = ffi.from_handle(tree.node.data)
        if isinstance(handle, Client) and handle in server.clients:
            client = handle
    return client, ss.surface, nx[0], ny[0]


# --- Move / resize grab ----------------------------------------------------

def begin_grab(server: Server, client: Client, mode: CursorMode) -> None:
    """Start an interactive move or resize of `client`. The window is
    forced floating if needed, and the cursor is changed to a grab icon.
    """
    if client.fullscreen:
        return
    if not client.floating:
        set_floating(server, client, True)
    server.grab = Grab(
        client=client, mode=mode,
        cursor_x=server.cursor.x, cursor_y=server.cursor.y,
        geometry=client.geometry,
    )
    server.cursor_mode = mode
    icon = b"grabbing" if mode is CursorMode.MOVE else b"bottom_right_corner"
    lib.wlr_cursor_set_xcursor(server.cursor, server.xcursor_mgr, icon)


def _drag_grab(server: Server, x: float, y: float) -> None:
    """Apply the current move/resize based on how far the cursor has moved
    from where the drag started."""
    grab = server.grab
    if grab is None:
        return
    dx = int(x - grab.cursor_x)
    dy = int(y - grab.cursor_y)
    gx, gy, gw, gh = grab.geometry
    if grab.mode is CursorMode.MOVE:
        resize(server, grab.client, (gx + dx, gy + dy, gw, gh),
               interactive=True)
    else:
        new_w = max(50, gw + dx)
        new_h = max(50, gh + dy)
        resize(server, grab.client, (gx, gy, new_w, new_h), interactive=True)


def end_grab(server: Server) -> None:
    """Finish an interactive move/resize: forget the grab state and let
    normal cursor handling resume."""
    server.grab = None
    server.cursor_mode = CursorMode.NORMAL
    process_cursor_motion(server, 0)


# --- Seat ------------------------------------------------------------------

def on_request_set_cursor(server: Server, data) -> None:
    """An app asked to change the cursor image (e.g. an editor showing an
    I-beam over text). Honour it, but only while we're not in the middle
    of a move/resize — we don't want apps overriding the grab cursor.
    """
    if server.cursor_mode is not CursorMode.NORMAL:
        return
    ev = ffi.cast(
        "struct wlr_seat_pointer_request_set_cursor_event *", data)
    if ev.seat_client == lib.pywl_seat_pointer_focused_client(server.seat):
        lib.wlr_cursor_set_surface(
            server.cursor, ev.surface, ev.hotspot_x, ev.hotspot_y)


def on_request_set_selection(server: Server, data) -> None:
    """App is asking to take over the clipboard. Allow it."""
    ev = ffi.cast("struct wlr_seat_request_set_selection_event *", data)
    lib.wlr_seat_set_selection(server.seat, ev.source, ev.serial)


def on_request_set_primary_selection(_server: Server, _data) -> None:
    """Same as clipboard, but for the X-style middle-click selection. Not
    yet supported (we wire the handler ahead of enabling the protocol).
    """


# --- GPU reset -------------------------------------------------------------

def on_gpu_reset(_server: Server, _data) -> None:
    """The GPU was reset (e.g. driver crash). We don't rebuild the
    renderer yet — just log it."""
    sys.stderr.write("warning: GPU reset; renderer not rebuilt yet\n")


# --- Spawn -----------------------------------------------------------------

def spawn(cmd: str | list[str]) -> None:
    """Launch an external program detached from us, so when we exit it
    doesn't get killed and it doesn't get our signals."""
    if isinstance(cmd, str):
        subprocess.Popen(cmd, shell=True, start_new_session=True)  # pylint: disable=consider-using-with
    else:
        subprocess.Popen(list(cmd), start_new_session=True)  # pylint: disable=consider-using-with


# --- print_status ----------------------------------------------------------

def print_status(server: Server) -> None:
    """One-line summary of every monitor's state on stdout, dwl-style."""
    selected = server.selected_monitor
    for monitor in server.monitors:
        focused = top_client(server, monitor)
        title = focused.title if focused else ""
        appid = focused.app_id if focused else ""
        marker = "*" if monitor is selected else " "
        sys.stdout.write(
            f"{marker} mon={monitor.name} "
            f"tags={monitor.selected_tags:#x} "
            f"layout={monitor.layout_symbol!r} "
            f"appid={appid!r} title={title!r}\n"
        )
    sys.stdout.flush()


# --- Helpers ---------------------------------------------------------------

def die(msg: str) -> None:
    """Print an error message and exit with a non-zero status."""
    sys.stderr.write(f"pywl: {msg}\n")
    sys.exit(1)


def _trace(msg: str) -> None:
    """Debug logging. Writes to both stderr and `trace.log`. No-op when
    `_TRACE` is False."""
    if not _TRACE:
        return
    if not hasattr(_trace, "file"):
        # pylint: disable-next=consider-using-with
        _trace.file = open("trace.log", "w", buffering=1, encoding="utf-8")
    line = f"[pywl] {msg}\n"
    sys.stderr.write(line)
    sys.stderr.flush()
    _trace.file.write(line)


def _float_color(rgba):
    """Convert a 4-tuple of floats into a C `float[4]` for wlroots APIs
    that take colours."""
    return ffi.new("float[4]", list(rgba))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
