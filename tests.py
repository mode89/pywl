"""Tests for pywl."""

# pylint: disable=too-many-lines

from __future__ import annotations

from types import SimpleNamespace as SN
from unittest.mock import MagicMock

import pytest

import wl


@pytest.fixture(autouse=True)
def _patch_bindings(monkeypatch):
    """Replace the cffi globals with mocks so tests run without wlroots."""
    lib = MagicMock()
    lib.pywl_seat_keyboard_focused_surface = MagicMock(return_value=None)
    lib.wlr_xdg_toplevel_set_size.return_value = 42

    monkeypatch.setattr(wl, "ffi", SN(
        NULL=object(),
        addressof=lambda *_a, **_k: None,
        cast=lambda _t, x: x,
        string=lambda _s: b"",
        new=lambda *_a, **_k: MagicMock(),
        new_handle=lambda x: x,
        from_handle=lambda x: x,
    ))
    monkeypatch.setattr(wl, "lib", lib)
    monkeypatch.setattr(
        wl, "listen", lambda _s, _c: MagicMock(remove=lambda: None))
    monkeypatch.setattr(wl, "config", _fake_config)


_fake_config = SN(
    border_width=3,
    border_color=(0.5, 0.5, 0.5, 1.0),
    focus_color=(1.0, 0.5, 0.0, 1.0),
    urgent_color=(0.8, 0.0, 0.0, 1.0),
    layouts=[
        SN(name="tile", symbol="[]="),
        SN(name="floating", symbol="><>"),
        SN(name="monocle", symbol="[M]"),
    ],
    rules=[],
    idle_inhibit_ignore_visibility=False,
)


def make_monitor(w: int = 1000, h: int = 800, tags_mask: int = 1) -> wl.Monitor:
    """Build a `Monitor` with a fixed size and one selected workspace."""
    m = wl.Monitor(
        wlr_output=MagicMock(), scene_output=MagicMock(), name="test")
    m.m = (0, 0, w, h)
    m.w = (0, 0, w, h)
    m.tags = [tags_mask, tags_mask]
    m.seltags = 0
    m.master_factor = 0.55
    m.num_master = 1
    return m


def make_client(
    monitor: wl.Monitor,
    tags: int = 1,
    floating: bool = False,
    fullscreen: bool = False,
    mapped: bool = True,
) -> wl.Client:
    """Build a mapped xdg-toplevel-backed `Client` on `monitor`."""
    base = MagicMock()
    base.initialized = True
    base.geometry.x = 0
    base.geometry.y = 0
    xdg_toplevel = MagicMock(base=base)
    xdg_toplevel.parent = wl.ffi.NULL
    return wl.Client(
        client_type=wl.ClientType.XDG_SHELL,
        xdg_toplevel=xdg_toplevel,
        surface=MagicMock(),
        scene_tree=MagicMock(),
        scene_surface=MagicMock(),
        border_rects=[MagicMock() for _ in range(4)],
        border_width=_fake_config.border_width,
        monitor=monitor,
        tags=tags,
        floating=floating,
        fullscreen=fullscreen,
        mapped=mapped,
    )


def make_server(monitor: wl.Monitor) -> wl.Server:
    """Build a `Server` with `monitor` as its single (and selected) screen."""
    s = wl.Server(
        display=MagicMock(), loop=MagicMock(), backend=MagicMock(),
        renderer=MagicMock(), allocator=MagicMock(), compositor=MagicMock(),
        output_layout=MagicMock(), scene=MagicMock(), scene_layout=MagicMock(),
        layers={layer: MagicMock() for layer in wl.Layer},
        root_bg=MagicMock(), locked_bg=MagicMock(), drag_icon=MagicMock(),
        xdg_shell=MagicMock(), output_mgr=MagicMock(),
        output_power_mgr=MagicMock(),
        session_lock_mgr=MagicMock(),
        idle_notifier=MagicMock(), idle_inhibit_mgr=MagicMock(),
        seat=MagicMock(), cursor=MagicMock(),
        xcursor_mgr=MagicMock(), keyboard_group=MagicMock(),
    )
    s.monitors.append(monitor)
    s.selected_monitor = monitor
    return s


# --- Tile layout ------------------------------------------------------------

def test_tile_single():
    """One window fills the whole screen — no stack column appears."""
    m = make_monitor(1000, 800)
    s = make_server(m)
    c = make_client(m)
    s.clients.append(c)

    wl.arrange(s, m)

    assert c.geometry == (0, 0, 1000, 800)


def test_tile_two():
    """Two windows split into master (0.55 of width) + single stack tile."""
    m = make_monitor(1000, 800)
    s = make_server(m)
    a, b = make_client(m), make_client(m)
    s.clients.extend([a, b])

    wl.arrange(s, m)

    # master_factor = 0.55, n_master = 1
    assert a.geometry == (0, 0, 550, 800)
    assert b.geometry == (550, 0, 450, 800)


def test_tile_nmaster2():
    """With two master slots the master column splits vertically."""
    m = make_monitor(1000, 800)
    m.num_master = 2
    s = make_server(m)
    a, b, c = make_client(m), make_client(m), make_client(m)
    s.clients.extend([a, b, c])

    wl.arrange(s, m)

    assert a.geometry == (0, 0, 550, 400)
    assert b.geometry == (0, 400, 550, 400)
    assert c.geometry == (550, 0, 450, 800)


def test_tile_nmaster0():
    """Setting `num_master = 0` should disable the master column entirely;
    all windows stack vertically across the full width."""
    m = make_monitor(1000, 800)
    m.num_master = 0
    s = make_server(m)
    a, b = make_client(m), make_client(m)
    s.clients.extend([a, b])

    wl.arrange(s, m)

    # No master column reserved; stack fills the full monitor width.
    assert a.geometry == (0, 0, 1000, 400)
    assert b.geometry == (0, 400, 1000, 400)


