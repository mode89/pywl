Wayland compositor written in Python on top of wlroots.

## Files

- `main.py`: entry point.
- `bindings.py`: inline cffi bindings.
- `vm/`: NixOS VM for headed testing (graphical QEMU window with virtio-gpu).

## Bindings

- wlroots types are opaque (forward-declared); only small, stable structs we
  read in Python are laid out.
- `pywl_*` C helpers are plumbing only — field accessors, static-inline
  wrappers, alloc/free for opaque-sized structs. Logic stays in Python.
- All listeners share one `extern "Python"` trampoline; `listen` routes by
  listener address.
