# Tier 0 / Tier 1 parity issues

Each entry below documents a place where `wl.py` diverges from `dwl.c` (`/data/temp/github/codeberg.dwl-dwl/dwl.c`). All Tier 0 and Tier 1 checkboxes in `PLAN.md` are marked `[DONE]`, but the items below were either skipped, implemented differently, or behave wrong.

Format: **dwl symbol → pywl symbol** · *file:line in dwl.c* · description · suggested fix. "PLAN #N" cross-references the plan's numbered item.

---

## High priority

### 1. `printstatus` output format mismatch (PLAN #26)
- **dwl** `printstatus` (dwl.c:2090): 7 lines per monitor — `<out> title …`, `appid …`, `fullscreen 0|1`, `floating 0|1`, `selmon 0|1`, `tags <occ> <selected> <selclient> <urg>`, `layout <symbol>`.
- **pywl** `print_status` (wl.py:~2540): 1 line, `* mon=… tags=0x… layout='…' appid='…' title='…'`.
- **Effect:** no dwl-aware bar (someblocks, etc.) can parse pywl output. Missing fields: occupied-tag mask, selected-client tags, urgent-tag mask, fullscreen flag, floating flag.
- **Fix:** rewrite `print_status` to emit dwl's exact line shape. Compute `occ` and `urg` by iterating `server.clients` per monitor.

### 2. `applybounds` missing (PLAN #23)
- **dwl** `applybounds` (dwl.c:461) clamps `c->geom` to `bbox` (= `sgeom` if `interact`, else `c->mon->w`) and enforces min size `1 + 2*bw`. Called from `resize` (dwl.c:2217).
- **pywl** `resize` (wl.py:~1390) never clamps. The `interactive` kwarg is ignored.
- **Effect:** interactive drags can shove a window fully off-screen with no recovery; degenerate (0,0,0,0) layouts aren't caught.
- **Fix:** add `_apply_bounds(client, geometry, interactive)` returning the clamped tuple, call it at the top of `resize`. Use `server`-level total layout box (cache from `update_monitors`) as the `interactive=True` bbox.

### 3. `client_set_bounds` not refreshed in `resize` (PLAN #23)
- **dwl** `resize` calls `client_set_bounds(c, geo.w, geo.h)` (client.h:108) on every resize, sending `wlr_xdg_toplevel_set_bounds`.
- **pywl** sets bounds only once at initial commit (`on_xdg_toplevel_commit`, wl.py:~1302).
- **Effect:** GTK4 and similar toolkits keep stale max-size hints after any layout/monitor change.
- **Fix:** call `wlr_xdg_toplevel_set_bounds(xdg, w, h)` from `resize`, guarded by a stored `client.bounds` tuple so it's a no-op when unchanged (dwl does this in `client_set_bounds`).

### 4. Per-monitor `fullscreen_bg` rect missing (PLAN #22)
- **dwl** `Monitor.fullscreen_bg` (dwl.c:191), created in `createmon` (dwl.c:1104) under `layers[LyrFS]`, repositioned/resized in `updatemons` (dwl.c:2895), enabled in `arrange` when `focustop(m)->isfullscreen` (dwl.c:522).
- **pywl** has no per-monitor backing rect.
- **Effect:** non-opaque fullscreen surfaces (transparent video players, OBS preview) leak whatever was behind them.
- **Fix:** add `Monitor.fullscreen_bg: object` field, create in `on_new_output` under `server.layers[Layer.FULLSCREEN]`, manage from `arrange` and `update_monitors`. Color: `config.fullscreen_color` (already exists).

### 5. Key-repeat for compositor bindings missing (PLAN #8, #18)
- **dwl** `createkeyboardgroup` (dwl.c:952) creates `group->key_repeat_source` via `wl_event_loop_add_timer`. `keypress` (dwl.c:1630) stashes `mods/keysyms/nsyms` and arms the timer at `repeat_delay`. `keyrepeat` (dwl.c:1690) re-fires the binding at `1000/rate` ms.
- **pywl** `on_keyboard_key` (wl.py:~2138) has no repeat state and no timer.
- **Effect:** bound keys (e.g. `MOD+h` to shrink master) don't repeat when held.
- **Fix:** add a per-server `key_repeat` state (mods, syms list, timer-source). Wire timer via `lib.wl_event_loop_add_timer(server.loop, …)`; bindings.py already exposes `wl_event_source_timer_update` (verify, add cdef if needed). Use a Python trampoline through `ffi.callback`.

