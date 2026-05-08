"""Minimal Wayland compositor using pywlroots."""

# pylint: disable=import-error
# pylint: disable=wrong-import-order
# pylint: disable=wrong-import-position
# pylint: disable=missing-function-docstring

import argparse
import os
import signal
import subprocess
import sys

import logging
from xkbcommon import xkb

TERMINAL = "foot"


def main() -> int:
    # Install signal handlers as early as possible so SIGINT during startup
    # doesn't raise KeyboardInterrupt mid-construction.
    _interrupted = _install_signal_handlers()

    args = _parse_args(sys.argv[1:])

    log_init(logging.INFO)
    ctx = create_context(_interrupted, scale=args.scale)
    run(ctx)

    return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="pywl")
    parser.add_argument(
        "--scale",
        type=_positive_float,
        default=1.0,
        help="Output scale factor applied to every output (default: 1.0).",
    )
    return parser.parse_args(argv)


def _install_signal_handlers():
    interrupted = False

    def _interrupt(_sig, _frame):
        nonlocal interrupted
        interrupted = True

    signal.signal(signal.SIGINT, _interrupt)
    signal.signal(signal.SIGTERM, _interrupt)

    return lambda: interrupted


def _positive_float(value: str) -> float:
    f = float(value)
    if f <= 0:
        raise argparse.ArgumentTypeError(f"scale must be > 0, got {value!r}")
    return f


from dataclasses import dataclass, field
from functools import partial
from typing import Callable

from pywayland.protocol.wayland import WlKeyboard, WlSeat
from pywayland.server import Display, Listener
from wlroots import helper as wlroots_helper
from wlroots import ffi as wlr_ffi
from wlroots import lib as wlr_lib
from wlroots.util.clock import Timespec
from wlroots.util.log import log_init
from wlroots.wlr_types import OutputLayout
from wlroots.wlr_types.cursor import (
    Cursor,
    PointerAxisEvent,
    PointerButtonEvent,
    PointerMotionAbsoluteEvent,
    PointerMotionEvent,
    WarpMode,
)
from wlroots.wlr_types.pointer import ButtonState
from wlroots.wlr_types.data_device_manager import DataDeviceManager
from wlroots.wlr_types.input_device import InputDevice, InputDeviceType
from wlroots.wlr_types.keyboard import (
    Keyboard,
    KeyboardKeyEvent,
    KeyboardModifier,
)
from wlroots.wlr_types.output import CustomMode, Output, OutputEventRequestState
from wlroots.wlr_types.scene import (
    Scene,
    SceneBuffer,
    SceneNode,
    SceneNodeType,
    SceneOutput,
    SceneRect,
    SceneSurface,
    SceneTree,
)
from wlroots.wlr_types.seat import Seat
from wlroots.wlr_types.xcursor_manager import XCursorManager
from wlroots.wlr_types.xdg_decoration_v1 import (
    XdgDecorationManagerV1,
    XdgToplevelDecorationV1,
    XdgToplevelDecorationV1Mode,
)
from wlroots.wlr_types.xdg_shell import XdgShell, XdgSurface, XdgSurfaceRole


BORDER_WIDTH = 2
BORDER_COLOR = (0.2, 0.2, 0.2, 1.0)
BORDER_COLOR_FOCUSED = (0.298, 0.471, 0.6, 1.0)


@dataclass(eq=False)
class View:
    """A mapped xdg toplevel and its scene tree node, used as focus target."""

    xdg_surface: XdgSurface
    scene_tree: SceneTree
    border: SceneRect


@dataclass
class Context:  # pylint: disable=too-many-instance-attributes
    """Compositor state. Passed as first argument to every handler."""

    interrupted: Callable[[], bool]
    output_scale: float

    display: Display
    compositor: object
    allocator: object
    renderer: object
    backend: object

    output_layout: OutputLayout
    scene: Scene
    data_device_manager: DataDeviceManager
    seat: Seat
    xdg_shell: XdgShell
    xdg_decoration_manager: XdgDecorationManagerV1
    cursor: Cursor
    cursor_manager: XCursorManager
    xkb_context: object
    xkb_keymap: object

    running: bool = True
    # Strong refs: pywayland holds raw pointers; Python-side GC mustn't reap.
    keyboards: list[Keyboard] = field(default_factory=list)
    outputs: list[Output] = field(default_factory=list)
    listeners: list[Listener] = field(default_factory=list)
    views: list[View] = field(default_factory=list)  # left-to-right tiling order; views[0] is master
    focused_view: View | None = None


