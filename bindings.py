"""Inline cffi bindings to wlroots 0.19 / libwayland-server / xkbcommon.

Compiled at import time via `cffi.FFI.set_source` + `compile()` into a
tempdir and loaded as `_pywl_cffi`. Re-exports `ffi`, `lib`, and the
`listen` helper.

Only the symbols required by main.py are exposed. Struct field access is
avoided by writing tiny C accessor helpers, so we do not depend on wlroots
struct layout — just on its public function ABI.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import cffi


_PKGS = ("wlroots-0.19", "wayland-server", "xkbcommon", "pixman-1")
_MODULE = "_pywl_cffi"


CDEF = r"""
typedef _Bool bool;
typedef unsigned int uint32_t;
typedef int int32_t;

struct wl_list { struct wl_list *prev; struct wl_list *next; };
struct timespec { long tv_sec; long tv_nsec; };

struct wl_listener;
typedef void (*wl_notify_func_t)(struct wl_listener *, void *);
struct wl_listener {
    struct wl_list link;
    wl_notify_func_t notify;
};
struct wl_signal { struct wl_list listener_list; };

// opaque types
struct wl_display;
struct wl_event_loop;
struct wlr_backend;
struct wlr_session;
struct wlr_renderer;
struct wlr_allocator;
struct wlr_output_layout;
struct wlr_output_layout_output;
struct wlr_scene_output;
struct wlr_scene_output_layout;
struct wlr_xdg_shell;
struct wlr_surface;
struct wlr_seat;
struct wlr_compositor;
struct wlr_subcompositor;
struct wlr_data_device_manager;
struct xkb_context;
struct xkb_keymap;
// wlr_keyboard_modifiers is embedded in wlr_keyboard, so cffi needs a
// (possibly empty) layout for it. We never read its fields from Python.
struct wlr_keyboard_modifiers { ...; };

// partial decls: cffi asks the C compiler for offsets at build time, so
// these are not locked to a specific wlroots ABI — only to the existence
// and types of the fields listed here.
struct wlr_output { int width; int height; ...; };
struct wlr_cursor { double x; double y; ...; };
struct wlr_input_device { int type; ...; };
struct wlr_keyboard {
    struct wlr_keyboard_modifiers modifiers;
    ...;
};
struct wlr_scene_tree;
struct wlr_scene_node {
    int x;
    int y;
    int type;
    struct wlr_scene_tree *parent;
    void *data;
    ...;
};
struct wlr_scene_tree { struct wlr_scene_node node; ...; };
struct wlr_scene { struct wlr_scene_tree tree; ...; };
struct wlr_scene_surface { struct wlr_surface *surface; ...; };
struct wlr_xdg_surface {
    struct wlr_surface *surface;
    bool initial_commit;
    void *data;
    ...;
};
struct wlr_xdg_toplevel { struct wlr_xdg_surface *base; ...; };
struct wlr_xdg_popup {
    struct wlr_xdg_surface *base;
    struct wlr_surface *parent;
    ...;
};
struct wlr_keyboard_key_event {
    uint32_t time_msec;
    uint32_t keycode;
    uint32_t state;
    ...;
};

// libwayland-server
void wl_list_remove(struct wl_list *);
struct wl_display *wl_display_create(void);
void wl_display_destroy(struct wl_display *);
void wl_display_destroy_clients(struct wl_display *);
void wl_display_run(struct wl_display *);
void wl_display_terminate(struct wl_display *);
void wl_display_flush_clients(struct wl_display *);
int wl_event_loop_dispatch(struct wl_event_loop *, int timeout);
const char *wl_display_add_socket_auto(struct wl_display *);
struct wl_event_loop *wl_display_get_event_loop(struct wl_display *);

// wlroots
struct wlr_backend *wlr_backend_autocreate(
        struct wl_event_loop *, struct wlr_session **);
bool wlr_backend_start(struct wlr_backend *);
void wlr_backend_destroy(struct wlr_backend *);

struct wlr_renderer *wlr_renderer_autocreate(struct wlr_backend *);
bool wlr_renderer_init_wl_display(struct wlr_renderer *, struct wl_display *);
void wlr_renderer_destroy(struct wlr_renderer *);

struct wlr_allocator *wlr_allocator_autocreate(
        struct wlr_backend *, struct wlr_renderer *);
void wlr_allocator_destroy(struct wlr_allocator *);
void wlr_scene_node_destroy(struct wlr_scene_node *);