def test_tile_no_stack():
    """When there are fewer windows than master slots, the master column
    expands to the full width — no empty stack column."""
    m = make_monitor(1000, 800)
    m.num_master = 3
    s = make_server(m)
    a, b = make_client(m), make_client(m)
    s.clients.extend([a, b])

    wl.arrange(s, m)

    # n=2 <= nmaster=3 → master column takes full width.
    assert a.geometry == (0, 0, 1000, 400)
    assert b.geometry == (0, 400, 1000, 400)


# --- Zoom -------------------------------------------------------------------

def test_zoom_promote():
    """Zoom on a non-master window swaps it into the master slot."""
    m = make_monitor()
    s = make_server(m)
    a, b, c = make_client(m), make_client(m), make_client(m)
    s.clients.extend([a, b, c])   # a is current master
    s.fstack.insert(0, c)         # c is focused

    wl.action_zoom(s, None)

    assert s.clients[0] is c
    assert s.clients[1] is a


def test_zoom_swap():
    """Zoom on the master pulls the next tiled window up instead, so
    pressing zoom repeatedly cycles which window is the master."""
    m = make_monitor()
    s = make_server(m)
    a, b, c = make_client(m), make_client(m), make_client(m)
    s.clients.extend([a, b, c])
    s.fstack.insert(0, a)         # a is master AND focused

    wl.action_zoom(s, None)

    # Zoom on master pulls tiled[1] up instead.
    assert s.clients[0] is b
    assert s.clients[1] is a


def test_zoom_noop():
    """Zoom with only one window does nothing (and doesn't crash)."""
    m = make_monitor()
    s = make_server(m)
    a = make_client(m)
    s.clients.append(a)
    s.fstack.insert(0, a)

    wl.action_zoom(s, None)

    assert s.clients == [a]


# --- Tag math ---------------------------------------------------------------

def test_view_switch():
    """Switching to a different workspace flips to the second tag slot,
    so the original selection is remembered for `view_previous`."""
    m = make_monitor(tags_mask=1)
    s = make_server(m)

    wl.action_view(s, 2)

    assert m.seltags == 1
    assert m.selected_tags == 2


def test_view_previous():
    """Calling `view` with mask 0 swaps back to the previous workspace."""
    m = make_monitor(tags_mask=1)
    s = make_server(m)
    wl.action_view(s, 2)          # tags = [1, 2], seltags = 1

    wl.action_view(s, 0)          # 0 means swap slots

    assert m.seltags == 0
    assert m.selected_tags == 1


def test_toggle_view_combine():
    """`toggle_view` adds a workspace to the current selection rather than
    replacing it (lets the user view multiple workspaces at once)."""
    m = make_monitor(tags_mask=1)
    s = make_server(m)

    wl.action_toggle_view(s, 2)

    assert m.selected_tags == 3   # 1 | 2


def test_toggle_view_reject():
    """Toggling off the only visible workspace is refused — we never
    leave the user with nothing showing."""
    m = make_monitor(tags_mask=1)
    s = make_server(m)

    wl.action_toggle_view(s, 1)   # would XOR to 0

    assert m.selected_tags == 1


def test_tag_move():
    """`tag` moves the focused window to the named workspace."""
    m = make_monitor(tags_mask=1)
    s = make_server(m)
    c = make_client(m, tags=1)
    s.clients.append(c)
    s.fstack.insert(0, c)

    wl.action_tag(s, 4)

    assert c.tags == 4


# --- num_master clamp -------------------------------------------------------

def test_nmaster_clamp():
    """`num_master` cannot go below 0 — negative master counts are
    meaningless and would break the tile arithmetic."""
    m = make_monitor()
    m.num_master = 1
    s = make_server(m)

    wl.action_inc_num_master(s, -5)

    assert m.num_master == 0


def test_nmaster_increment():
    """Positive deltas add master slots."""
    m = make_monitor()
    m.num_master = 1
    s = make_server(m)

    wl.action_inc_num_master(s, 2)

    assert m.num_master == 3


# --- Fullscreen border ------------------------------------------------------

def test_fullscreen_border():
    """Going fullscreen hides the border (so the window really fills the
    screen edge-to-edge)."""
    m = make_monitor()
    s = make_server(m)
    c = make_client(m)
    s.clients.append(c)
    s.fstack.insert(0, c)

    wl.set_fullscreen(s, c, True)

    assert c.fullscreen is True
    assert c.border_width == 0


def test_unfullscreen_border():
    """Leaving fullscreen restores the configured border width."""
    m = make_monitor()
    s = make_server(m)
    c = make_client(m)
    s.clients.append(c)
    s.fstack.insert(0, c)

    wl.set_fullscreen(s, c, True)
    wl.set_fullscreen(s, c, False)

    assert c.fullscreen is False
    assert c.border_width == _fake_config.border_width


# --- Floating toggle --------------------------------------------------------

def test_float_preserve():
    """Making a window float keeps its current geometry; the tile layout
    no longer touches it."""
    m = make_monitor()
    s = make_server(m)
    a, b = make_client(m), make_client(m)
    s.clients.extend([a, b])
    wl.arrange(s, m)
    before = a.geometry

    wl.set_floating(s, a, True)

    # set_floating itself doesn't touch geometry; arrange() skips floats.
    assert a.geometry == before


def test_float_retile():
    """Un-floating a window puts it back into the tile layout alongside
    the other tiled windows."""
    m = make_monitor()
    s = make_server(m)
    a, b = make_client(m), make_client(m)
    s.clients.extend([a, b])
    wl.arrange(s, m)
    wl.set_floating(s, a, True)   # a floats; b takes the whole monitor

    wl.set_floating(s, a, False)  # back to tile

    assert a.geometry == (0, 0, 550, 800)
    assert b.geometry == (550, 0, 450, 800)


# --- top_client / focus -----------------------------------------------------

