#!/usr/bin/env python3
"""Sample Wayland client for testing pywl features.

Extend this as new features land. Currently:

- "Request activation in 3s" button: starts a 3-second timer, then
  calls Gtk.Window.present(), which under Wayland emits an
  xdg_activation_v1.activate request. Switch focus to another window
  during the delay to observe pywl's urgent-flag behaviour (border
  turns red instead of focus being stolen).
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
# pylint: disable=wrong-import-position
from gi.repository import GLib, Gtk

ACTIVATE_DELAY_MS = 3000


def build_window(app: Gtk.Application) -> None:
    """Construct and show the one window the sample owns."""
    window = Gtk.ApplicationWindow(application=app, title="pywl sample")
    window.set_default_size(320, 120)

    button = Gtk.Button(
        label=f"Request activation in {ACTIVATE_DELAY_MS // 1000}s")
    button.connect("clicked", lambda _b: schedule_activation(window))
    window.set_child(button)
    window.present()


def schedule_activation(window: Gtk.Window) -> None:
    """Re-present after a delay. GTK only emits an xdg-activation
    request when present() runs on an unfocused window, so the user
    needs that window to focus something else first."""
    def fire() -> bool:
        window.present()
        return False  # one-shot
    GLib.timeout_add(ACTIVATE_DELAY_MS, fire)


def main() -> None:
    """Entry point."""
    app = Gtk.Application(application_id="org.pywl.sample")
    app.connect("activate", build_window)
    app.run(None)


if __name__ == "__main__":
    main()