struct wlr_compositor *wlr_compositor_create(
        struct wl_display *, uint32_t version, struct wlr_renderer *);
struct wlr_subcompositor *wlr_subcompositor_create(struct wl_display *);
struct wlr_data_device_manager *wlr_data_device_manager_create(
        struct wl_display *);

struct wlr_output_layout *wlr_output_layout_create(struct wl_display *);
struct wlr_output_layout_output *wlr_output_layout_add_auto(
        struct wlr_output_layout *, struct wlr_output *);

struct wlr_output_state;
struct wlr_output_mode;
bool wlr_output_init_render(struct wlr_output *,
        struct wlr_allocator *, struct wlr_renderer *);
void wlr_output_state_set_enabled(struct wlr_output_state *, bool enabled);
void wlr_output_state_set_mode(
        struct wlr_output_state *, struct wlr_output_mode *);
struct wlr_output_mode *wlr_output_preferred_mode(struct wlr_output *);
bool wlr_output_commit_state(
        struct wlr_output *, const struct wlr_output_state *);

struct wlr_scene *wlr_scene_create(void);
struct wlr_scene_output_layout *wlr_scene_attach_output_layout(
        struct wlr_scene *, struct wlr_output_layout *);
struct wlr_scene_output *wlr_scene_output_create(
        struct wlr_scene *, struct wlr_output *);
void wlr_scene_output_layout_add_output(struct wlr_scene_output_layout *,
        struct wlr_output_layout_output *, struct wlr_scene_output *);
struct wlr_scene_output *wlr_scene_get_scene_output(
        struct wlr_scene *, struct wlr_output *);
bool wlr_scene_output_commit(struct wlr_scene_output *, void *);
struct wlr_scene_tree *wlr_scene_xdg_surface_create(
        struct wlr_scene_tree *, struct wlr_xdg_surface *);

struct wlr_xdg_shell *wlr_xdg_shell_create(
        struct wl_display *, uint32_t version);
void wlr_xdg_toplevel_set_size(struct wlr_xdg_toplevel *, int32_t, int32_t);
uint32_t wlr_xdg_toplevel_set_activated(struct wlr_xdg_toplevel *, bool activated);
void wlr_xdg_toplevel_send_close(struct wlr_xdg_toplevel *);
struct wlr_xdg_surface *wlr_xdg_surface_try_from_wlr_surface(struct wlr_surface *);
void wlr_xdg_surface_schedule_configure(struct wlr_xdg_surface *);

struct wlr_seat *wlr_seat_create(struct wl_display *, const char *);
void wlr_seat_set_capabilities(struct wlr_seat *, uint32_t caps);
struct wlr_scene_node;
void wlr_scene_node_set_position(struct wlr_scene_node *, int x, int y);
void wlr_seat_set_keyboard(struct wlr_seat *, struct wlr_keyboard *);
void wlr_seat_keyboard_notify_key(struct wlr_seat *, uint32_t time_msec,
        uint32_t key, uint32_t state);
void wlr_seat_keyboard_notify_modifiers(struct wlr_seat *,
        struct wlr_keyboard_modifiers *modifiers);
void wlr_seat_keyboard_notify_enter(struct wlr_seat *, struct wlr_surface *,
        const uint32_t keycodes[], size_t num_keycodes,
        struct wlr_keyboard_modifiers *modifiers);

struct wlr_keyboard *wlr_keyboard_from_input_device(struct wlr_input_device *);
bool wlr_keyboard_set_keymap(struct wlr_keyboard *, struct xkb_keymap *);
void wlr_keyboard_set_repeat_info(
        struct wlr_keyboard *, int32_t rate_hz, int32_t delay_ms);
uint32_t wlr_keyboard_get_modifiers(struct wlr_keyboard *);
struct wlr_keyboard *wlr_seat_get_keyboard(struct wlr_seat *);
void wlr_seat_keyboard_clear_focus(struct wlr_seat *);

// Cursor / pointer
struct wlr_xcursor_manager;
struct wlr_pointer;
struct wlr_pointer_motion_event {
    uint32_t time_msec;
    double delta_x;
    double delta_y;
    ...;
};
struct wlr_pointer_motion_absolute_event {
    uint32_t time_msec;
    double x;
    double y;
    ...;
};
struct wlr_pointer_button_event {
    uint32_t time_msec;
    uint32_t button;
    uint32_t state;
    ...;
};
struct wlr_pointer_axis_event {
    uint32_t time_msec;
    uint32_t source;
    uint32_t orientation;
    uint32_t relative_direction;
    double delta;
    int32_t delta_discrete;
    ...;
};

