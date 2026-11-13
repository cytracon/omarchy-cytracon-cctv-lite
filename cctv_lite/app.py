"""GTK4 / libadwaita multi-camera UI."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import __app_id__, __version__
from .config import Config, ensure_config
from .player import CameraPlayer
from .settings import SettingsWindow

log = logging.getLogger("cctv_lite.app")

ICON_NAME = "com.bbachmann.cctv-lite"

CSS = """
window {
  background: #0b0d10;
}
.camera-cell {
  background: #11141a;
  border: 1px solid #1e2430;
  border-radius: 6px;
  margin: 2px;
}
.camera-cell:hover {
  border-color: #3d5a80;
}
.camera-cell.selected {
  border-color: #3d8bfd;
  box-shadow: 0 0 0 1px #3d8bfd;
}
.camera-label {
  background: rgba(0,0,0,0.55);
  color: #e8eef7;
  font-size: 11px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 0 0 6px 0;
}
.camera-status {
  color: #9aa7b8;
  font-size: 10px;
  padding: 0 8px 4px 8px;
}
.camera-status.error {
  color: #ff7b72;
}
.camera-status.playing {
  color: #3fb950;
}
.toolbar-dark {
  background: #12151c;
}
.placeholder {
  color: #6b7785;
  font-size: 13px;
}
.detail-click {
  /* full-area click target for returning to grid */
}
"""


def _install_icon_search_paths() -> None:
    """Ensure user and app icon dirs are visible to the theme."""
    display = Gdk.Display.get_default()
    if display is None:
        return
    theme = Gtk.IconTheme.get_for_display(display)
    candidates = [
        Path.home() / ".local/share/icons",
        Path.home() / ".icons",
        Path.home() / ".local/share/cctv-lite/icons",
    ]
    for p in candidates:
        if p.is_dir():
            try:
                theme.add_search_path(str(p))
            except Exception:
                pass


class CameraCell(Gtk.Overlay):
    def __init__(self, player: CameraPlayer, show_labels: bool = True):
        super().__init__()
        self.player = player
        self.add_css_class("camera-cell")
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_cursor(Gdk.Cursor.new_from_name("pointer"))

        self.player.widget.set_hexpand(True)
        self.player.widget.set_vexpand(True)
        self.set_child(self.player.widget)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.set_halign(Gtk.Align.START)
        box.set_valign(Gtk.Align.START)
        box.set_can_target(False)

        self.label = Gtk.Label(label=player.name, xalign=0)
        self.label.add_css_class("camera-label")
        self.status = Gtk.Label(label="idle", xalign=0)
        self.status.add_css_class("camera-status")

        if show_labels:
            box.append(self.label)
            box.append(self.status)
        self.add_overlay(box)

        click = Gtk.GestureClick()
        click.set_button(1)
        click.connect("released", self._on_click)
        self.add_controller(click)

        self._on_activate = None
        self._selected = False

    def set_on_activate(self, cb) -> None:
        self._on_activate = cb

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        if selected:
            self.add_css_class("selected")
        else:
            self.remove_css_class("selected")

    def _on_click(self, gesture: Gtk.GestureClick, n_press: int, x: float, y: float):
        # Single click opens large view
        if self._on_activate:
            self.set_selected(True)
            self._on_activate(self.player)

    def update_status(self, status: str) -> None:
        self.status.set_text(status)
        self.status.remove_css_class("error")
        self.status.remove_css_class("playing")
        if status.startswith("error") or status.startswith("reconnect"):
            self.status.add_css_class("error")
        elif status == "playing" or status.startswith("playing"):
            self.status.add_css_class("playing")


class CctvWindow(Adw.ApplicationWindow):
    def __init__(self, app: "CctvApplication", config: Config):
        super().__init__(application=app, title="CCTV Lite")
        self.app = app
        self.config = config
        self.players: dict[str, CameraPlayer] = {}
        self.cells: dict[str, CameraCell] = {}
        self._grid: Gtk.Grid | None = None
        self._fullscreen_cam: str | None = None
        self._detail_player: CameraPlayer | None = None
        self._detail_epoch = 0
        self._layout_epoch = 0
        self._start_idle_ids: list[int] = []
        self._settings_win: SettingsWindow | None = None
        self._suppress_layout_signal = False
        self._background_suspended = False
        self._state_surface = None
        self._state_handler = None

        try:
            self.set_icon_name(ICON_NAME)
        except Exception:
            try:
                self.set_icon_name("cctv-lite")
            except Exception:
                pass

        ui = config.ui()
        self.set_default_size(
            int(ui.get("window_width") or 1600),
            int(ui.get("window_height") or 900),
        )

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_content(root)

        header = Adw.HeaderBar()
        header.add_css_class("toolbar-dark")
        # show title + icon in header / top bar
        title = Adw.WindowTitle(title="CCTV Lite", subtitle=f"v{__version__}")
        header.set_title_widget(title)
        root.append(header)
        self.header = header
        self._detail_took_fullscreen = False

        self.layout_model = Gtk.StringList()
        for lay in config.layouts():
            self.layout_model.append(lay.name)
        self.layout_dropdown = Gtk.DropDown(model=self.layout_model)
        self.layout_dropdown.set_selected(int(config.data.get("active_layout") or 0))
        self.layout_dropdown.set_tooltip_text("Layout")
        self.layout_dropdown.connect("notify::selected", self._on_layout_changed)
        header.pack_start(self.layout_dropdown)

        btn_settings = Gtk.Button(
            icon_name="emblem-system-symbolic",
            tooltip_text="Settings",
        )
        btn_settings.connect("clicked", lambda *_: self.open_settings())
        header.pack_end(btn_settings)

        btn_reload = Gtk.Button(
            icon_name="view-refresh-symbolic", tooltip_text="Reconnect all"
        )
        btn_reload.connect("clicked", lambda *_: self.reconnect_all())
        header.pack_end(btn_reload)

        btn_fs = Gtk.Button(
            icon_name="view-fullscreen-symbolic", tooltip_text="Toggle fullscreen"
        )
        btn_fs.connect("clicked", lambda *_: self._toggle_window_fullscreen())
        header.pack_end(btn_fs)

        quality_box = Gtk.Box(spacing=4)
        self.quality_sub = Gtk.ToggleButton(label="Sub")
        self.quality_main = Gtk.ToggleButton(label="Main")
        self.quality_sub.set_group(self.quality_main)
        lay0 = config.active_layout()
        if lay0.quality == "main":
            self.quality_main.set_active(True)
        else:
            self.quality_sub.set_active(True)
        self.quality_sub.connect("toggled", self._on_quality_toggled)
        self.quality_main.connect("toggled", self._on_quality_toggled)
        quality_box.append(self.quality_sub)
        quality_box.append(self.quality_main)
        header.pack_end(quality_box)

        self.stack = Gtk.Stack()
        self.stack.set_hexpand(True)
        self.stack.set_vexpand(True)
        # Instant switch: a crossfade snapshot of the 360p grid looks muddy
        # when opening the main-stream large view.
        self.stack.set_transition_type(Gtk.StackTransitionType.NONE)
        root.append(self.stack)

        self.grid_host = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.grid_host.set_hexpand(True)
        self.grid_host.set_vexpand(True)
        self.stack.add_named(self.grid_host, "grid")

        # Detail view: full-area click returns to grid
        self.single_host = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.single_host.set_hexpand(True)
        self.single_host.set_vexpand(True)
        self.single_picture_box = Gtk.Box()
        self.single_picture_box.set_hexpand(True)
        self.single_picture_box.set_vexpand(True)
        self.single_picture_box.add_css_class("detail-click")
        self.single_picture_box.set_cursor(Gdk.Cursor.new_from_name("pointer"))
        self.single_host.append(self.single_picture_box)
        self.stack.add_named(self.single_host, "single")
        self.stack.set_visible_child_name("grid")

        detail_click = Gtk.GestureClick()
        detail_click.set_button(1)
        detail_click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        detail_click.connect("released", self._on_detail_clicked)
        # CAPTURE on the whole detail host so video widgets don't swallow the click
        self.single_host.add_controller(detail_click)

        self._install_shortcuts()
        self.connect("map", lambda *_: self._on_map())
        self.connect("unmap", lambda *_: self._on_unmap())
        self.connect("realize", lambda *_: self._watch_surface_state())
        self.connect("unrealize", lambda *_: self._unwatch_surface_state())

        self.build_layout()
        # Name sync in a worker thread — never block the GTK main loop with curl probes
        GLib.timeout_add_seconds(3, self._auto_sync_names)

    def _auto_sync_names(self) -> bool:
        import threading
        from copy import deepcopy

        def worker():
            try:
                from .nvr import sync_names

                data = deepcopy(self.config.data)
                n, msg = sync_names(data)
                log.info("auto NVR name sync: %s", msg)

                def apply():
                    if n > 0:
                        self.config.data = data
                        try:
                            self.config.save()
                        except Exception:
                            log.exception("save names failed")
                        self.refresh_labels_from_config()
                    return False

                GLib.idle_add(apply)
            except Exception:
                log.exception("auto NVR name sync failed")

        threading.Thread(target=worker, name="cctv-nvr-names", daemon=True).start()
        return False  # one-shot

    def _install_shortcuts(self) -> None:
        controller = Gtk.ShortcutController()
        controller.set_scope(Gtk.ShortcutScope.LOCAL)

        def add(key, cb):
            trigger = Gtk.ShortcutTrigger.parse_string(key)
            action = Gtk.CallbackAction.new(lambda *_a: (cb() or True))
            controller.add_shortcut(Gtk.Shortcut.new(trigger, action))

        add("F11", self._toggle_window_fullscreen)
        add("Escape", self._on_escape)
        add("r", self.reconnect_all)
        add("space", self._toggle_pause_all)
        add("<primary>comma", self.open_settings)
        self.add_controller(controller)

    def open_settings(self) -> None:
        if self._settings_win is not None:
            self._settings_win.present()
            return

        def on_saved(cfg: Config, reconnect: bool):
            self.config = cfg
            self.app.config = cfg
            style = self.app.get_style_manager()
            if cfg.ui().get("dark", True):
                style.set_color_scheme(Adw.ColorScheme.FORCE_DARK)
            else:
                style.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
            self._reload_layout_dropdown()
            if reconnect:
                self.build_layout()
            else:
                self.refresh_labels_from_config()

        def on_names_synced(cfg: Config):
            self.config = cfg
            self.app.config = cfg
            self.refresh_labels_from_config()

        try:
            win = SettingsWindow(
                self, self.config, on_saved=on_saved, on_names_synced=on_names_synced
            )
            win.connect("close-request", self._on_settings_closed)
            self._settings_win = win
            win.present()
        except Exception:
            log.exception("Settings failed to open")
            # Prefer a dialog over silent failure
            dlg = Adw.MessageDialog(
                transient_for=self,
                heading="Settings failed to open",
                body="See logs (journalctl / terminal). Run: python3 tests/smoke_settings.py",
            )
            dlg.add_response("ok", "OK")
            dlg.present()

    def _on_settings_closed(self, *_):
        self._settings_win = None
        return False

    def _reload_layout_dropdown(self) -> None:
        self._suppress_layout_signal = True
        self.layout_model = Gtk.StringList()
        for lay in self.config.layouts():
            self.layout_model.append(lay.name)
        self.layout_dropdown.set_model(self.layout_model)
        idx = int(self.config.data.get("active_layout") or 0)
        n = self.layout_model.get_n_items()
        if n:
            self.layout_dropdown.set_selected(max(0, min(idx, n - 1)))
        self._suppress_layout_signal = False

    def _on_detail_clicked(self, *_args):
        # Click anywhere on large view → back to overview
        if self._fullscreen_cam:
            self.show_grid()

    def _on_escape(self) -> None:
        if self._fullscreen_cam:
            self.show_grid()
        elif self.is_fullscreen():
            self.unfullscreen()

    def _toggle_window_fullscreen(self) -> None:
        if self.is_fullscreen():
            self.unfullscreen()
        else:
            self.fullscreen()

    def _visible_players(self) -> list[CameraPlayer]:
        if self._fullscreen_cam and self._detail_player is not None:
            return [self._detail_player]
        return list(self.players.values())

    def _toggle_pause_all(self) -> None:
        players = self._visible_players()
        any_playing = any(str(p.status).startswith("playing") for p in players)
        if any_playing:
            for player in players:
                player.pause()
        elif self._fullscreen_cam and self._detail_player is not None:
            self._detail_player.play()
        else:
            self._start_players_staggered()

    def _on_map(self) -> None:
        if self._background_suspended:
            return
        if not self.config.ui().get("pause_when_unmapped", True):
            return

        def stale(player: CameraPlayer) -> bool:
            status = player.status
            return status in ("paused", "stopped", "idle") or status.startswith("error")

        if self._fullscreen_cam and self._detail_player:
            if stale(self._detail_player):
                self._detail_player.hard_restart()
        elif any(stale(player) for player in self.players.values()):
            self._start_players_staggered_hard()

    def _on_unmap(self) -> None:
        if self.config.ui().get("pause_when_unmapped", True):
            self._clear_start_timers()
            for p in self.players.values():
                p.stop()
            if self._detail_player:
                self._detail_player.stop()

    def _watch_surface_state(self) -> None:
        self._unwatch_surface_state()
        surface = self.get_surface()
        if surface is not None and hasattr(surface, "get_state"):
            self._state_surface = surface
            self._state_handler = surface.connect("notify::state", self._on_surface_state)
            self._on_surface_state(surface)

    def _unwatch_surface_state(self) -> None:
        if self._state_surface is not None and self._state_handler is not None:
            self._state_surface.disconnect(self._state_handler)
        self._state_surface = None
        self._state_handler = None

    def _on_surface_state(self, surface, *_args) -> None:
        # On Wayland a window can stay mapped while the compositor suspends it.
        # map/unmap alone therefore misses minimized or suspended windows.
        flags = Gdk.ToplevelState.MINIMIZED | getattr(Gdk.ToplevelState, "SUSPENDED", 0)
        suspended = bool(surface.get_state() & flags) and self.config.ui().get("pause_when_unmapped", True)
        if suspended == self._background_suspended:
            return
        self._background_suspended = suspended
        if suspended:
            log.info("Window suspended: stopping camera streams")
            self._on_unmap()
        elif self.get_mapped():
            log.info("Window visible again: resuming camera streams")
            self._on_map()

    def _on_layout_changed(self, dropdown, *_):
        if self._suppress_layout_signal:
            return
        idx = int(dropdown.get_selected())
        self.config.set_active_layout(idx)
        try:
            self.config.save()
        except Exception:
            log.exception("save active layout failed")
        lay = self.config.active_layout()
        if lay.quality == "main":
            self.quality_main.set_active(True)
        else:
            self.quality_sub.set_active(True)
        self.build_layout()

    def _on_quality_toggled(self, btn: Gtk.ToggleButton):
        if not btn.get_active():
            return
        quality = "main" if btn is self.quality_main else "sub"
        self._apply_quality(quality)

    def _current_quality(self) -> str:
        return "main" if self.quality_main.get_active() else "sub"

    def _apply_quality(self, quality: str) -> None:
        cmap = self.config.camera_map()
        for cam_id, player in self.players.items():
            cam = cmap.get(cam_id)
            if not cam:
                continue
            url = self.config.url_for_camera(cam, quality)
            if url != player.url:
                player.set_url(url, autoplay=True)

    def _clear_start_timers(self) -> None:
        for i in self._start_idle_ids:
            try:
                GLib.source_remove(i)
            except Exception:
                pass
        self._start_idle_ids.clear()

    def _empty_box(self, box: Gtk.Widget) -> None:
        child = box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            box.remove(child)
            child = nxt

    def _restore_grid_chrome(self) -> None:
        try:
            self.header.set_visible(True)
        except Exception:
            pass
        if self._detail_took_fullscreen:
            self._detail_took_fullscreen = False
            if self.is_fullscreen():
                self.unfullscreen()
        self.stack.set_visible_child_name("grid")
        for cell in self.cells.values():
            cell.set_selected(False)

    def _when_released(
        self,
        players: list[CameraPlayer],
        then: Callable[[], None],
        timeout_ms: int = 1200,
    ) -> None:
        """NULL every pipeline, then run then(). A timeout continues anyway."""
        if not players:
            GLib.idle_add(lambda: then() or False)
            return
        pending = {id(player) for player in players}
        fired = False

        def fire():
            nonlocal fired
            if fired:
                return False
            fired = True
            try:
                then()
            except Exception:
                log.exception("release continuation failed")
            return False

        def one(player_id: int):
            pending.discard(player_id)
            if not pending:
                GLib.idle_add(fire)

        for player in players:
            player.suspend(on_settled=lambda i=id(player): one(i))
        GLib.timeout_add(timeout_ms, fire)

    def _join_players(self, players: list[CameraPlayer], timeout: float = 2.0) -> None:
        import time

        deadline = time.monotonic() + timeout
        for player in players:
            left = deadline - time.monotonic()
            if left > 0:
                player.join_null(left)

    def teardown_players(self) -> None:
        self._clear_start_timers()
        self._detail_epoch += 1
        self._fullscreen_cam = None
        players = list(self.players.values())
        if self._detail_player is not None:
            players.append(self._detail_player)
        self._detail_player = None
        self.players.clear()
        self.cells.clear()
        for player in players:
            player.destroy()
        self._join_players(players)
        self._empty_box(self.grid_host)
        self._empty_box(self.single_picture_box)
        self._grid = None

    def build_layout(self) -> None:
        self._layout_epoch += 1
        epoch = self._layout_epoch
        self._detail_epoch += 1
        self._fullscreen_cam = None
        self._clear_start_timers()
        self._restore_grid_chrome()
        old = list(self.players.values())
        if self._detail_player is not None:
            old.append(self._detail_player)
        self.players = {}
        self.cells = {}
        self._detail_player = None

        def populate():
            if epoch != self._layout_epoch:
                return
            self._empty_box(self.grid_host)
            self._empty_box(self.single_picture_box)
            self._fill_grid()

        self._when_released(old, populate, timeout_ms=1500)

    def _fill_grid(self) -> None:
        layout = self.config.active_layout()
        quality = self._current_quality() or layout.quality
        show_labels = bool(self.config.ui().get("show_labels", True))
        cmap = self.config.camera_map()
        rtsp = self.config.rtsp()

        grid = Gtk.Grid()
        grid.set_column_homogeneous(True)
        grid.set_row_homogeneous(True)
        grid.set_column_spacing(2)
        grid.set_row_spacing(2)
        grid.set_hexpand(True)
        grid.set_vexpand(True)
        self._grid = grid
        self.grid_host.append(grid)

        cam_ids = list(layout.cameras)
        cols = layout.cols
        rows = layout.rows
        slots = cols * rows

        def on_status(cam_id: str, status: str):
            cell = self.cells.get(cam_id)
            if cell:
                GLib.idle_add(cell.update_status, status)

        for i in range(slots):
            r, c = i // cols, i % cols
            if i >= len(cam_ids):
                ph = Gtk.Label(label="")
                ph.add_css_class("placeholder")
                ph.set_hexpand(True)
                ph.set_vexpand(True)
                grid.attach(ph, c, r, 1, 1)
                continue
            cam_id = cam_ids[i]
            cam = cmap.get(cam_id)
            if cam is None or not cam.enabled:
                ph = Gtk.Label(label=f"Missing {cam_id}")
                ph.add_css_class("placeholder")
                grid.attach(ph, c, r, 1, 1)
                continue
            url = self.config.url_for_camera(cam, quality)
            player = CameraPlayer(
                cam_id=cam.id,
                name=cam.name,
                url=url,
                rtsp_opts=rtsp,
                on_status=on_status,
            )
            cell = CameraCell(player, show_labels=show_labels)
            cell.set_on_activate(self._open_single)
            self.players[cam.id] = player
            self.cells[cam.id] = cell
            grid.attach(cell, c, r, 1, 1)

        self._start_players_staggered()

    def _start_players_staggered(self) -> None:
        self._clear_start_timers()
        if self._background_suspended:
            return
        stagger = int(self.config.rtsp().get("stagger_ms") or 150)
        for i, player in enumerate(self.players.values()):

            def start(p=player):
                p.play()
                return False

            if stagger <= 0 or i == 0:
                start()
            else:
                sid = GLib.timeout_add(stagger * i, start)
                self._start_idle_ids.append(sid)

    def reconnect_all(self) -> None:
        """Fresh RTSP sessions for the streams that are actually on screen."""
        self._clear_start_timers()
        if self._background_suspended:
            return
        if self._fullscreen_cam and self._detail_player is not None:
            self._detail_player.hard_restart()
            return
        for p in self.players.values():
            p.stop()
        self._start_players_staggered_hard()

    def _start_players_staggered_hard(self) -> None:
        """Staggered hard_restart (new RTSP session per camera)."""
        self._clear_start_timers()
        if self._background_suspended:
            return
        stagger = int(self.config.rtsp().get("stagger_ms") or 150)
        for i, player in enumerate(self.players.values()):

            def start(p=player):
                p.hard_restart()
                return False

            if stagger <= 0 or i == 0:
                start()
            else:
                sid = GLib.timeout_add(stagger * i, start)
                self._start_idle_ids.append(sid)

    def refresh_labels_from_config(self) -> None:
        cmap = self.config.camera_map()
        for cam_id, cell in self.cells.items():
            cam = cmap.get(cam_id)
            if not cam:
                continue
            cell.player.set_name(cam.name)
            cell.label.set_text(cam.name)
            cell.label.set_tooltip_text(
                f"{cam.name}\nmain={cam.channel_main}  sub={cam.channel_sub}"
            )

    def sync_names_from_nvr(self, quiet: bool = False) -> str:
        """Fetch channel names via ISAPI and apply. Returns status message."""
        from .nvr import sync_names

        n, msg = sync_names(self.config.data)
        if n > 0:
            try:
                self.config.save()
            except Exception:
                log.exception("save after NVR name sync failed")
            self.refresh_labels_from_config()
        if not quiet:
            log.info("NVR name sync: %s", msg)
        return msg

    def _open_single(self, player: CameraPlayer) -> None:
        """Main stream of one camera. Other RTSP sessions are closed first."""
        cam = self.config.camera_map().get(player.cam_id)
        if cam is None:
            return
        self._clear_start_timers()
        self._detail_epoch += 1
        epoch = self._detail_epoch
        self._fullscreen_cam = player.cam_id
        for cell in self.cells.values():
            cell.set_selected(cell.player.cam_id == player.cam_id)

        prev = self._detail_player
        self._detail_player = None
        self.stack.set_visible_child_name("single")
        try:
            self.header.set_visible(False)
        except Exception:
            pass
        if not self.is_fullscreen():
            self._detail_took_fullscreen = True
            self.fullscreen()
        else:
            self._detail_took_fullscreen = False
        targets = list(self.players.values())
        if prev is not None:
            targets.append(prev)

        def start():
            if epoch != self._detail_epoch:
                return
            self._mount_detail(cam, epoch)

        # Suspend first so paintables are cleared before the placeholder
        # unparents the previous large-view widget.
        self._when_released(targets, start, timeout_ms=1200)
        self._set_detail_placeholder(f"{cam.name}\nClosing other cameras…")

    def _mount_detail(self, cam, epoch: int) -> None:
        if epoch != self._detail_epoch:
            return
        self._empty_box(self.single_picture_box)
        detail = CameraPlayer(
            cam_id=f"{cam.id}-detail",
            name=cam.name,
            url=self.config.url_for_camera(cam, "main"),
            rtsp_opts=self.config.rtsp(),
        )
        overlay = Gtk.Overlay()
        overlay.set_hexpand(True)
        overlay.set_vexpand(True)
        overlay.set_child(detail.widget)
        label = Gtk.Label(label=f"{cam.name}  ·  main  ·  click to return", xalign=0)
        label.add_css_class("camera-label")
        label.set_halign(Gtk.Align.START)
        label.set_valign(Gtk.Align.START)
        label.set_can_target(False)
        overlay.add_overlay(label)
        hint = Gtk.Label(
            label="Click video or press Esc to return to overview", xalign=0.5
        )
        hint.add_css_class("camera-status")
        hint.set_halign(Gtk.Align.CENTER)
        hint.set_valign(Gtk.Align.END)
        hint.set_margin_bottom(12)
        hint.set_can_target(False)
        overlay.add_overlay(hint)
        self.single_picture_box.append(overlay)
        self._detail_player = detail

        # Build/play after the widget is in the tree so gtk4paintablesink
        # sees a real allocation instead of scaling a tiny first frame.
        try:
            detail.build()
        except Exception as e:
            log.error("detail start failed: %s", e)
            self._set_detail_placeholder(f"{cam.name}: start failed — {e}")
            return

        def _start(_detail=detail, _epoch=epoch):
            if (
                self._background_suspended
                or self._detail_player is not _detail
                or _epoch != self._detail_epoch
            ):
                return False
            _detail.play()
            return False

        # Widget is in the tree, so the first frame is scaled to the real window.
        GLib.timeout_add(50, _start)

    def _set_detail_placeholder(self, text: str) -> None:
        self._empty_box(self.single_picture_box)
        lab = Gtk.Label(label=text)
        lab.add_css_class("placeholder")
        lab.set_hexpand(True)
        lab.set_vexpand(True)
        self.single_picture_box.append(lab)

    def show_grid(self) -> None:
        self._detail_epoch += 1
        epoch = self._detail_epoch
        self._fullscreen_cam = None
        detail = self._detail_player
        self._detail_player = None
        self._restore_grid_chrome()

        def restart():
            if epoch != self._detail_epoch:
                return
            self._empty_box(self.single_picture_box)
            if self.players:
                self._start_players_staggered()

        if detail is None:
            restart()
        else:
            detail.destroy(on_dead=restart)

    def destroy_all(self) -> None:
        if self._settings_win is not None:
            self._settings_win.destroy()
            self._settings_win = None
        self.teardown_players()


class CctvApplication(Adw.Application):
    def __init__(self, config: Config):
        super().__init__(
            application_id=__app_id__,
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )
        self.config = config
        self.win: CctvWindow | None = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        # WM class / top-bar identity
        GLib.set_prgname(__app_id__)
        GLib.set_application_name("CCTV Lite")
        _install_icon_search_paths()
        try:
            Gtk.Window.set_default_icon_name(ICON_NAME)
        except Exception:
            try:
                Gtk.Window.set_default_icon_name("cctv-lite")
            except Exception:
                pass

        css = Gtk.CssProvider()
        css.load_from_data(CSS.encode("utf-8"))
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
        if self.config.ui().get("dark", True):
            style = self.get_style_manager()
            style.set_color_scheme(Adw.ColorScheme.FORCE_DARK)

        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<primary>q"])

        settings_action = Gio.SimpleAction.new("settings", None)
        settings_action.connect(
            "activate",
            lambda *_: self.win.open_settings() if self.win else None,
        )
        self.add_action(settings_action)
        self.set_accels_for_action("app.settings", ["<primary>comma"])

    def do_activate(self):
        if self.win is None:
            self.win = CctvWindow(self, self.config)
            self.win.connect("close-request", self._on_close)
        self.win.present()

    def _on_close(self, *_):
        if self.win:
            self.win.destroy_all()
        return False

    def do_shutdown(self):
        if self.win:
            self.win.destroy_all()
        Adw.Application.do_shutdown(self)


def run_app(config: Config | None = None) -> int:
    cfg = config or ensure_config(import_if_missing=True)
    app = CctvApplication(cfg)
    return app.run(None)