### 6. Single-keysym + case-sensitive binding dispatch (PLAN #18)
- **dwl** `keybinding` (dwl.c:~1615) compares `xkb_keysym_to_lower(sym) == xkb_keysym_to_lower(k->keysym)` and iterates **all** keysyms `xkb_state_key_get_syms` returns.
- **pywl** `dispatch_key` (wl.py:~2185) compares raw, single keysym (via the `pywl_keyboard_keysym` helper).
- **Effect:** `MOD+Shift+0` → `tag TAG_ALL` won't fire because Shift+0 yields `parenright`, not `0`. This is why pywl's defaults use digit-keysyms-with-Shift everywhere; the underlying mechanism is still broken.
- **Fix:** add a bindings helper that returns a list of keysyms for a keycode (wraps `xkb_state_key_get_syms`). Iterate all of them and fold with `xkb_keysym_to_lower`.

### 7. `focusclient` missing four behaviors (PLAN #17)
Compare `focus_client` (wl.py:~1714) against `focusclient` (dwl.c:1404):
- **No popup cleanup.** dwl destroys all `old_c->surface.xdg->popups` before swapping focus. pywl leaves popups on the old window.
- **No `exclusive_focus`/`seat->drag` guard.** dwl skips the border-color and old-deactivation paths when either is set. pywl always repaints/deactivates.
- **No cursor refresh.** dwl calls `motionnotify(0, NULL, 0,0,0,0)` after the focus change; pywl doesn't.
- **No "old layer surface still visible" early return.** dwl returns early when `old` is a TOP/OVERLAY layer surface still on-screen.
- **Fix:** mirror dwl's structure line for line. Reuse `_client_from_surface` and a new `_layer_from_surface` for the type detection. Call `process_cursor_motion(server, 0)` for the cursor refresh.

### 8. `buttonpress` divergences (PLAN #19)
`on_cursor_button` (wl.py:~2228) vs `buttonpress` (dwl.c:623):
- pywl never sets `cursor_mode = CursorMode.PRESSED` on press.
- pywl never updates `selected_monitor` on press (dwl: `selmon = xytomon(cursor->x, cursor->y)`).
- pywl runs button bindings **before** click-to-focus; dwl focuses first, then runs the binding. With dwl's order, `MOD+drag` focuses the underlying window in the same gesture.
- On release of a grab, dwl re-homes the dragged window with `setmon(grabc, xytomon(cursor->x, cursor->y), 0)`. pywl's `end_grab` doesn't touch `client.monitor`, so cross-monitor drags leave the window's monitor stale.
- **Fix:** reorder to press → set CursorMode.PRESSED → update selmon → focus → bindings. In `end_grab`, look up `monitor_at(server, x, y)` and call `set_monitor(server, grab.client, …, grab.client.tags)` before clearing grab state.

### 9. `on_new_output` rule application is broken (PLAN #20)
`on_new_output` (wl.py:~700) vs `createmon` (dwl.c:1040):
- Both branches of the `rule.x >= 0 and rule.y >= 0` test call `wlr_output_layout_add_auto`. Should be `wlr_output_layout_add(..., rule.x, rule.y)` in the positioned branch.
- `rule.scale` and `rule.rotation` are never applied (`wlr_output_state_set_scale` / `_set_transform` missing on the initial commit).
- `monitor.layouts[1]` is left as the default `1` regardless of `rule.layout_index`. dwl does `m->lt[1] = &layouts[LENGTH(layouts) > 1 && r->lt != &layouts[1]]` so the secondary slot is always different from the primary; pywl can end up with both slots equal and `MOD+space` becomes a no-op.
- **Fix:** apply scale/transform on the initial output state, fix the layout-add branch, and seed `layouts[1]` to be the first index in `range(len(config.layouts))` not equal to `rule.layout_index`.

### 10. `update_monitors` incomplete (PLAN #20)
`update_monitors` (wl.py:~860) vs `updatemons` (dwl.c:2842):
- Doesn't update `root_bg` position/size from the layout box.
- Doesn't reposition/resize the per-monitor `fullscreen_bg` (because it doesn't exist — see #4).
- After arrange, doesn't re-call `resize(c, m.m, 0)` for a focused fullscreen client.
- No gamma-LUT refresh flag (dwl: `m->gamma_lut_changed = 1`).
- No trailing `wlr_cursor_move(cursor, NULL, 0, 0)` to repaint the cursor image after monitor changes.
- Orphan re-homing runs only when iterating the selected monitor; dwl does it as a dedicated `setmon(c, selmon, c->tags)` pass after the monitor loop.
- **Fix:** restructure to match `updatemons` step-by-step. Add a `Monitor.gamma_lut_changed` field for #33 to consume.

