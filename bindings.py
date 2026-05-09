"""Inline cffi bindings to wlroots 0.19 / libwayland-server / xkbcommon.

Compiled at import time via `cffi.FFI.set_source` + `compile()` into a
tempdir and loaded as `_pywl_cffi`. Re-exports `ffi`, `lib`, and the
`add_listener` helper.

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

/* opaque types */
struct wl_display;
struct wl_event_loop;
struct wlr_backend;
struct wlr_session;
struct wlr_renderer;
struct wlr_allocator;
struct wlr_output;
struct wlr_output_layout;
struct wlr_output_layout_output;
struct wlr_scene;
struct wlr_scene_tree;
struct wlr_scene_output;
struct wlr_scene_output_layout;
struct wlr_xdg_shell;
struct wlr_xdg_toplevel;
struct wlr_xdg_surface;
struct wlr_surface;
struct wlr_seat;
struct wlr_compositor;
struct wlr_subcompositor;
struct wlr_data_device_manager;
struct wlr_input_device;
struct wlr_keyboard;
struct xkb_context;
struct xkb_keymap;
struct wlr_keyboard_modifiers {
    uint32_t depressed;
    uint32_t latched;
    uint32_t locked;
    uint32_t group;
};
struct wlr_keyboard_key_event {
    uint32_t time_msec;
    uint32_t keycode;
    bool update_state;
    uint32_t state;
};

/* libwayland-server */
void wl_list_remove(struct wl_list *);
struct wl_display *wl_display_create(void);
void wl_display_destroy(struct wl_display *);
void wl_display_destroy_clients(struct wl_display *);
void wl_display_run(struct wl_display *);
void wl_display_terminate(struct wl_display *);
const char *wl_display_add_socket_auto(struct wl_display *);
struct wl_event_loop *wl_display_get_event_loop(struct wl_display *);

/* wlroots */
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

struct xkb_context *xkb_context_new(int flags);
void xkb_context_unref(struct xkb_context *);
struct xkb_keymap *xkb_keymap_new_from_names(struct xkb_context *,
        const void *names, int flags);
void xkb_keymap_unref(struct xkb_keymap *);

void wlr_scene_output_send_frame_done(struct wlr_scene_output *,
        struct timespec *);

void wlr_log_init(int verbosity, void *callback);

/* enum wlr_input_device_type */
#define WLR_INPUT_DEVICE_KEYBOARD 0
#define WLR_INPUT_DEVICE_POINTER 1

/* wl_seat capability bits */
#define WL_SEAT_CAPABILITY_POINTER 1
#define WL_SEAT_CAPABILITY_KEYBOARD 2

/* our helpers */
void pywl_signal_add(struct wl_signal *, struct wl_listener *);
struct wl_signal *pywl_backend_new_output(struct wlr_backend *);
struct wl_signal *pywl_output_frame(struct wlr_output *);
struct wl_signal *pywl_xdg_shell_new_toplevel(struct wlr_xdg_shell *);
struct wl_signal *pywl_surface_commit(struct wlr_surface *);
struct wlr_xdg_surface *pywl_toplevel_base(struct wlr_xdg_toplevel *);
struct wlr_surface *pywl_xdg_surface_surface(struct wlr_xdg_surface *);
bool pywl_xdg_surface_initial_commit(struct wlr_xdg_surface *);
struct wlr_scene_tree *pywl_scene_tree(struct wlr_scene *);
/* wlr_output_state has non-trivial init (pixman region) and an unstable
   layout we don't want to declare; expose just an alloc/free pair. */
struct wlr_output_state *pywl_output_state_new(void);
void pywl_output_state_free(struct wlr_output_state *);

/* output size accessors */
int pywl_output_width(struct wlr_output *);
int pywl_output_height(struct wlr_output *);

/* scene_tree's first field is a wlr_scene_node; expose it explicitly. */
struct wlr_scene_node *pywl_scene_tree_node(struct wlr_scene_tree *);

struct wl_signal *pywl_xdg_toplevel_destroy(struct wlr_xdg_toplevel *);

/* keyboard input field accessors (struct layout we don't want to declare) */
struct wl_signal *pywl_backend_new_input(struct wlr_backend *);
struct wl_signal *pywl_surface_map(struct wlr_surface *);
struct wl_signal *pywl_keyboard_key_signal(struct wlr_keyboard *);
struct wl_signal *pywl_keyboard_modifiers_signal(struct wlr_keyboard *);
int pywl_input_device_type(struct wlr_input_device *);
struct wlr_keyboard_modifiers *pywl_keyboard_modifiers_ptr(
        struct wlr_keyboard *);
uint32_t *pywl_keyboard_keycodes(struct wlr_keyboard *);
size_t pywl_keyboard_num_keycodes(struct wlr_keyboard *);

extern "Python" void _pywl_dispatch(struct wl_listener *, void *);
"""


