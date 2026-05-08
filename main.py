"""Minimal Wayland compositor using pywlroots."""

import os
import signal
import sys

import logging
from xkbcommon import xkb


def main() -> int:
    # Install signal handlers as early as possible so SIGINT during startup
    # doesn't raise KeyboardInterrupt mid-construction.
    interrupted = False

    def _interrupted():
        return interrupted

    def _interrupt(_sig, _frame):
        nonlocal interrupted
        interrupted = True

    signal.signal(signal.SIGINT, _interrupt)
    signal.signal(signal.SIGTERM, _interrupt)

    log_init(logging.INFO)
    Compositor(_interrupted).run()

    return 0


from dataclasses import dataclass

from pywayland.protocol.wayland import WlSeat
from pywayland.server import Display, Listener
from wlroots import helper as wlroots_helper
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
from wlroots.wlr_types.keyboard import Keyboard, KeyboardKeyEvent, KeyboardModifiers
from wlroots.wlr_types.output import CustomMode, Output, OutputEventRequestState
from wlroots.wlr_types.scene import (
    Scene,
    SceneBuffer,
    SceneNode,
    SceneNodeType,
    SceneOutput,
    SceneSurface,
    SceneTree,
)
from wlroots.wlr_types.seat import Seat
from wlroots.wlr_types.xcursor_manager import XCursorManager
from wlroots.wlr_types.xdg_shell import XdgShell, XdgSurface, XdgSurfaceRole


@dataclass(eq=False)
class View:
    """A mapped xdg toplevel and its scene tree node, used as focus target."""

    xdg_surface: XdgSurface
    scene_tree: SceneTree


