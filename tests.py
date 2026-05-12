"""
Tests for pywl.

Naming: `test_<system>_<scenario>`, where `<system>` is 1-2 words for the
subsystem under test and `<scenario>` is 1-2 words for the specific case.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import wl


@pytest.fixture(autouse=True)
def _patch_bindings(monkeypatch):
    """Replace the cffi globals with mocks so tests run without wlroots."""
    lib = MagicMock()
    lib.pywl_seat_keyboard_focused_surface = MagicMock(return_value=None)
    lib.wlr_xdg_toplevel_set_size.return_value = 42

    monkeypatch.setattr(wl, "ffi", SimpleNamespace(
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


_fake_config = SimpleNamespace(
    border_width=3,
    border_color=(0.5, 0.5, 0.5, 1.0),
    focus_color=(1.0, 0.5, 0.0, 1.0),
    urgent_color=(0.8, 0.0, 0.0, 1.0),
    layouts=[
        SimpleNamespace(name="tile", symbol="[]="),
        SimpleNamespace(name="floating", symbol="><>"),
        SimpleNamespace(name="monocle", symbol="[M]"),
    ],
    rules=[],
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
        xdg_shell=MagicMock(), seat=MagicMock(), cursor=MagicMock(),
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


def test_commit_initial_no_resize():
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


def test_render_commits_when_idle():
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


def test_render_skips_pending_tile():
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


def test_render_ignores_pending_float():
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


def test_render_ignores_pending_other_monitor():
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


def test_render_ignores_pending_hidden_tag():
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

def test_resize_short_circuits_when_dims_match():
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


def test_resize_calls_set_size_when_dims_differ():
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