def test_top_client_visible():
    """`top_client` skips windows whose workspace isn't currently shown
    and returns the next most-recently-focused visible one."""
    m = make_monitor(tags_mask=1)
    s = make_server(m)
    a = make_client(m, tags=1)
    b = make_client(m, tags=2)    # invisible: different tag
    c = make_client(m, tags=1)
    s.fstack.extend([b, a, c])

    assert wl.top_client(s, m) is a


def test_top_client_unmapped():
    """`top_client` skips windows that aren't currently showing content
    (unmapped) and returns the next focusable one."""
    m = make_monitor(tags_mask=1)
    s = make_server(m)
    a = make_client(m, tags=1, mapped=False)
    b = make_client(m, tags=1)
    s.fstack.extend([a, b])

    assert wl.top_client(s, m) is b


# --- Commit re-clips / re-pins ---------------------------------------------

def test_commit_reapplies_clip(monkeypatch):
    """Non-initial commits must refresh the clip against the current xdg
    geometry, otherwise a CSD shadow inset that changes across configures
    (e.g. on fullscreen toggle) leaves the clip stale by one frame."""
    m = make_monitor(1000, 800)
    s = make_server(m)
    c = make_client(m)
    c.geometry = (0, 0, 1000, 800)
    s.clients.append(c)

    boxes = []
    monkeypatch.setattr(
        wl.ffi, "new",
        lambda _t, d=None, **_k: (boxes.append(d), MagicMock())[1])

    # Simulate a fresh commit reporting a new CSD shadow inset.
    c.xdg_toplevel.base.initial_commit = False  # pylint: disable=no-member
    c.xdg_toplevel.base.geometry.x = 10  # pylint: disable=no-member
    c.xdg_toplevel.base.geometry.y = 20  # pylint: disable=no-member

    wl.on_xdg_toplevel_commit(s, c, None)

    inner_w = 1000 - 2 * _fake_config.border_width
    inner_h = 800 - 2 * _fake_config.border_width
    assert {"x": 10, "y": 20, "width": inner_w, "height": inner_h} in boxes
    wl.lib.wlr_scene_subsurface_tree_set_clip.assert_called()


def test_commit_repins_size():
    """Non-initial commits must re-issue set_size so clients that try to
    shrink themselves (e.g. menu-bar toggle) get pinned back to the tile."""
    m = make_monitor(1000, 800)
    s = make_server(m)
    c = make_client(m)
    c.geometry = (0, 0, 1000, 800)
    s.clients.append(c)
    c.xdg_toplevel.base.initial_commit = False  # pylint: disable=no-member
    wl.lib.wlr_xdg_toplevel_set_size.reset_mock()

    wl.on_xdg_toplevel_commit(s, c, None)

    inner_w = 1000 - 2 * _fake_config.border_width
    inner_h = 800 - 2 * _fake_config.border_width
    wl.lib.wlr_xdg_toplevel_set_size.assert_called_with(
        c.xdg_toplevel, inner_w, inner_h)


def test_commit_initial():
    """Initial commit follows the setup path, not the re-resize path."""
    m = make_monitor(1000, 800)
    s = make_server(m)
    c = make_client(m)
    c.geometry = (0, 0, 1000, 800)
    s.clients.append(c)
    c.xdg_toplevel.base.initial_commit = True  # pylint: disable=no-member
    wl.lib.wlr_xdg_toplevel_set_size.reset_mock()

    wl.on_xdg_toplevel_commit(s, c, None)

    # Initial path calls set_size(0, 0) once; never with the tile dims.
    calls = wl.lib.wlr_xdg_toplevel_set_size.call_args_list
    assert all(call.args[1:] == (0, 0) for call in calls)


# --- Render gating (transactional layout) ----------------------------------

def _reset_render_mocks():
    """Clear the call history of the two render-related lib calls so each
    render-gating test starts from a known state."""
    wl.lib.wlr_scene_output_commit.reset_mock()
    wl.lib.wlr_scene_output_send_frame_done.reset_mock()


def test_render_idle():
    """When no client has a pending resize, painting proceeds normally."""
    m = make_monitor()
    s = make_server(m)
    c = make_client(m)
    c.resize_serial = 0
    s.clients.append(c)
    _reset_render_mocks()

    wl.render_monitor(s, m)

    wl.lib.wlr_scene_output_commit.assert_called_once()
    wl.lib.wlr_scene_output_send_frame_done.assert_called_once()


def test_render_pending_tile():
    """A pending tile resize stalls the frame commit (so the user sees an
    atomic layout change), but `frame_done` still fires so the client
    keeps animating."""
    m = make_monitor()
    s = make_server(m)
    c = make_client(m)
    c.resize_serial = 7
    s.clients.append(c)
    _reset_render_mocks()

    wl.render_monitor(s, m)

    wl.lib.wlr_scene_output_commit.assert_not_called()
    # Clients still need frame_done to drive their own paint loops.
    wl.lib.wlr_scene_output_send_frame_done.assert_called_once()


def test_render_pending_float():
    """Floating clients don't gate the frame; they're outside the tile
    transaction and can paint at their own pace."""
    m = make_monitor()
    s = make_server(m)
    c = make_client(m, floating=True)
    c.resize_serial = 7
    s.clients.append(c)
    _reset_render_mocks()

    wl.render_monitor(s, m)

    wl.lib.wlr_scene_output_commit.assert_called_once()


def test_render_other_monitor():
    """A pending tile on monitor B must not stall monitor A."""
    a = make_monitor(1000, 800)
    b = make_monitor(1000, 800)
    s = make_server(a)
    s.monitors.append(b)
    pending_on_b = make_client(b)
    pending_on_b.resize_serial = 7
    s.clients.append(pending_on_b)
    _reset_render_mocks()

    wl.render_monitor(s, a)

    wl.lib.wlr_scene_output_commit.assert_called_once()