def create_context(  # pylint: disable=too-many-locals
    interrupted: Callable[[], bool], *, scale: float = 1.0
) -> Context:
    display = Display()
    (
        compositor,
        allocator,
        renderer,
        backend,
        _subcompositor,
    ) = wlroots_helper.build_compositor(display)

    output_layout = OutputLayout()
    scene = Scene()
    scene.attach_output_layout(output_layout)

    xkb_context = xkb.Context()
    xkb_keymap = xkb_context.keymap_new_from_names()

    ctx = Context(
        interrupted=interrupted,
        output_scale=scale,
        display=display,
        compositor=compositor,
        allocator=allocator,
        renderer=renderer,
        backend=backend,
        output_layout=output_layout,
        scene=scene,
        data_device_manager=DataDeviceManager(display),
        seat=Seat(display, "seat0"),
        xdg_shell=XdgShell(display),
        xdg_decoration_manager=XdgDecorationManagerV1.create(display),
        cursor=Cursor(output_layout),
        cursor_manager=XCursorManager(None, 24),
        xkb_context=xkb_context,
        xkb_keymap=xkb_keymap,
    )

    ctx.xdg_shell.new_surface_event.add(
        Listener(partial(on_new_xdg_surface, ctx))
    )
    ctx.xdg_decoration_manager.new_toplevel_decoration_event.add(
        Listener(on_new_toplevel_decoration)
    )
    # pylint: disable=no-member  # backend signals are dynamic via cffi
    ctx.backend.new_output_event.add(Listener(partial(on_new_output, ctx)))
    ctx.backend.new_input_event.add(Listener(partial(on_new_input, ctx)))
    # pylint: enable=no-member

    for sig, handler in (
        (ctx.cursor.motion_event, on_cursor_motion),
        (ctx.cursor.motion_absolute_event, on_cursor_motion_absolute),
        (ctx.cursor.button_event, on_cursor_button),
        (ctx.cursor.axis_event, on_cursor_axis),
        (ctx.cursor.frame_event, on_cursor_frame),
    ):
        listener = Listener(partial(handler, ctx))
        sig.add(listener)
        ctx.listeners.append(listener)

    return ctx


def run(ctx: Context) -> None:
    socket = ctx.display.add_socket().decode()
    os.environ["WAYLAND_DISPLAY"] = socket
    print(f"pywl: running on WAYLAND_DISPLAY={socket}")

    # Spawn a terminal so the empty session is immediately usable.
    # Detach via start_new_session so it survives our shutdown path
    # (os._exit) and doesn't receive our SIGINT.
    # pylint: disable-next=consider-using-with  # intentionally detached
    subprocess.Popen([TERMINAL], start_new_session=True)

    # Drive the wayland event loop ourselves so Python signal
    # handlers get a chance to fire between dispatches.
    loop = ctx.display.get_event_loop()
    with ctx.backend:
        while ctx.running and not ctx.interrupted():
            ctx.display.flush_clients()
            loop.dispatch(200)  # ms; bounded so signals fire promptly

    # Skip Python's GC-driven teardown: pywlroots' object destructors
    # don't agree with libwayland on ordering and segfault. The OS
    # will reclaim everything cleanly.
    os._exit(0)


# --- handlers ---


def on_new_output(ctx: Context, _listener, output: Output) -> None:
    # Keep a strong Python ref to the Output wrapper. Its frame_event /
    # request_state_event Signal objects are attributes of this wrapper,
    # and Signal._link is the only thing keeping our Listener (and its
    # wl_listener cdata) alive. Letting the wrapper get GC'd silently
    # drops the frame callback and the host window stops rendering.
    ctx.outputs.append(output)
    output.init_render(ctx.allocator, ctx.renderer)

    mode = output.preferred_mode()
    if mode is not None:
        output.set_mode(mode)
    else:
        # wl/headless backends have no fixed modes; pick something.
        output.set_custom_mode(CustomMode(width=1280, height=720, refresh=0))
    output.set_scale(ctx.output_scale)
    output.enable()
    output.commit()

    ctx.output_layout.add_auto(output)
    scene_output = SceneOutput.create(ctx.scene, output)
    ctx.cursor_manager.load(output.scale)

    def _on_frame(_l, _d) -> None:
        scene_output.commit()
        scene_output.send_frame_done(Timespec.get_monotonic_time())

    output.frame_event.add(Listener(_on_frame))

    def _on_request_state(_l, event: OutputEventRequestState) -> None:
        # The wl/x11 backend asks us to apply a new mode/scale when the
        # host window is resized or moved between monitors.
        output.commit(event.state)

    output.request_state_event.add(Listener(_on_request_state))


def on_new_toplevel_decoration(
    _listener, decoration: XdgToplevelDecorationV1
) -> None:
    # Force server-side decorations; we draw none, so toplevels are borderless.
    decoration.set_mode(XdgToplevelDecorationV1Mode.SERVER_SIDE)