struct wlr_cursor *wlr_cursor_create(void);
void wlr_cursor_destroy(struct wlr_cursor *);
void wlr_cursor_attach_output_layout(
        struct wlr_cursor *, struct wlr_output_layout *);
void wlr_cursor_attach_input_device(
        struct wlr_cursor *, struct wlr_input_device *);
void wlr_cursor_move(struct wlr_cursor *, struct wlr_input_device *,
        double delta_x, double delta_y);
void wlr_cursor_warp_absolute(struct wlr_cursor *, struct wlr_input_device *,
        double x, double y);
void wlr_cursor_set_xcursor(struct wlr_cursor *,
        struct wlr_xcursor_manager *, const char *);

struct wlr_xcursor_manager *wlr_xcursor_manager_create(
        const char *name, uint32_t size);
void wlr_xcursor_manager_destroy(struct wlr_xcursor_manager *);
bool wlr_xcursor_manager_load(struct wlr_xcursor_manager *, float scale);

void wlr_seat_pointer_notify_enter(struct wlr_seat *, struct wlr_surface *,
        double sx, double sy);
void wlr_seat_pointer_notify_motion(
        struct wlr_seat *, uint32_t time_msec, double sx, double sy);
uint32_t wlr_seat_pointer_notify_button(struct wlr_seat *,
        uint32_t time_msec, uint32_t button, uint32_t state);
void wlr_seat_pointer_notify_axis(struct wlr_seat *, uint32_t time_msec,
        uint32_t orientation, double value, int32_t value_discrete,
        uint32_t source, uint32_t relative_direction);
void wlr_seat_pointer_notify_frame(struct wlr_seat *);
void wlr_seat_pointer_clear_focus(struct wlr_seat *);

void wlr_scene_node_raise_to_top(struct wlr_scene_node *);

struct xkb_context *xkb_context_new(int flags);
void xkb_context_unref(struct xkb_context *);
struct xkb_keymap *xkb_keymap_new_from_names(struct xkb_context *,
        const void *names, int flags);
void xkb_keymap_unref(struct xkb_keymap *);

void wlr_scene_output_send_frame_done(struct wlr_scene_output *,
        struct timespec *);

void wlr_log_init(int verbosity, void *callback);

// enum wlr_input_device_type
#define WLR_INPUT_DEVICE_KEYBOARD ...
#define WLR_INPUT_DEVICE_POINTER ...

// wl_seat capability bits
#define WL_SEAT_CAPABILITY_POINTER ...
#define WL_SEAT_CAPABILITY_KEYBOARD ...

// wl_pointer button state
#define WL_POINTER_BUTTON_STATE_PRESSED ...
#define WL_KEYBOARD_KEY_STATE_PRESSED ...

// enum wlr_keyboard_modifier
#define WLR_MODIFIER_ALT ...
#define WLR_MODIFIER_SHIFT ...

// Resolve a key name (e.g. "Return") to an xkb keysym, or 0 if unknown.
uint32_t xkb_keysym_from_name(const char *name, int flags);

// First xkb keysym for an event keycode on this keyboard, or 0.
uint32_t pywl_keyboard_keysym(struct wlr_keyboard *kb, uint32_t keycode);

// linux/input-event-codes.h
#define BTN_LEFT ...
#define BTN_RIGHT ...

// our helpers
void pywl_signal_add(struct wl_signal *, struct wl_listener *);
struct wl_signal *pywl_backend_new_output(struct wlr_backend *);
struct wl_signal *pywl_output_frame(struct wlr_output *);
struct wl_signal *pywl_output_request_state(struct wlr_output *);
struct wl_signal *pywl_output_destroy_signal(struct wlr_output *);
struct wl_signal *pywl_input_device_destroy_signal(struct wlr_input_device *);
struct wl_signal *pywl_xdg_shell_new_toplevel(struct wlr_xdg_shell *);
struct wl_signal *pywl_surface_commit(struct wlr_surface *);
// wlr_output_state has non-trivial init and unstable layout; expose
// alloc/free instead of declaring it.
struct wlr_output_state *pywl_output_state_new(void);
void pywl_output_state_free(struct wlr_output_state *);

struct wl_signal *pywl_xdg_toplevel_destroy(struct wlr_xdg_toplevel *);
struct wl_signal *pywl_xdg_shell_new_popup(struct wlr_xdg_shell *);
struct wl_signal *pywl_xdg_popup_destroy(struct wlr_xdg_popup *);

