Wayland compositor written in Python on top of wlroots.

## Files

- `wl.py`: entry point.
- `bindings.py`: inline cffi bindings.
- `tests.py`: unit tests.
- `vm/`: NixOS VM for headed testing (graphical QEMU window with virtio-gpu).

## dwl Parity

When implementing compositor behavior that dwl already handles, mirror dwl's code flow and state transitions as closely as Python permits. Prefer translating dwl's established concepts into small Python equivalents over inventing new control flow.

- Compare against dwl's source code before changing behavior that dwl also implements.
- Preserve dwl's ordering of side effects. In wlroots compositors, small ordering changes can create visible client glitches, stale compositor state, or crashes.
- If intentionally diverging from dwl, document why in code and cover the divergence with a focused test.
- If you spot an existing discrepancy from dwl while working in related code, report it to the user even if it is outside the requested change. Do not fix unrelated discrepancies unless asked.
- Avoid Python-side mutation of wlroots-owned state fields when dwl relies on wlroots events/state.

## Bindings

- `pywl_*` C helpers are plumbing only — static-inline wrappers, alloc/free for opaque-sized structs, accessors for anonymous struct members. For regular named struct fields, declare the struct in the cdef and access from Python directly. Logic stays in Python.
- All listeners share one `extern "Python"` trampoline; `listen` routes by listener address.

## Testing

Run with `pytest tests.py`.

Name tests `test_<system>_<scenario>`, where `<system>` is 1-2 words for the subsystem under test and `<scenario>` is 1-2 words for the specific case.

## Linting

Run `pylint wl.py tests.py` and address what it flags.

## Docstrings

- Write for someone unfamiliar with Wayland/wlroots: prefer "window", "screen", "app" over "toplevel", "output", "client".
- Focus on *why* and non-obvious semantics. Don't restate the field list of a dataclass or the signature of a function.
- If you feel the urge to document a field, put it as an inline comment on the field itself, not in the class docstring.