def test_render_hidden_tag():
    """A pending tile on a tag the user is not viewing must not stall the
    monitor — its pixels aren't on screen this frame anyway."""
    m = make_monitor(tags_mask=1)
    s = make_server(m)
    c = make_client(m, tags=2)   # visible tags = 1, client on tag 2
    c.resize_serial = 7
    s.clients.append(c)
    _reset_render_mocks()

    wl.render_monitor(s, m)

    wl.lib.wlr_scene_output_commit.assert_called_once()


# --- resize_serial ack tracking --------------------------------------------

def test_resize_dims_match():
    """resize() must not re-issue set_size when the client already
    committed at our requested inner size; the gate would otherwise
    re-arm on every commit and the output would never paint."""
    m = make_monitor()
    s = make_server(m)
    c = make_client(m)
    c.geometry = (0, 0, 1000, 800)
    s.clients.append(c)
    bw = _fake_config.border_width
    c.xdg_toplevel.current.width = 1000 - 2 * bw  # pylint: disable=no-member
    c.xdg_toplevel.current.height = 800 - 2 * bw  # pylint: disable=no-member
    wl.lib.wlr_xdg_toplevel_set_size.reset_mock()

    wl.resize(s, c, c.geometry)

    wl.lib.wlr_xdg_toplevel_set_size.assert_not_called()
    assert c.resize_serial == 0


def test_resize_dims_differ():
    """When the client's current size doesn't match the tile, `resize`
    issues a new `set_size` and records the configure serial so the
    render gate can wait for the client to ack it."""
    m = make_monitor()
    s = make_server(m)
    c = make_client(m)
    c.geometry = (0, 0, 1000, 800)
    s.clients.append(c)
    c.xdg_toplevel.current.width = 0  # differs from any inner box  # pylint: disable=no-member
    c.xdg_toplevel.current.height = 0  # pylint: disable=no-member
    wl.lib.wlr_xdg_toplevel_set_size.return_value = 42

    wl.resize(s, c, c.geometry)

    wl.lib.wlr_xdg_toplevel_set_size.assert_called_once()
    assert c.resize_serial == 42


# --- Focus modifier replay --------------------------------------------------

def test_focus_modifiers():
    """On focus change we replay the keys already held down to the new
    window, so a key pressed before focus moves still registers."""
    m = make_monitor()
    s = make_server(m)
    c = make_client(m)
    s.clients.append(c)
    kb = MagicMock(num_keycodes=2)
    wl.lib.wlr_seat_get_keyboard = MagicMock(return_value=kb)

    wl.focus_client(s, c, lift=True)

    args = wl.lib.wlr_seat_keyboard_notify_enter.call_args.args
    # (seat, surface, keycodes, num_keycodes, modifiers)
    assert args[2] is kb.keycodes
    assert args[3] == 2


def test_focus_no_keyboard():
    """If no keyboard is attached, focus still happens but the held-key
    replay degrades to an empty list — doesn't crash."""
    m = make_monitor()
    s = make_server(m)
    c = make_client(m)
    s.clients.append(c)
    wl.lib.wlr_seat_get_keyboard = MagicMock(return_value=wl.ffi.NULL)

    wl.focus_client(s, c, lift=True)

    args = wl.lib.wlr_seat_keyboard_notify_enter.call_args.args
    assert args[2] is wl.ffi.NULL
    assert args[3] == 0
    assert args[4] is wl.ffi.NULL


# --- xdg-activation ---------------------------------------------------------

def _wire_surface_lookup(server: wl.Server) -> None:
    """Make `_client_from_surface` resolve test clients: any surface is its
    own root, and a surface maps to its client's xdg_toplevel."""
    wl.lib.wlr_surface_get_root_surface.side_effect = lambda s: s

    def to_toplevel(root):
        for client in server.clients:
            if client.surface is root:
                return client.xdg_toplevel
        return wl.ffi.NULL
    wl.lib.wlr_xdg_toplevel_try_from_wlr_surface.side_effect = to_toplevel
    wl.lib.wlr_xdg_popup_try_from_wlr_surface.return_value = wl.ffi.NULL


def test_activation_marks_urgent():
    """Request on an unfocused client flags it urgent and repaints borders."""
    m = make_monitor()
    s = make_server(m)
    a, b = make_client(m), make_client(m)
    s.clients.extend([a, b])
    s.fstack.insert(0, a)  # `a` is focused; `b` is the activation target
    _wire_surface_lookup(s)

    wl.on_request_activate(s, SN(surface=b.surface))

    assert b.urgent is True
    assert wl.lib.wlr_scene_rect_set_color.call_count == 4


def test_activation_ignores_focused():
    """A request for the already-focused window is a no-op."""
    m = make_monitor()
    s = make_server(m)
    a = make_client(m)
    s.clients.append(a)
    s.fstack.insert(0, a)
    _wire_surface_lookup(s)

    wl.on_request_activate(s, SN(surface=a.surface))

    assert a.urgent is False
    assert wl.lib.wlr_scene_rect_set_color.call_count == 0


def test_activation_unknown_surface():
    """A request for a surface we don't track is silently ignored."""
    m = make_monitor()
    s = make_server(m)
    a = make_client(m)
    s.clients.append(a)
    _wire_surface_lookup(s)

    wl.on_request_activate(s, SN(surface=object()))

    assert a.urgent is False


# --- output-management (wlr-randr, kanshi) ---------------------------------

def _quiet_arrange(monkeypatch):
    """Stub `update_monitors`' side effects so tests focus on the
    output-management interaction (head creation, set_configuration)."""
    monkeypatch.setattr(wl, "_refresh_monitor_box", lambda *_a: None)
    monkeypatch.setattr(wl, "arrange", lambda *_a: None)
    monkeypatch.setattr(wl, "arrange_layers", lambda *_a: None)
    monkeypatch.setattr(wl, "print_status", lambda *_a: None)