struct wlr_output_event_request_state {
    const struct wlr_output_state *state;
    ...;
};

// cursor signals (events.* lives in an anonymous sub-struct)
struct wl_signal *pywl_cursor_motion(struct wlr_cursor *);
struct wl_signal *pywl_cursor_motion_absolute(struct wlr_cursor *);
struct wl_signal *pywl_cursor_button(struct wlr_cursor *);
struct wl_signal *pywl_cursor_axis(struct wlr_cursor *);
struct wl_signal *pywl_cursor_frame(struct wlr_cursor *);

// hit-test entry point
struct wlr_scene_buffer;
struct wlr_scene_node *wlr_scene_node_at(struct wlr_scene_node *root,
        double lx, double ly, double *nx, double *ny);
struct wlr_scene_buffer *wlr_scene_buffer_from_node(struct wlr_scene_node *);
struct wlr_scene_surface *wlr_scene_surface_try_from_buffer(
        struct wlr_scene_buffer *);

// enum wlr_scene_node_type
#define WLR_SCENE_NODE_BUFFER ...

// keyboard input field accessors (struct layout we don't want to declare)
struct wl_signal *pywl_backend_new_input(struct wlr_backend *);
struct wl_signal *pywl_surface_map(struct wlr_surface *);
struct wl_signal *pywl_keyboard_key_signal(struct wlr_keyboard *);
struct wl_signal *pywl_keyboard_modifiers_signal(struct wlr_keyboard *);
extern "Python" void _pywl_dispatch(struct wl_listener *, void *);
"""


SOURCE = r"""
#define WLR_USE_UNSTABLE
#include <stdint.h>
#include <stdbool.h>
#include <stdlib.h>
#include <time.h>
#include <linux/input-event-codes.h>
#include <wayland-server-core.h>
#include <wlr/backend.h>
#include <wlr/render/allocator.h>
#include <wlr/render/wlr_renderer.h>
#include <wlr/types/wlr_compositor.h>
#include <wlr/types/wlr_data_device.h>
#include <wlr/types/wlr_output.h>
#include <wlr/types/wlr_output_layout.h>
#include <wlr/types/wlr_scene.h>
#include <wlr/types/wlr_seat.h>
#include <wlr/types/wlr_subcompositor.h>
#include <wlr/types/wlr_input_device.h>
#include <wlr/types/wlr_keyboard.h>
#include <wlr/types/wlr_pointer.h>
#include <wlr/types/wlr_cursor.h>
#include <wlr/types/wlr_xcursor_manager.h>
#include <wlr/types/wlr_xdg_shell.h>
#include <wlr/util/log.h>
#include <xkbcommon/xkbcommon.h>

void pywl_signal_add(struct wl_signal *s, struct wl_listener *l) {
    wl_signal_add(s, l);
}

uint32_t pywl_keyboard_keysym(struct wlr_keyboard *kb, uint32_t keycode) {
    const xkb_keysym_t *syms;
    // wlr_keyboard_key_event.keycode is evdev; xkb expects +8.
    int n = xkb_state_key_get_syms(kb->xkb_state, keycode + 8, &syms);
    return n > 0 ? (uint32_t)syms[0] : 0;
}

struct wl_signal *pywl_backend_new_output(struct wlr_backend *b) {
    return &b->events.new_output;
}
struct wl_signal *pywl_output_frame(struct wlr_output *o) {
    return &o->events.frame;
}
struct wl_signal *pywl_output_request_state(struct wlr_output *o) {
    return &o->events.request_state;
}
struct wl_signal *pywl_output_destroy_signal(struct wlr_output *o) {
    return &o->events.destroy;
}
struct wl_signal *pywl_input_device_destroy_signal(struct wlr_input_device *d) {
    return &d->events.destroy;
}
struct wl_signal *pywl_xdg_shell_new_toplevel(struct wlr_xdg_shell *s) {
    return &s->events.new_toplevel;
}
struct wl_signal *pywl_surface_commit(struct wlr_surface *s) {
    return &s->events.commit;
}

struct wlr_output_state *pywl_output_state_new(void) {
    struct wlr_output_state *s = calloc(1, sizeof(*s));
    wlr_output_state_init(s);
    return s;
}
void pywl_output_state_free(struct wlr_output_state *s) {
    wlr_output_state_finish(s);
    free(s);
}