class Compositor:
    def __init__(self, interrupted) -> None:
        self._interrupted = interrupted
        self.display = Display()
        (
            self.compositor,
            self.allocator,
            self.renderer,
            self.backend,
            _subcompositor,
        ) = wlroots_helper.build_compositor(self.display)

        self.output_layout = OutputLayout()
        self.scene = Scene()
        self.scene.attach_output_layout(self.output_layout)

        self.data_device_manager = DataDeviceManager(self.display)
        self.seat = Seat(self.display, "seat0")

        self.xdg_shell = XdgShell(self.display)
        self.xdg_shell.new_surface_event.add(Listener(self._on_new_xdg_surface))

        self.backend.new_output_event.add(Listener(self._on_new_output))
        self.backend.new_input_event.add(Listener(self._on_new_input))

        # Keep strong refs to per-device/surface listeners and keyboards
        # so they aren't GC'd while wlroots still holds them.
        self._keyboards: list[Keyboard] = []
        self._listeners: list[Listener] = []
        self._views: list[View] = []  # bottom-to-top stacking order
        self._focused_view: View | None = None

        self.cursor = Cursor(self.output_layout)
        self.cursor_manager = XCursorManager(None, 24)
        for signal, handler in (
            (self.cursor.motion_event, self._on_cursor_motion),
            (self.cursor.motion_absolute_event, self._on_cursor_motion_absolute),
            (self.cursor.button_event, self._on_cursor_button),
            (self.cursor.axis_event, self._on_cursor_axis),
            (self.cursor.frame_event, self._on_cursor_frame),
        ):
            listener = Listener(handler)
            signal.add(listener)
            self._listeners.append(listener)

        self._xkb_context = xkb.Context()
        self._xkb_keymap = self._xkb_context.keymap_new_from_names()

    def run(self) -> None:
        socket = self.display.add_socket().decode()
        os.environ["WAYLAND_DISPLAY"] = socket
        print(f"pywl: running on WAYLAND_DISPLAY={socket}")

        # Drive the wayland event loop ourselves so Python signal
        # handlers get a chance to fire between dispatches.

        loop = self.display.get_event_loop()
        with self.backend:
            while not self._interrupted():
                self.display.flush_clients()
                loop.dispatch(200)  # ms; bounded so signals fire promptly

        # Skip Python's GC-driven teardown: pywlroots' object destructors
        # don't agree with libwayland on ordering and segfault. The OS
        # will reclaim everything cleanly.
        os._exit(0)

    # --- handlers ---

    def _on_new_output(self, _listener, output: Output) -> None:
        output.init_render(self.allocator, self.renderer)

        mode = output.preferred_mode()
        if mode is not None:
            output.set_mode(mode)
        else:
            # wl/headless backends have no fixed modes; pick something.
            output.set_custom_mode(CustomMode(width=1280, height=720, refresh=0))
        output.enable()
        output.commit()

        self.output_layout.add_auto(output)
        scene_output = SceneOutput.create(self.scene, output)
        self.cursor_manager.load(output.scale)

        def _on_frame(_l, _d) -> None:
            scene_output.commit()
            scene_output.send_frame_done(Timespec.get_monotonic_time())

        output.frame_event.add(Listener(_on_frame))

        def _on_request_state(_l, event: OutputEventRequestState) -> None:
            # The wl/x11 backend asks us to apply a new mode/scale when the
            # host window is resized or moved between monitors.
            output.commit(event.state)

        output.request_state_event.add(Listener(_on_request_state))

    def _on_new_xdg_surface(self, _listener, xdg_surface: XdgSurface) -> None:
        if xdg_surface.role != XdgSurfaceRole.TOPLEVEL:
            return
        scene_tree = Scene.xdg_surface_create(self.scene.tree, xdg_surface)
        view = View(xdg_surface=xdg_surface, scene_tree=scene_tree)
        # Tag the scene node so click hit-tests can recover the view by
        # walking up parents from whatever (sub)surface was hit.
        scene_tree.node.data = view

        surface = xdg_surface.surface

        def _on_map(_l, _d) -> None:
            self._views.append(view)
            self._focus_view(view)

        def _on_unmap(_l, _d) -> None:
            if view in self._views:
                self._views.remove(view)
            if self._focused_view is view:
                self._focused_view = None
                self.seat.keyboard_clear_focus()
                # Hand focus to the next view in stacking order, if any.
                if self._views:
                    self._focus_view(self._views[-1])

        for signal, handler in (
            (surface.map_event, _on_map),
            (surface.unmap_event, _on_unmap),
        ):
            listener = Listener(handler)
            signal.add(listener)
            self._listeners.append(listener)

    # --- focus ---

    def _focus_view(self, view: View) -> None:
        if self._focused_view is view:
            return
        prev = self._focused_view
        if prev is not None:
            prev.xdg_surface.set_activated(False)
        view.scene_tree.node.raise_to_top()
        view.xdg_surface.set_activated(True)
        # Keep _views ordered bottom-to-top so unmap can pick the new top.
        if view in self._views:
            self._views.remove(view)
        self._views.append(view)
        self._focused_view = view
        keyboard = self.seat.get_keyboard()
        if keyboard is not None:
            self.seat.keyboard_notify_enter(view.xdg_surface.surface, keyboard)

    def _view_at(self, lx: float, ly: float) -> View | None:
        result = self.scene.tree.node.node_at(lx, ly)
        if result is None:
            return None
        node, _sx, _sy = result
        return self._view_for_node(node)

    def _view_for_node(self, node: SceneNode | None) -> View | None:
        while node is not None:
            data = node.data
            if isinstance(data, View):
                return data
            parent = node.parent
            if parent is None:
                return None
            node = parent.node
        return None

    # --- input ---

    def _on_new_input(self, _listener, device: InputDevice) -> None:
        if device.type == InputDeviceType.KEYBOARD:
            keyboard = Keyboard.from_input_device(device)
            keyboard.set_keymap(self._xkb_keymap)
            keyboard.set_repeat_info(25, 600)

            def _on_key(_l, event: KeyboardKeyEvent) -> None:
                self.seat.set_keyboard(keyboard)
                self.seat.keyboard_notify_key(event)

            def _on_modifiers(_l, _d) -> None:
                self.seat.set_keyboard(keyboard)
                self.seat.keyboard_notify_modifiers(keyboard.modifiers)

            for signal, handler in (
                (keyboard.key_event, _on_key),
                (keyboard.modifiers_event, _on_modifiers),
            ):
                listener = Listener(handler)
                signal.add(listener)
                self._listeners.append(listener)

            self._keyboards.append(keyboard)
            self.seat.set_keyboard(keyboard)
            self.seat.set_capabilities(
                WlSeat.capability.keyboard | WlSeat.capability.pointer
            )
        elif device.type == InputDeviceType.POINTER:
            self.cursor.attach_input_device(device)
            self.seat.set_capabilities(
                WlSeat.capability.keyboard | WlSeat.capability.pointer
            )

    # --- pointer ---

    def _on_cursor_motion(self, _l, event: PointerMotionEvent) -> None:
        self.cursor.move(event.delta_x, event.delta_y, input_device=event.pointer.base)
        self._process_cursor_motion(event.time_msec)

    def _on_cursor_motion_absolute(
        self, _l, event: PointerMotionAbsoluteEvent
    ) -> None:
        self.cursor.warp(
            WarpMode.AbsoluteClosest, event.x, event.y, input_device=event.pointer.base
        )
        self._process_cursor_motion(event.time_msec)

    def _on_cursor_button(self, _l, event: PointerButtonEvent) -> None:
        if event.button_state == ButtonState.PRESSED:
            view = self._view_at(self.cursor.x, self.cursor.y)
            if view is not None:
                self._focus_view(view)
        self.seat.pointer_notify_button(event.time_msec, event.button, event.button_state)

    def _on_cursor_axis(self, _l, event: PointerAxisEvent) -> None:
        self.seat.pointer_notify_axis(
            event.time_msec,
            event.orientation,
            event.delta,
            event.delta_discrete,
            event.source,
        )

    def _on_cursor_frame(self, _l, _d) -> None:
        self.seat.pointer_notify_frame()

    def _process_cursor_motion(self, time_msec: int) -> None:
        surface, sx, sy = self._surface_at(self.cursor.x, self.cursor.y)
        if surface is None:
            # Default cursor image when over no client surface.
            self.cursor.set_xcursor(self.cursor_manager, "default")
            self.seat.pointer_notify_clear_focus()
            return
        self.seat.pointer_notify_enter(surface, sx, sy)
        self.seat.pointer_notify_motion(time_msec, sx, sy)

    def _surface_at(self, lx: float, ly: float):
        result = self.scene.tree.node.node_at(lx, ly)
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


if __name__ == "__main__":
    sys.exit(main())
