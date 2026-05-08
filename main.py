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


from pywayland.protocol.wayland import WlSeat
from pywayland.server import Display, Listener
from wlroots import helper as wlroots_helper
from wlroots.util.clock import Timespec
from wlroots.util.log import log_init
from wlroots.wlr_types import OutputLayout
from wlroots.wlr_types.data_device_manager import DataDeviceManager
from wlroots.wlr_types.input_device import InputDevice, InputDeviceType
from wlroots.wlr_types.keyboard import Keyboard, KeyboardKeyEvent, KeyboardModifiers
from wlroots.wlr_types.output import CustomMode, Output, OutputEventRequestState
from wlroots.wlr_types.scene import Scene, SceneOutput
from wlroots.wlr_types.seat import Seat
from wlroots.wlr_types.xdg_shell import XdgShell, XdgSurface, XdgSurfaceRole


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
        Scene.xdg_surface_create(self.scene.tree, xdg_surface)

        surface = xdg_surface.surface

        def _on_map(_l, _d) -> None:
            keyboard = self.seat.get_keyboard()
            if keyboard is not None:
                self.seat.keyboard_notify_enter(surface, keyboard)

        listener = Listener(_on_map)
        surface.map_event.add(listener)
        self._listeners.append(listener)

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


if __name__ == "__main__":
    sys.exit(main())