def _fake_head(output=None, **fields) -> object:
    """Fabricate a config-head-shaped object for the apply/test handler."""
    custom = SN(width=1920, height=1080, refresh=60_000)
    for key in ("width", "height", "refresh"):
        if key in fields:
            setattr(custom, key, fields.pop(key))
    state = SN(
        output=output if output is not None else SN(enabled=True),
        enabled=True, mode="non-null", custom_mode=custom,
        x=0, y=0, transform=0, scale=1.0, adaptive_sync_enabled=False,
    )
    for key, value in fields.items():
        setattr(state, key, value)
    return SN(state=state)


def test_update_monitors_enabled_only(monkeypatch):
    """Disabled outputs are dropped from the advertised configuration."""
    _quiet_arrange(monkeypatch)
    on = make_monitor()
    on.m = (100, 200, 800, 600)
    on.wlr_output.enabled = True
    off = make_monitor()
    off.wlr_output.enabled = False
    s = make_server(on)
    s.monitors.append(off)

    fake_config = MagicMock()
    wl.lib.wlr_output_configuration_v1_create.return_value = fake_config
    head = MagicMock()
    wl.lib.wlr_output_configuration_head_v1_create.return_value = head

    wl.update_monitors(s)

    wl.lib.wlr_output_configuration_head_v1_create.assert_called_once_with(
        fake_config, on.wlr_output)
    assert (head.state.x, head.state.y) == (100, 200)
    wl.lib.wlr_output_manager_v1_set_configuration.assert_called_once_with(
        s.output_mgr, fake_config)


def test_apply_per_head(monkeypatch):
    """A successful apply commits each head and sends `succeeded`."""
    output = SN(enabled=True)
    head = _fake_head(output=output, x=10, y=20)
    monkeypatch.setattr(wl, "_iter_config_heads", lambda _c: iter([head]))
    wl.lib.wlr_output_commit_state.return_value = True
    wl.lib.wlr_output_layout_get.return_value = wl.ffi.NULL
    s = make_server(make_monitor())

    wl.on_output_mgr_apply_or_test(s, MagicMock(), test=False)

    wl.lib.wlr_output_commit_state.assert_called_once()
    wl.lib.wlr_output_test_state.assert_not_called()
    wl.lib.wlr_output_layout_add.assert_called_once_with(
        s.output_layout, output, 10, 20)
    wl.lib.wlr_output_configuration_v1_send_succeeded.assert_called_once()
    wl.lib.wlr_output_configuration_v1_send_failed.assert_not_called()
    wl.lib.wlr_output_configuration_v1_destroy.assert_called_once()


def test_apply_test_only(monkeypatch):
    """`test=True` previews via `wlr_output_test_state`; nothing is committed
    and the layout is left untouched."""
    head = _fake_head()
    monkeypatch.setattr(wl, "_iter_config_heads", lambda _c: iter([head]))
    wl.lib.wlr_output_test_state.return_value = True
    s = make_server(make_monitor())

    wl.on_output_mgr_apply_or_test(s, MagicMock(), test=True)

    wl.lib.wlr_output_test_state.assert_called_once()
    wl.lib.wlr_output_commit_state.assert_not_called()
    wl.lib.wlr_output_layout_add.assert_not_called()
    wl.lib.wlr_output_configuration_v1_send_succeeded.assert_called_once()


def test_apply_custom_mode(monkeypatch):
    """`mode == NULL` means the client gave width/height/refresh directly."""
    head = _fake_head(
        mode=wl.ffi.NULL, width=1280, height=720, refresh=59_940)
    monkeypatch.setattr(wl, "_iter_config_heads", lambda _c: iter([head]))
    wl.lib.wlr_output_commit_state.return_value = True
    wl.lib.wlr_output_layout_get.return_value = wl.ffi.NULL
    s = make_server(make_monitor())

    wl.on_output_mgr_apply_or_test(s, MagicMock(), test=False)

    wl.lib.wlr_output_state_set_mode.assert_not_called()
    wl.lib.wlr_output_state_set_custom_mode.assert_called_once()
    _state, w, h, r = wl.lib.wlr_output_state_set_custom_mode.call_args.args
    assert (w, h, r) == (1280, 720, 59_940)


def test_apply_position_unchanged(monkeypatch):
    """Re-adding at the same position would mark the output as manually
    placed; the handler skips that redundant call."""
    output = SN(enabled=True)
    head = _fake_head(output=output, x=50, y=60)
    monkeypatch.setattr(wl, "_iter_config_heads", lambda _c: iter([head]))
    wl.lib.wlr_output_commit_state.return_value = True
    wl.lib.wlr_output_layout_get.return_value = SN(x=50, y=60)
    s = make_server(make_monitor())

    wl.on_output_mgr_apply_or_test(s, MagicMock(), test=False)

    wl.lib.wlr_output_layout_add.assert_not_called()


def test_apply_disabled_head(monkeypatch):
    """A disabled head only flips `enabled`; mode/scale/etc. aren't set."""
    head = _fake_head(enabled=False)
    monkeypatch.setattr(wl, "_iter_config_heads", lambda _c: iter([head]))
    wl.lib.wlr_output_commit_state.return_value = True
    s = make_server(make_monitor())

    wl.on_output_mgr_apply_or_test(s, MagicMock(), test=False)

    wl.lib.wlr_output_state_set_enabled.assert_called_once()
    wl.lib.wlr_output_state_set_mode.assert_not_called()
    wl.lib.wlr_output_state_set_custom_mode.assert_not_called()
    wl.lib.wlr_output_state_set_scale.assert_not_called()