SOURCE = r"""
#define WLR_USE_UNSTABLE
#include <stdint.h>
#include <stdbool.h>
#include <stdlib.h>
#include <time.h>
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
#include <wlr/types/wlr_xdg_shell.h>
#include <wlr/util/log.h>

void pywl_signal_add(struct wl_signal *s, struct wl_listener *l) {
    wl_signal_add(s, l);
}

struct wl_signal *pywl_backend_new_output(struct wlr_backend *b) {
    return &b->events.new_output;
}
struct wl_signal *pywl_output_frame(struct wlr_output *o) {
    return &o->events.frame;
}
struct wl_signal *pywl_xdg_shell_new_toplevel(struct wlr_xdg_shell *s) {
    return &s->events.new_toplevel;
}
struct wl_signal *pywl_surface_commit(struct wlr_surface *s) {
    return &s->events.commit;
}

struct wlr_xdg_surface *pywl_toplevel_base(struct wlr_xdg_toplevel *t) {
    return t->base;
}
struct wlr_surface *pywl_xdg_surface_surface(struct wlr_xdg_surface *s) {
    return s->surface;
}
bool pywl_xdg_surface_initial_commit(struct wlr_xdg_surface *s) {
    return s->initial_commit;
}
struct wlr_scene_tree *pywl_scene_tree(struct wlr_scene *s) { return &s->tree; }

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
int pywl_input_device_type(struct wlr_input_device *d) {
    return d->type;
}
struct wlr_keyboard_modifiers *pywl_keyboard_modifiers_ptr(
        struct wlr_keyboard *k) {
    return &k->modifiers;
}
uint32_t *pywl_keyboard_keycodes(struct wlr_keyboard *k) {
    return k->keycodes;
}
size_t pywl_keyboard_num_keycodes(struct wlr_keyboard *k) {
    return k->num_keycodes;
}

int pywl_output_width(struct wlr_output *o) { return o->width; }
int pywl_output_height(struct wlr_output *o) { return o->height; }
struct wlr_scene_node *pywl_scene_tree_node(struct wlr_scene_tree *t) {
    return &t->node;
}
struct wl_signal *pywl_xdg_toplevel_destroy(struct wlr_xdg_toplevel *t) {
    return &t->events.destroy;
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
    so_path = builder.compile(tmpdir=build_dir)

    spec = importlib.util.spec_from_file_location(_MODULE, so_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE] = mod
    spec.loader.exec_module(mod)

    return mod.ffi, mod.lib


ffi, lib = _build()


# --- Listener plumbing -------------------------------------------------------
#
# wlroots delivers events via `struct wl_listener` whose `notify` field is a
# C function pointer. We allocate one wl_listener per Python callback, register
# them on signals, and route all of them through a single dispatch trampoline
# that looks up the Python callable by listener address.

# ptr_int -> (listener_keepalive, callable)
_listeners: dict[int, tuple[object, object]] = {}


@ffi.def_extern()
def _pywl_dispatch(listener, data):
    entry = _listeners.get(int(ffi.cast("uintptr_t", listener)))
    if entry is not None:
        entry[1](data)


def add_listener(signal, callback):
    """Register `callback(data)` on `signal`. Returns an opaque handle that
    keeps the underlying wl_listener alive; pass it to `remove_listener` to
    detach (and free) it."""
    listener = ffi.new("struct wl_listener *")
    listener.notify = lib._pywl_dispatch
    key = int(ffi.cast("uintptr_t", listener))
    _listeners[key] = (listener, callback)
    lib.pywl_signal_add(signal, listener)
    return key


def remove_listener(key):
    """Detach a listener registered with `add_listener`. Safe to call before
    the signal's owner is freed; required if the owner will be freed while
    we still hold the listener."""
    entry = _listeners.pop(key, None)
    if entry is None:
        return
    listener, _cb = entry
    lib.wl_list_remove(ffi.addressof(listener[0], "link"))