def on_new_xdg_surface(
    ctx: Context, _listener, xdg_surface: XdgSurface
) -> None:
    if xdg_surface.role != XdgSurfaceRole.TOPLEVEL:
        return
    scene_tree = Scene.xdg_surface_create(ctx.scene.tree, xdg_surface)
    border = SceneRect(
        ctx.scene.tree, 0, 0, wlr_ffi.new("float[4]", list(BORDER_COLOR))
    )
    # Render border below the surface so it shows only as a frame around it.
    border.node.place_below(scene_tree.node)
    view = View(
        xdg_surface=xdg_surface, scene_tree=scene_tree, border=border
    )
    # Tag the scene node so click hit-tests can recover the view by
    # walking up parents from whatever (sub)surface was hit.
    scene_tree.node.data = view

    surface = xdg_surface.surface

    def _on_map(_l, _d) -> None:
        ctx.views.insert(0, view)
        apply_tiling(ctx)
        focus_view(ctx, view)

    def _on_unmap(_l, _d) -> None:
        view.border.node.destroy()
        if view in ctx.views:
            ctx.views.remove(view)
            apply_tiling(ctx)
        if ctx.focused_view is view:
            ctx.focused_view = None
            ctx.seat.keyboard_clear_focus()
            # Promote the new master.
            if ctx.views:
                focus_view(ctx, ctx.views[0])

    for sig, handler in (
        (surface.map_event, _on_map),
        (surface.unmap_event, _on_unmap),
    ):
        listener = Listener(handler)
        sig.add(listener)
        ctx.listeners.append(listener)


# --- tiling ---


def apply_tiling(ctx: Context) -> None:
    """Master/stack: views[0] is master on the left, rest stack vertically right.

    Single window: full screen. New windows are inserted at index 0 and so
    become the new master.
    """
    if not ctx.views or not ctx.outputs:
        return
    box = ctx.output_layout.get_box(ctx.outputs[0])
    master, *stack = ctx.views
    if not stack:
        place_tile(master, box.x, box.y, box.width, box.height)
        return
    master_w = box.width // 2
    stack_w = box.width - master_w
    place_tile(master, box.x, box.y, master_w, box.height)
    n = len(stack)
    tile_h = box.height // n
    remainder = box.height - tile_h * n
    y = box.y
    stack_x = box.x + master_w
    for i, view in enumerate(stack):
        h = tile_h + (1 if i < remainder else 0)
        place_tile(view, stack_x, y, stack_w, h)
        y += h


def set_border_color(
    view: View, color: tuple[float, float, float, float]
) -> None:
    view.border.set_color(wlr_ffi.new("float[4]", list(color)))


def place_tile(view: View, x: int, y: int, w: int, h: int) -> None:
    """Position a view at (x, y) with size (w, h), framed by a border."""
    b = BORDER_WIDTH
    view.border.node.set_position(x, y)
    view.border.set_size(w, h)
    view.scene_tree.node.set_position(x + b, y + b)
    view.xdg_surface.set_size(max(0, w - 2 * b), max(0, h - 2 * b))


# --- focus ---


def focus_view(ctx: Context, view: View) -> None:
    if ctx.focused_view is view:
        return
    prev = ctx.focused_view
    if prev is not None:
        prev.xdg_surface.set_activated(False)
        set_border_color(prev, BORDER_COLOR)
    view.scene_tree.node.raise_to_top()
    view.xdg_surface.set_activated(True)
    set_border_color(view, BORDER_COLOR_FOCUSED)
    ctx.focused_view = view
    keyboard = ctx.seat.get_keyboard()
    if keyboard is not None:
        ctx.seat.keyboard_notify_enter(view.xdg_surface.surface, keyboard)


def view_at(ctx: Context, lx: float, ly: float) -> View | None:
    result = ctx.scene.tree.node.node_at(lx, ly)
    if result is None:
        return None
    node, _sx, _sy = result
    return view_for_node(node)


def view_for_node(node: SceneNode | None) -> View | None:
    while node is not None:
        data = node.data
        if isinstance(data, View):
            return data
        parent = node.parent
        if parent is None:
            return None
        node = parent.node
    return None


# --- compositor key bindings ---


def handle_compositor_key(
    ctx: Context, keyboard: Keyboard, event: KeyboardKeyEvent
) -> bool:
    """Intercept compositor-level shortcuts. Returns True if consumed."""
    if is_exit_chord(keyboard, event):
        ctx.running = False
        return True
    return False


def is_exit_chord(keyboard: Keyboard, event: KeyboardKeyEvent) -> bool:
    """Alt+Shift+E: terminate the compositor."""
    required = KeyboardModifier.ALT | KeyboardModifier.SHIFT
    return (
        event.state == WlKeyboard.key_state.pressed
        and (keyboard.modifier & required) == required
        and event_keysym(keyboard, event) == keysym("e")
    )