def test_apply_commit_failed(monkeypatch):
    """If any head's commit fails, the client gets a single `failed`."""
    monkeypatch.setattr(
        wl, "_iter_config_heads", lambda _c: iter([_fake_head()]))
    wl.lib.wlr_output_commit_state.return_value = False
    s = make_server(make_monitor())

    wl.on_output_mgr_apply_or_test(s, MagicMock(), test=False)

    wl.lib.wlr_output_configuration_v1_send_failed.assert_called_once()
    wl.lib.wlr_output_configuration_v1_send_succeeded.assert_not_called()
    wl.lib.wlr_output_configuration_v1_destroy.assert_called_once()


def test_output_power_disable(monkeypatch):
    """A DPMS off event commits `enabled=False` on the matching output."""
    _quiet_arrange(monkeypatch)
    monitor = make_monitor()
    s = make_server(monitor)
    event = SN(output=monitor.wlr_output, mode=0)

    wl.on_output_power_set_mode(s, event)

    wl.lib.wlr_output_state_set_enabled.assert_called_once()
    _state, enabled = wl.lib.wlr_output_state_set_enabled.call_args.args
    assert enabled is False
    wl.lib.wlr_output_commit_state.assert_called_once()


def test_output_power_unknown(monkeypatch):
    """Events for outputs we don't track are ignored."""
    _quiet_arrange(monkeypatch)
    s = make_server(make_monitor())
    event = SN(output=MagicMock(), mode=1)

    wl.on_output_power_set_mode(s, event)

    wl.lib.wlr_output_commit_state.assert_not_called()


def test_session_lock_focus(monkeypatch):
    """new_lock enables the backdrop, blanks focus, and accepts the lock;
    a per-output surface then takes keyboard focus on the selected monitor."""
    _quiet_arrange(monkeypatch)
    m = make_monitor()
    s = make_server(m)
    wlr_lock = SN()

    wl.on_new_session_lock(s, wlr_lock)

    assert s.locked is True
    assert s.current_lock is not None
    wl.lib.wlr_session_lock_v1_send_locked.assert_called_once_with(wlr_lock)
    wl.lib.wlr_scene_node_set_enabled.assert_any_call(
        wl.lib.pywl_scene_rect_node.return_value, True)

    lock_surface = SN(output=m.wlr_output, surface=SN(data=None))
    wl.on_new_lock_surface(s, s.current_lock, lock_surface)

    assert m.lock_surface is lock_surface
    wl.lib.wlr_session_lock_surface_v1_configure.assert_called_once()
    wl.lib.wlr_seat_keyboard_notify_enter.assert_called()


def test_session_lock_clears_pointer():
    """Lock start drops stale pointer focus and compositor drags."""
    m = make_monitor()
    s = make_server(m)
    s.grab = object()
    s.cursor_mode = wl.CursorMode.MOVE

    wl.on_new_session_lock(s, SN())

    wl.lib.wlr_seat_pointer_clear_focus.assert_called_once_with(s.seat)
    assert s.grab is None
    assert s.cursor_mode is wl.CursorMode.NORMAL


def test_session_lock_second_locker():
    """While a locker is active, a second new_lock is destroyed outright."""
    m = make_monitor()
    s = make_server(m)
    s.locked = True
    s.current_lock = wl.SessionLock(wlr=SN(), scene_tree=MagicMock())
    intruder = SN()

    wl.on_new_session_lock(s, intruder)

    wl.lib.wlr_session_lock_v1_destroy.assert_called_once_with(intruder)
    wl.lib.wlr_session_lock_v1_send_locked.assert_not_called()


def test_session_lock_unlock_focus(monkeypatch):
    """unlock disables the backdrop, clears `locked`, and refocuses."""
    _quiet_arrange(monkeypatch)
    # process_cursor_motion would walk monitors with mocked cursor coords.
    monkeypatch.setattr(wl, "process_cursor_motion", lambda *_a, **_k: None)
    m = make_monitor()
    s = make_server(m)
    lock = wl.SessionLock(wlr=SN(), scene_tree=MagicMock())
    s.current_lock = lock
    s.locked = True

    wl.destroy_lock(s, lock, unlocked=True)

    assert s.locked is False
    assert s.current_lock is None
    wl.lib.wlr_scene_node_set_enabled.assert_any_call(
        wl.lib.pywl_scene_rect_node.return_value, False)


def test_focus_client_locked():
    """While locked, focus_client must not touch the seat."""
    m = make_monitor()
    s = make_server(m)
    s.locked = True
    c = make_client(m)

    wl.focus_client(s, c, lift=True)

    wl.lib.wlr_seat_keyboard_notify_enter.assert_not_called()
    wl.lib.wlr_xdg_toplevel_set_activated.assert_not_called()


def test_cursor_button_locked_focus():
    """Locked: skip mouse bindings + click-to-focus; still forward."""
    m = make_monitor()
    s = make_server(m)
    s.locked = True
    ev = SN(state=wl.lib.WL_POINTER_BUTTON_STATE_PRESSED, button=1,
            time_msec=0)

    wl.on_cursor_button(s, ev)

    wl.lib.wlr_xdg_toplevel_set_activated.assert_not_called()
    wl.lib.wlr_seat_pointer_notify_button.assert_called_once()


def test_keyboard_key_locked(monkeypatch):
    """Locked: compositor keybindings are skipped; key still forwards."""
    m = make_monitor()
    s = make_server(m)
    s.locked = True
    dispatch = MagicMock(return_value=True)
    monkeypatch.setattr(wl, "dispatch_key", dispatch)
    ev = SN(state=wl.lib.WL_KEYBOARD_KEY_STATE_PRESSED, keycode=24,
            time_msec=123)

    wl.on_keyboard_key(s, ev)

    dispatch.assert_not_called()
    wl.lib.wlr_seat_keyboard_notify_key.assert_called_once_with(
        s.seat, 123, 24, wl.lib.WL_KEYBOARD_KEY_STATE_PRESSED)


