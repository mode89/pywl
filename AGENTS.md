# AGENTS.md

A minimal Wayland compositor written in Python on top of `pywlroots`.

## Layout

- `main.py` — single-file compositor implementation.
- `NOTES.md` — log of findings (API quirks, version-specific behavior, workarounds).

## Running

```
python main.py
```

Inside a running session (e.g. Hyprland), the wl backend is auto-selected and a nested window appears. The socket is printed as `WAYLAND_DISPLAY=wayland-N`.

Test with a client:
```
WAYLAND_DISPLAY=wayland-N foot
```

## Testing a change

The compositor blocks in an event loop. To validate a change end-to-end:

1. Launch under `timeout` so a hang doesn't wedge the agent:
   ```
   timeout 5 python main.py &
   PID=$!
   ```
2. Grab `WAYLAND_DISPLAY` from stdout, run a client (`foot`, `weston-terminal`) against it, observe.
3. Kill with `kill -INT $PID` and verify `kill -0 $PID` reports the process gone before trusting the exit status.

See `NOTES.md` → "Testing discipline" for why.