---

## Medium priority

### 11. `closemon` skips floating shift
- **dwl** `closemon` (dwl.c:793) shifts floating clients left by `m->w.width` when their x is past the removed monitor's width.
- **pywl** `close_monitor` (wl.py:~810) doesn't.
- **Fix:** add the shift loop before the per-client `set_monitor`.

### 12. `cleanupmon` doesn't tear down layer surfaces / scene output
- **dwl** `cleanupmon` (dwl.c:728) iterates `m->layers[i]` calling `wlr_layer_surface_v1_destroy` on each, then `wlr_scene_output_destroy(m->scene_output)`, then `wlr_scene_node_destroy(&m->fullscreen_bg->node)`.
- **pywl** `cleanup_monitor` (wl.py:~793) does none of these.
- **Effect:** hot-unplugging an output leaks layer surfaces and the scene output.
- **Fix:** iterate `monitor.layer_surfaces` calling `wlr_layer_surface_v1_destroy(layer.wlr)`; then destroy `monitor.scene_output` and the fullscreen_bg (once #4 lands).

### 13. `setmon` parity gaps
`set_monitor` (wl.py:~824) vs `setmon` (dwl.c:2402):
- Doesn't save `client.prev_geometry = client.geometry` on monitor change.
- Doesn't call `resize(c, c.geom, False)` to ensure overlap with the new monitor.
- Doesn't call `set_floating(client, client.floating)` to re-run the layer reparent (relevant when switching between tile and floating layouts across monitors).
- Doesn't call `focus_client(top_client(server, selmon), lift=True)` at the end.
- **Fix:** mirror dwl's sequence exactly. Be careful with the `set_fullscreen` recursion in `cleanup_monitor`/`update_monitors` paths so you don't double-focus.

### 14. `setfloating` unconditional reparent (PLAN #22)
- **dwl** `setfloating` (dwl.c:2335) returns early when the current layout has no arrange function (`!m->lt[m->sellt]->arrange`), keeping floating clients under `LyrTile` so real floaters don't sit always-on-top. Also handles parent-fullscreen (parent fullscreen → child goes to LyrFS).
- **pywl** `set_floating` (wl.py:~1880) always reparents to `Layer.FLOAT`.
- **Fix:** check `LAYOUTS[monitor.layout_name] is layout_floating` before reparenting; pick `Layer.FS` if parent client is fullscreen.

### 15. `arrange` missing dwl wrap-up + floating-layout reparent (PLAN #14)
- **dwl** `arrange` (dwl.c:507): reparents non-fullscreen, non-FS-tree clients between `LyrTile` and `LyrFloat` depending on whether the active layout has an `arrange` function and whether the client is floating; ends with `motionnotify(0, NULL, 0,0,0,0)` and `checkidleinhibitor(NULL)`.
- **pywl** `arrange` (wl.py:~1335) has the idle check but no reparent pass and no cursor refresh.
- **Fix:** add the reparent pass mirroring dwl's expression; call `process_cursor_motion(server, 0)` at the end.

### 16. `unmapnotify` ordering
`on_xdg_toplevel_unmap` (wl.py:~1359) vs `unmapnotify` (dwl.c:1789):
- dwl: `wl_list_remove(c link); setmon(c, NULL, 0); wl_list_remove(c flink); wlr_scene_node_destroy(c scene); printstatus(); motionnotify(0,…)`. The `setmon` already arranges old monitor and refocuses.
- pywl: arrange → destroy scene → focus_client. Focus handoff configure can go out after the unmapping client's scene has been torn down; no cursor refresh.
- **Fix:** route through `set_monitor(client, None, 0)` and rely on it for arrange+focus. Then destroy scene, print_status, process_cursor_motion(0).

### 17. Tag/layout action ordering
- **dwl** consistently: mutate state → `focusclient(focustop(selmon), 1)` → `arrange(selmon)` → `printstatus()`. See `view` (dwl.c:2972), `toggleview` (dwl.c:2780), `tag` (dwl.c:2718), `toggletag` (dwl.c:2766), `zoom` (dwl.c:3048), `setlayout` (dwl.c:2373).
- **pywl** consistently: `arrange(…)` → `focus_client(…)` (which prints).
- **Effect:** activate + size configures are dispatched in the wrong order; clients can paint a transient inconsistent state.
- **Fix:** swap the order in every action listed above.

### 18. `applyrules` divergences (PLAN #21)
`_rule_for` + `on_xdg_toplevel_map` (wl.py:~1488) vs `applyrules`
(dwl.c:478):
- dwl iterates **all** rules, OR-ing `newtags |= r->tags`, with last-match wins for `isfloating` and `monitor`. pywl returns the first match and replaces.
- dwl assigns `c->isfloating = r->isfloating` (can clear). pywl only sets to True.
- dwl applies `c->isfloating |= client_is_float_type(c)` (parent- dialog) **after** rules. pywl checks parent **before** rules.
- dwl ends with `setmon(c, mon, newtags)`. pywl mutates fields and arranges later in map.
- **Fix:** rewrite `_apply_rules(server, client)` as a side-effecting helper that mirrors dwl, returning nothing. Call from `mapnotify` in place of the current rule block.

### 19. `commitnotify` initial-commit doesn't pre-resolve monitor
- **dwl** `commitnotify` initial-commit (dwl.c:880): calls `applyrules(c)` then `setmon(c, NULL, 0)` so the scale call uses the rule-resolved monitor.
- **pywl** uses `server.selected_monitor` for the `set_bounds` call — wrong screen on multi-monitor + rule.
- Also: dwl's non-initial commit calls `resize(c, c.geom, c.isfloating && !c.isfullscreen)` (interactive flag); pywl always passes False.
- **Fix:** in `on_xdg_toplevel_commit` initial path, run `_apply_rules` against a temporary monitor binding, use its `w` for bounds, then clear `client.monitor` so map-time rule application still happens. Pass interactive flag in the non-initial branch.

### 20. `focusstack` doesn't honor fullscreen
- **dwl** `focusstack` (dwl.c:1483) early-returns if `sel->isfullscreen && !client_has_children(sel)`.
- **pywl** `action_focus_stack` (wl.py:~1845) cycles regardless.
- **Fix:** add the guard. `client_has_children` ≈ any other client whose xdg parent is `sel`'s toplevel.

### 21. `setmfact` clamp + arrange-check
- **dwl** `setmfact` (dwl.c:2388): no-op if current layout has no arrange function; clamp `0.1 < f < 0.9`.
- **pywl** `action_set_master_factor` (wl.py:~2018): clamps `0.05 ≤ f ≤ 0.95`, runs in any layout.
- **Fix:** match dwl clamp; early-return when in floating layout.

### 22. `process_cursor_motion` updates selmon without sloppy_focus
- **dwl** `motionnotify` (dwl.c:1916) updates `selmon` only when `sloppyfocus`.
- **pywl** `process_cursor_motion` (wl.py:~2208) always updates.
- **Effect:** monitor selection follows the cursor even when sloppy focus is off — visible via `print_status`.
- **Fix:** gate `server.selected_monitor` update on `config.sloppy_focus`.

### 23. `on_request_set_primary_selection` is a no-op (PLAN #7)
- **dwl** `setpsel` (similar to `setsel`) calls `wlr_seat_set_primary_selection(seat, ev->source, ev->serial)`.
- **pywl** (wl.py:~2598) has only a docstring.
- **Fix:** call `wlr_seat_set_primary_selection`. Safe to do before Tier 2 #34 wires the protocol global — it just won't have clients yet.

---

## Lower priority

### 24. `zoom` insertion position / layout check (PLAN #15)
- **dwl** `zoom` (dwl.c:3048): returns early if current layout has no arrange function; inserts the promoted client at the **head** of `clients` (`wl_list_insert(&clients, &sel->link)`).
- **pywl** `action_zoom` (wl.py:~1944): no layout check; inserts at the position of the first tiled client (not necessarily head).
- **Fix:** add layout guard; `server.clients.remove(client); server.clients.insert(0, client)`.

### 25. `cleanup` destroys scene before display
- **dwl** `cleanup` (dwl.c:701) destroys scene **after** `wl_display_destroy(dpy)` ("to avoid destroying them with an invalid scene output").
- **pywl** `cleanup` (wl.py:~600) destroys scene before.
- **Fix:** move the `wlr_scene_node_destroy` after `wl_display_destroy`. Also: dwl doesn't explicitly destroy renderer or allocator (they're owned by display/backend) — pywl does. Verify this isn't a double-free with current wlroots.