def test_cursor_button_locked_binding(monkeypatch):
    """Locked: mouse bindings are skipped even if the click matches."""
    action = MagicMock()
    monkeypatch.setitem(wl.ACTIONS, "move_resize", action)
    monkeypatch.setattr(wl, "config", SN(
        buttons=[SN(mod=0, button=1, action="move_resize", arg="move")],
        keys=[], layouts=_fake_config.layouts,
    ))
    m = make_monitor()
    s = make_server(m)
    s.locked = True
    ev = SN(state=wl.lib.WL_POINTER_BUTTON_STATE_PRESSED, button=1,
            time_msec=0)

    wl.on_cursor_button(s, ev)

    action.assert_not_called()
    wl.lib.wlr_seat_pointer_notify_button.assert_called_once()


def test_session_lock_crash(monkeypatch):
    """If the locker dies before unlock, keep the black screen enabled."""
    refresh = MagicMock()
    monkeypatch.setattr(wl, "process_cursor_motion", refresh)
    m = make_monitor()
    s = make_server(m)
    lock = wl.SessionLock(wlr=SN(), scene_tree=MagicMock())
    s.current_lock = lock
    s.locked = True

    wl.destroy_lock(s, lock, unlocked=False)

    assert s.locked is True
    assert s.current_lock is None
    wl.lib.wlr_scene_node_set_enabled.assert_not_called()
    refresh.assert_not_called()


def test_session_lock_unlock_pointer(monkeypatch):
    """Unlock re-picks the pointer target without waiting for motion."""
    refresh = MagicMock()
    monkeypatch.setattr(wl, "process_cursor_motion", refresh)
    m = make_monitor()
    s = make_server(m)
    lock = wl.SessionLock(wlr=SN(), scene_tree=MagicMock())
    s.current_lock = lock
    s.locked = True

    wl.destroy_lock(s, lock, unlocked=True)

    refresh.assert_called_once_with(s, 0)


def test_lock_surface_destroy_self(monkeypatch):
    """wlroots requires the destroy listener list empty by teardown."""
    callbacks = []
    handle = MagicMock()

    def fake_listen(_signal, cb):
        callbacks.append(cb)
        return handle

    monkeypatch.setattr(wl, "listen", fake_listen)
    m = make_monitor()
    s = make_server(m)
    lock = wl.SessionLock(wlr=SN(), scene_tree=MagicMock())
    surface = SN(output=m.wlr_output, surface=SN(data=None))

    wl.on_new_lock_surface(s, lock, surface)
    callbacks[0](None)

    handle.remove.assert_called_once()
    assert m.lock_surface is None


def test_lock_surface_destroy_refocus():
    """If one lock surface dies, keyboard focus stays within the lock."""
    m1 = make_monitor()
    m2 = make_monitor()
    s = make_server(m1)
    s.monitors.append(m2)
    s.locked = True
    surface1 = SN(surface=object())
    surface2 = SN(surface=object())
    m1.lock_surface = surface1
    m2.lock_surface = surface2
    wl.lib.pywl_seat_keyboard_focused_surface.return_value = surface1.surface

    wl.on_lock_surface_destroy(s, m1, surface1)

    assert m1.lock_surface is None
    wl.lib.wlr_seat_keyboard_clear_focus.assert_not_called()
    wl.lib.wlr_seat_keyboard_notify_enter.assert_called_once()


def test_lock_surface_destroy_last():
    """If no lock surface remains, keyboard focus must be empty."""
    m = make_monitor()
    s = make_server(m)
    s.locked = True
    surface = SN(surface=object())
    m.lock_surface = surface
    wl.lib.pywl_seat_keyboard_focused_surface.return_value = surface.surface

    wl.on_lock_surface_destroy(s, m, surface)

    assert m.lock_surface is None
    wl.lib.wlr_seat_keyboard_clear_focus.assert_called_once_with(s.seat)


def test_update_monitors_lock_focus(monkeypatch):
    """After output changes, the selected screen's lock surface gets keys."""
    _quiet_arrange(monkeypatch)
    layout_box = SN(x=0, y=0, width=1000, height=800)
    monkeypatch.setattr(wl, "ffi", SN(
        NULL=None,
        addressof=lambda *_a, **_k: None,
        cast=lambda _t, x: x,
        new=lambda *_a, **_k: layout_box,
    ))
    m = make_monitor()
    lock_surface = SN(surface=SN(data=SN(node=MagicMock())))
    m.lock_surface = lock_surface
    s = make_server(m)
    s.locked = True
    wl.lib.wlr_seat_get_keyboard.return_value = wl.ffi.NULL

    wl.update_monitors(s)

    wl.lib.wlr_seat_keyboard_notify_enter.assert_called_with(
        s.seat, lock_surface.surface, wl.ffi.NULL, 0, wl.ffi.NULL)


def test_update_monitors_lock_resize(monkeypatch):
    """Layout changes keep app content hidden and lock surfaces fitted."""
    _quiet_arrange(monkeypatch)
    layout_box = SN(x=-10, y=20, width=3000, height=900)
    monkeypatch.setattr(wl, "ffi", SN(
        NULL=None,
        addressof=lambda *_a, **_k: None,
        cast=lambda _t, x: x,
        new=lambda *_a, **_k: layout_box,
    ))
    m = make_monitor(1000, 800)
    m.lock_surface = SN(surface=SN(data=SN(node=MagicMock())))
    s = make_server(m)

    wl.update_monitors(s)

    wl.lib.wlr_scene_rect_set_size.assert_called_with(
        s.locked_bg, 3000, 900)
    wl.lib.wlr_session_lock_surface_v1_configure.assert_called_with(
        m.lock_surface, 1000, 800)