struct wl_signal *pywl_backend_new_input(struct wlr_backend *b) {
    return &b->events.new_input;
}
struct wl_signal *pywl_surface_map(struct wlr_surface *s) {
    return &s->events.map;
}
struct wl_signal *pywl_keyboard_key_signal(struct wlr_keyboard *k) {
    return &k->events.key;
}
struct wl_signal *pywl_keyboard_modifiers_signal(struct wlr_keyboard *k) {
    return &k->events.modifiers;
}
struct wl_signal *pywl_xdg_toplevel_destroy(struct wlr_xdg_toplevel *t) {
    return &t->events.destroy;
}
struct wl_signal *pywl_xdg_shell_new_popup(struct wlr_xdg_shell *s) {
    return &s->events.new_popup;
}
struct wl_signal *pywl_xdg_popup_destroy(struct wlr_xdg_popup *p) {
    return &p->events.destroy;
}

struct wl_signal *pywl_cursor_motion(struct wlr_cursor *c) {
    return &c->events.motion;
}
struct wl_signal *pywl_cursor_motion_absolute(struct wlr_cursor *c) {
    return &c->events.motion_absolute;
}
struct wl_signal *pywl_cursor_button(struct wlr_cursor *c) {
    return &c->events.button;
}
struct wl_signal *pywl_cursor_axis(struct wlr_cursor *c) {
    return &c->events.axis;
}
struct wl_signal *pywl_cursor_frame(struct wlr_cursor *c) {
    return &c->events.frame;
}
"""


def _build():
    """Compile the inline cffi extension and return its (ffi, lib)."""
    def pkgcfg(flag, *pkgs):
        return subprocess.check_output(
            ["pkg-config", flag, *pkgs]
        ).decode().split()

    cflags = pkgcfg("--cflags", *_PKGS)
    libs = pkgcfg("--libs", *_PKGS)
    include_dirs = [a[2:] for a in cflags if a.startswith("-I")]
    extra_cflags = (
        [a for a in cflags if not a.startswith("-I")]
        + ["-DWLR_USE_UNSTABLE"]
    )
    libraries = [a[2:] for a in libs if a.startswith("-l")]
    library_dirs = [a[2:] for a in libs if a.startswith("-L")]

    build_dir = tempfile.mkdtemp(prefix="pywl-build-")

    # wlroots includes <xdg-shell-protocol.h>, which must be generated
    # locally from the xdg-shell.xml protocol description shipped with
    # wayland-protocols.
    protocols_dir = subprocess.check_output(
        ["pkg-config", "--variable=pkgdatadir", "wayland-protocols"]
    ).decode().strip()
    subprocess.check_call([
        "wayland-scanner", "server-header",
        os.path.join(protocols_dir, "stable/xdg-shell/xdg-shell.xml"),
        os.path.join(build_dir, "xdg-shell-protocol.h"),
    ])
    include_dirs.append(build_dir)

    builder = cffi.FFI()
    builder.cdef(CDEF)
    builder.set_source(
        _MODULE,
        SOURCE,
        include_dirs=include_dirs,
        libraries=libraries,
        library_dirs=library_dirs,
        extra_compile_args=extra_cflags + ["-w"],
    )
    print(f"Compiling bindings in {build_dir} ...")
    so_path = builder.compile(tmpdir=build_dir)

    spec = importlib.util.spec_from_file_location(_MODULE, so_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE] = mod
    spec.loader.exec_module(mod)

    return mod.ffi, mod.lib


ffi, lib = _build()


# --- Listener plumbing -------------------------------------------------------
# One wl_listener per Python callback, all routed through a single C
# trampoline that looks up the callable by listener address.

@ffi.def_extern()
def _pywl_dispatch(wl_listener, data):
    entry = listen.listeners.get(int(ffi.cast("uintptr_t", wl_listener)))
    if entry is not None:
        entry[1](data)


def listen(signal, callback):
    """Register `callback(data)` on `signal`. Returns a handle with a
    `.remove()` method that detaches (and frees) the underlying wl_listener.
    `.remove()` is safe to call more than once."""
    wl_listener = ffi.new("struct wl_listener *")
    wl_listener.notify = lib._pywl_dispatch
    key = int(ffi.cast("uintptr_t", wl_listener))
    listen.listeners[key] = (wl_listener, callback)
    lib.pywl_signal_add(signal, wl_listener)

    def remove():
        entry = listen.listeners.pop(key, None)
        if entry is None:
            return
        held, _cb = entry
        lib.wl_list_remove(ffi.addressof(held[0], "link"))

    return SimpleNamespace(remove=remove)


# wl_listener -> (wl_listener, callable)
listen.listeners = {}
