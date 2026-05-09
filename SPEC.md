# SPEC: A more immutable interface on top of wlroots

Status: proposal / sketch. Not implemented. See `main.py` for current state.

## Problem

`Context`, `View`, and `LayerView` in `main.py` mix three different kinds of
mutation:

1. **Domain state** — `views` (order = tiling), `focused_view`,
   `fullscreen_view`, `output_usable`, `View.box`, `LayerView.mapped`,
   `Context.running`. Conceptually answers "what should be on screen?".
2. **wlroots handles** — `XdgSurface`, `SceneTree`, `SceneRect`, `Output`,
   `Cursor`, `Seat`, …. Identity-bearing; pywlroots mutates them via setters.
3. **GC anchors** — `keyboards`, `listeners`, `outputs`. They exist only to
   keep Python refs alive (see the comment in `on_new_output`).

Only (1) is a good candidate for immutability. (2) and (3) are forced by the
C library and shouldn't be fought.

## Proposed shape: pure core / impure shell

```
┌──────────── pure core ────────────┐    ┌──────── impure shell ────────┐
│ World (frozen)                     │    │ Resources                    │
│   views:    tuple[ViewModel, ...]  │    │   xdg:    dict[ViewId, …]   │
│   focused:  ViewId | None          │    │   trees:  dict[ViewId, …]   │
│   fullscreen: ViewId | None        │    │   borders, layer scenes,    │
│   outputs:  tuple[OutputModel,...] │    │   listeners, keyboards…     │
│   usable:   Map[OutputId, Box]     │    │                              │
│                                    │    │ + apply(prev, next, res)    │
│ reducers: World -> World           │    │   diffs world states and    │
│   on_mapped, on_unmapped,          │    │   issues set_position /     │
│   on_focus, on_toggle_fullscreen,  │    │   set_size / set_enabled /  │
│   on_output_added, on_layer_arr…   │    │   set_activated /           │
│                                    │    │   raise_to_top / border     │
│ layout: World -> tuple[Placement]  │    │   colour / etc.             │
└────────────────────────────────────┘    └──────────────────────────────┘
```

- `ViewModel = (id, app_id, last_box)`, no wlroots refs. Same for
  `OutputModel`, `LayerViewModel`.
- `ViewId` is `id(xdg_surface)` or a monotonic int; `Resources` is the only
  thing that translates id ↔ handle.
- Event handlers collapse to:
  ```
  delta = ...
  new = reduce(world, delta)
  apply(world, new, res)
  world = new
  ```
- `apply_tiling` becomes a pure
  `layout(world, output_id) -> tuple[(ViewId, Box), ...]`; the shell turns
  those into `set_position`/`set_size` calls, and only when the box actually
  changed.

## Wins

- Layout, focus-direction selection, chord matching, fullscreen toggling,
  and layer arrange become testable **without a backend** — they're
  `World -> World` (or `World -> data`).
- `apply` is the single place wlroots is poked. "Where do side effects live?"
  has one answer.
- Diffing naturally minimises configures (wlroots prefers we only commit on
  real changes anyway).
- Matches the file's existing lean: top-level functions, dataclasses for
  state, helpers like `view_center` and `is_exit_chord` already pure.

## Honest costs

- **Indirection**: every callback gains an id lookup into `Resources`.
- **No memory win**: same strong refs, just behind a dict.
- **Real complexity in `apply`**: must handle creation/destruction of scene
  nodes, not only attribute updates. Today's code conflates "create scene
  tree" with "place tile"; splitting them is a real refactor.
- **Over-engineering risk** at ~850 LOC. A frozen `World` with a mutable
  `Resources` is genuinely simpler than today's mixed `Context`. A full
  Elm-style update/view loop probably isn't.

## Smallest useful slice (recommended starting point)

1. Extract a pure
   `compute_layout(views_box_data, usable_box, fullscreen_id) -> tuple[(ViewId, Box, Role), ...]`
   from `apply_tiling`. Keep current `View`/`Context`. Tests don't need
   pywlroots.
2. Move `View.box` and `Context.{focused_view, fullscreen_view, views}`
   updates through a small reducer module. Side-effecting setters move into
   one `apply_layout(views, placements)` function.
3. Only then consider freezing `View` and introducing the `Resources` split —
   by that point we'll know whether the indirection pays for itself.

## Decisions

- **Scope**: full core/shell split.
- **Single file**: keep `main.py` monolithic.
- **Tests**: introduce pytest, in `tests.py`.
- **Immutability mechanism**: `dataclass(frozen=True)` plus tuples; no new
  dependency.