def test_cleanup_monitor_lock_surface(monkeypatch):
    """Unplugging a screen while locked forgets its lock surface."""
    monkeypatch.setattr(wl, "arrange", lambda *_a: None)
    monkeypatch.setattr(wl, "print_status", lambda *_a: None)
    m1 = make_monitor()
    m2 = make_monitor()
    surface1 = SN(surface=object())
    surface2 = SN(surface=object())
    m1.lock_surface = surface1
    m2.lock_surface = surface2
    s = make_server(m1)
    s.monitors.append(m2)
    s.locked = True
    wl.lib.pywl_seat_keyboard_focused_surface.return_value = surface1.surface

    wl.cleanup_monitor(s, m1)

    assert m1.lock_surface is None
    assert m1 not in s.monitors
    assert s.selected_monitor is m2
    wl.lib.wlr_seat_keyboard_notify_enter.assert_called_once()


def test_cleanup_listener_order():
    """Shutdown should not let client teardown fire global listeners that
    still point at compositor-owned objects."""
    m = make_monitor()
    s = make_server(m)
    order = []
    s.listeners = [MagicMock(remove=lambda: order.append("remove"))]
    wl.lib.wl_display_destroy_clients.side_effect = lambda _d: order.append(
        "destroy_clients")

    wl.cleanup(s)

    assert order[:2] == ["remove", "destroy_clients"]


def test_unmap_scene_order(monkeypatch):
    """Idle-inhibit visibility is checked while the scene node is still
    valid; after that the surface must not keep a stale scene pointer."""
    m = make_monitor()
    s = make_server(m)
    c = make_client(m)
    c.surface = SN(data=object())
    c.scene_tree = SN(node=SN())
    s.clients.append(c)
    order = []

    def arrange(_server, _monitor):
        order.append("arrange")
        assert c.surface.data is not wl.ffi.NULL

    def destroy(_node):
        order.append("destroy")
        assert c.surface.data is wl.ffi.NULL

    monkeypatch.setattr(wl, "arrange", arrange)
    monkeypatch.setattr(wl, "focus_client", lambda *_a, **_k: None)
    monkeypatch.setattr(wl, "top_client", lambda *_a, **_k: None)
    monkeypatch.setattr(wl, "print_status", lambda *_a, **_k: None)
    wl.lib.wlr_scene_node_destroy.side_effect = destroy

    wl.on_xdg_toplevel_unmap(s, c, None)

    assert order == ["arrange", "destroy"]


def test_cursor_motion_zero_time(monkeypatch):
    """Internal pointer refreshes use time 0 and should not reset idle."""
    m = make_monitor()
    s = make_server(m)
    s.cursor.x = 10
    s.cursor.y = 20
    monkeypatch.setattr(wl, "surface_at", lambda *_a: None)

    wl.process_cursor_motion(s, 0)

    wl.lib.wlr_idle_notifier_v1_notify_activity.assert_not_called()


def test_cursor_motion_idle(monkeypatch):
    """Real pointer motion should reset idle."""
    m = make_monitor()
    s = make_server(m)
    s.cursor.x = 10
    s.cursor.y = 20
    monkeypatch.setattr(wl, "surface_at", lambda *_a: None)

    wl.process_cursor_motion(s, 123)

    wl.lib.wlr_idle_notifier_v1_notify_activity.assert_called_once_with(
        s.idle_notifier, s.seat)


def test_idle_inhibit_none(monkeypatch):
    """Nothing inhibiting: notifier's inhibited flag is False."""
    monkeypatch.setattr(wl, "_iter_idle_inhibitors", lambda _m: iter(()))
    monkeypatch.setattr(wl, "config", SN(idle_inhibit_ignore_visibility=False))
    s = make_server(make_monitor())

    wl.check_idle_inhibitor(s, None)

    wl.lib.wlr_idle_notifier_v1_set_inhibited.assert_called_once_with(
        s.idle_notifier, False)


def test_idle_inhibit_visible(monkeypatch):
    """A visible inhibitor (scene node reachable) inhibits idle."""
    monkeypatch.setattr(wl, "config", SN(idle_inhibit_ignore_visibility=False))
    surface = SN(data=SN(node=object()))
    inhibitor = SN(surface=surface)
    monkeypatch.setattr(
        wl, "_iter_idle_inhibitors", lambda _m: iter([inhibitor]))
    wl.lib.wlr_surface_get_root_surface.side_effect = lambda s: s
    wl.lib.wlr_scene_node_coords.return_value = True
    s = make_server(make_monitor())

    wl.check_idle_inhibitor(s, None)

    wl.lib.wlr_idle_notifier_v1_set_inhibited.assert_called_once_with(
        s.idle_notifier, True)


def test_idle_inhibit_excluded(monkeypatch):
    """An inhibitor whose root surface is `exclude` doesn't count."""
    monkeypatch.setattr(wl, "config", SN(idle_inhibit_ignore_visibility=False))
    surface = SN(data=SN(node=object()))
    inhibitor = SN(surface=surface)
    monkeypatch.setattr(
        wl, "_iter_idle_inhibitors", lambda _m: iter([inhibitor]))
    wl.lib.wlr_surface_get_root_surface.side_effect = lambda s: s
    s = make_server(make_monitor())

    wl.check_idle_inhibitor(s, surface)

    wl.lib.wlr_idle_notifier_v1_set_inhibited.assert_called_once_with(
        s.idle_notifier, False)


def test_idle_inhibit_bypass(monkeypatch):
    """Ignore-visibility mode inhibits without checking the scene."""
    monkeypatch.setattr(wl, "config", SN(idle_inhibit_ignore_visibility=True))
    inhibitor = SN(surface=SN(data=None))
    monkeypatch.setattr(
        wl, "_iter_idle_inhibitors", lambda _m: iter([inhibitor]))
    wl.lib.wlr_surface_get_root_surface.side_effect = lambda s: s
    s = make_server(make_monitor())

    wl.check_idle_inhibitor(s, None)

    wl.lib.wlr_scene_node_coords.assert_not_called()
    wl.lib.wlr_idle_notifier_v1_set_inhibited.assert_called_once_with(
        s.idle_notifier, True)
