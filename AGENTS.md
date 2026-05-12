Wayland compositor written in Python on top of wlroots.

## Files

- `wl.py`: entry point.
- `bindings.py`: inline cffi bindings.
- `vm/`: NixOS VM for headed testing (graphical QEMU window with virtio-gpu).

## Bindings

- `pywl_*` C helpers are plumbing only — static-inline wrappers,
  alloc/free for opaque-sized structs, accessors for anonymous struct
  members. For regular named struct fields, declare the struct in the
  cdef and access from Python directly. Logic stays in Python.
- All listeners share one `extern "Python"` trampoline; `listen` routes by
  listener address.
