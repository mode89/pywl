Wayland compositor written in Python on top of wlroots.

## Files

- `wl.py`: entry point.
- `bindings.py`: inline cffi bindings.
- `tests.py`: unit tests. Run with `pytest tests.py`.
- `vm/`: NixOS VM for headed testing (graphical QEMU window with virtio-gpu).

## Bindings

- `pywl_*` C helpers are plumbing only — static-inline wrappers, alloc/free for opaque-sized structs, accessors for anonymous struct members. For regular named struct fields, declare the struct in the cdef and access from Python directly. Logic stays in Python.
- All listeners share one `extern "Python"` trampoline; `listen` routes by listener address.

## Linting

Run `pylint wl.py tests.py` and address what it flags.

## Docstrings

- Write for someone unfamiliar with Wayland/wlroots: prefer "window", "screen", "app" over "toplevel", "output", "client".
- Focus on *why* and non-obvious semantics. Don't restate the field list of a dataclass or the signature of a function.
- If you feel the urge to document a field, put it as an inline comment on the field itself, not in the class docstring.