# --- input ---


def on_new_input(ctx: Context, _listener, device: InputDevice) -> None:
    if device.type == InputDeviceType.KEYBOARD:
        keyboard = Keyboard.from_input_device(device)
        keyboard.set_keymap(ctx.xkb_keymap)
        keyboard.set_repeat_info(25, 600)

        def _on_key(_l, event: KeyboardKeyEvent) -> None:
            if handle_compositor_key(ctx, keyboard, event):
                return
            ctx.seat.set_keyboard(keyboard)
            ctx.seat.keyboard_notify_key(event)

        def _on_modifiers(_l, _d) -> None:
            ctx.seat.set_keyboard(keyboard)
            ctx.seat.keyboard_notify_modifiers(keyboard.modifiers)

        for sig, handler in (
            (keyboard.key_event, _on_key),
            (keyboard.modifiers_event, _on_modifiers),
        ):
            listener = Listener(handler)
            sig.add(listener)
            ctx.listeners.append(listener)

        ctx.keyboards.append(keyboard)
        ctx.seat.set_keyboard(keyboard)
        ctx.seat.set_capabilities(
            WlSeat.capability.keyboard | WlSeat.capability.pointer
        )
    elif device.type == InputDeviceType.POINTER:
        ctx.cursor.attach_input_device(device)
        ctx.seat.set_capabilities(
            WlSeat.capability.keyboard | WlSeat.capability.pointer
        )


# --- pointer ---


def on_cursor_motion(ctx: Context, _l, event: PointerMotionEvent) -> None:
    ctx.cursor.move(
        event.delta_x, event.delta_y, input_device=event.pointer.base
    )
    process_cursor_motion(ctx, event.time_msec)


def on_cursor_motion_absolute(
    ctx: Context, _l, event: PointerMotionAbsoluteEvent
) -> None:
    ctx.cursor.warp(
        WarpMode.AbsoluteClosest,
        event.x,
        event.y,
        input_device=event.pointer.base,
    )
    process_cursor_motion(ctx, event.time_msec)


def on_cursor_button(ctx: Context, _l, event: PointerButtonEvent) -> None:
    if event.button_state == ButtonState.PRESSED:
        view = view_at(ctx, ctx.cursor.x, ctx.cursor.y)
        if view is not None:
            focus_view(ctx, view)
    ctx.seat.pointer_notify_button(
        event.time_msec, event.button, event.button_state
    )


def on_cursor_axis(ctx: Context, _l, event: PointerAxisEvent) -> None:
    ctx.seat.pointer_notify_axis(
        event.time_msec,
        event.orientation,
        event.delta,
        event.delta_discrete,
        event.source,
    )


def on_cursor_frame(ctx: Context, _l, _d) -> None:
    ctx.seat.pointer_notify_frame()


def process_cursor_motion(ctx: Context, time_msec: int) -> None:
    surface, sx, sy = surface_at(ctx, ctx.cursor.x, ctx.cursor.y)
    if surface is None:
        # Default cursor image when over no client surface.
        ctx.cursor.set_xcursor(ctx.cursor_manager, "default")
        ctx.seat.pointer_notify_clear_focus()
        return
    ctx.seat.pointer_notify_enter(surface, sx, sy)
    ctx.seat.pointer_notify_motion(time_msec, sx, sy)


def surface_at(ctx: Context, lx: float, ly: float):
    result = ctx.scene.tree.node.node_at(lx, ly)
    if result is None:
        return None, 0.0, 0.0
    node, sx, sy = result
    if node.type != SceneNodeType.BUFFER:
        return None, 0.0, 0.0
    scene_buffer = SceneBuffer.from_node(node)
    scene_surface = SceneSurface.from_buffer(scene_buffer)
    if scene_surface is None:
        return None, 0.0, 0.0
    return scene_surface.surface, sx, sy


# --- xkb keysym helpers ---


def keysym(name: str) -> int:
    """Return the xkb keysym for ``name`` (e.g. "q", "Escape", "F1")."""
    return wlr_lib.xkb_keysym_from_name(
        name.encode(), wlr_lib.XKB_KEYSYM_NO_FLAGS
    )


def event_keysym(keyboard: Keyboard, event: KeyboardKeyEvent) -> int:
    """Resolve a key event to a layout-aware, lowercased xkb keysym."""
    xkb_keycode = event.keycode + 8  # libinput → xkb (X11 +8 offset)
    # pylint: disable-next=protected-access  # _ptr is the only path to xkb_state
    xkb_state = keyboard._ptr.xkb_state
    sym = wlr_lib.xkb_state_key_get_one_sym(xkb_state, xkb_keycode)
    return wlr_lib.xkb_keysym_to_lower(sym)


if __name__ == "__main__":
    sys.exit(main())