### 26. Scene tree created at create-notify, not at map-notify
- **dwl** `createnotify` (dwl.c:1121) only allocates Client + listeners and sets `c->bw`. Scene tree, scene surface, borders, listmembership all happen in `mapnotify`.
- **pywl** `on_new_xdg_toplevel` (wl.py:~1217) creates everything up front, including 4 border rects parented under the surface.
- **Effects:**
  - Clients destroyed before map leak scene resources (`destroy` listener doesn't tear them down — only `unmap` does).
  - Borders are placed below the scene surface via `wlr_scene_node_place_below`; dwl leaves them above and relies on the surface-tree clip.
  - The tree is never `set_enabled(False)` pre-map (dwl does `set_enabled(client_is_unmanaged(c))` after creation).
- **Fix:** push scene creation into `on_xdg_toplevel_map`, mirroring dwl. Destroy-before-map then becomes a no-op for scene state.

### 27. `update_title` predicate
- **dwl** `updatetitle` (dwl.c:2948): `c == focustop(c->mon)`.
- **pywl** `on_xdg_toplevel_set_title` (wl.py:~1437): global fstack head.
- **Effect:** title change for a per-monitor-focused but not globally selected client doesn't trigger `print_status` in pywl.
- **Fix:** compare against `top_client(server, client.monitor)`.

### 28. `_initial_float_geometry` divergence
- **dwl** uses the client's own `geometry` from `client_get_geometry(c, &c->geom)` plus `2*bw`.
- **pywl** (wl.py:~1567) always centers at half the monitor size.
- **Fix:** read `xdg_toplevel.base.geometry` for the size; only fall
  back to a default if it's 0×0.

### 29. `setup` global ordering
- **dwl** creates scene **before** renderer/allocator/compositor.
- **pywl** creates them after (wl.py:~530).
- No known visible effect, but AGENTS says preserve dwl ordering.
- **Fix:** reorder.

### 30. `XCURSOR_SIZE` env var
- **dwl** `setup` (dwl.c:2603): `setenv("XCURSOR_SIZE", "24", 1)`.
- **pywl**: not set.
- **Effect:** client-drawn cursors (some toolkits) read this and default to a different size.
- **Fix:** `os.environ.setdefault("XCURSOR_SIZE", str(config.cursor_size))` in `main`.

### 31. `_default_config` defaults don't match `config.def.h`
PLAN says config "mirrors" `config.def.h`. Deviations:
- `border_width=3` vs dwl `borderpx=1`.
- `sloppy_focus=False` vs dwl `sloppyfocus=1` (latent — feature is Tier 3 #40, but the default should still match).
- `MOD+Return` → `spawn` in pywl, but `zoom` in dwl. dwl's `MOD+Shift+Return` → spawn term.
- `MOD+f` → `toggle_fullscreen` in pywl, but `set_layout` (floating) in dwl. dwl's `MOD+e` is fullscreen.
- Tag-key bindings don't include the shifted keysyms (`exclam`, `at`, …) the way dwl's `TAGKEYS` macro does. Loosely related to #6 — once #6 lands, decide whether to add both.
- No `MOD+p` → menu binding.
- **Fix:** rewrite `_default_config` keybinding list to mirror dwl's, using the dwl key names. Document any intentional deviation in a comment.

---

## Notes / non-issues

These I checked and they match dwl:
- 8-layer enum ordering (BG/Bottom/Tile/Float/Top/FS/Overlay/Block). Note PLAN.md item #4's prose text has them in the wrong order ("Fullscreen, Top, Overlay") — the code is right; the PLAN text is wrong and should be fixed.
- `_LAYERMAP` = `(BG, Bottom, Top, Overlay)` matches dwl's `layermap[]`.
- `drag_icon` placed below `Layer.BLOCK`.
- `arrange_layers` two-pass (exclusive then non-exclusive) + topmost keyboard-interactive scan order (overlay first, then top).
- `on_request_activate` (urgency) matches dwl `urgent`.
- Session-lock new-lock/new-surface/unlock/destroy lifecycle.
- Idle-inhibit visibility check including `idle_inhibit_ignore_visibility` (= dwl `bypass_surface_visibility`).
- xdg-decoration always-SSD policy.
- `wlr_output_management_v1` apply/test loop matches dwl `outputmgrapplyortest`.
- `set_fullscreen` save/restore of `prev_geometry`.
