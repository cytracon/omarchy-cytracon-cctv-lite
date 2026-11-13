"""Settings dialog for CCTV Lite (libadwaita Preferences)."""

from __future__ import annotations

import logging
from copy import deepcopy
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk  # noqa: E402

from .config import Config

log = logging.getLogger("cctv_lite.settings")


def _entry_row(title: str, text: str = "", tooltip: str = "") -> Adw.EntryRow:
    row = Adw.EntryRow(title=title)
    row.set_text(text or "")
    if tooltip:
        row.set_tooltip_text(tooltip)
    return row


def _password_row(title: str, text: str = "") -> Adw.PasswordEntryRow:
    row = Adw.PasswordEntryRow(title=title)
    row.set_text(text or "")
    return row


def _spin_row(
    title: str,
    value: float,
    lower: float,
    upper: float,
    step: float,
    digits: int = 0,
    tooltip: str = "",
) -> Adw.SpinRow:
    adj = Gtk.Adjustment(
        value=value,
        lower=lower,
        upper=upper,
        step_increment=step,
        page_increment=step * 5,
    )
    row = Adw.SpinRow(title=title, adjustment=adj, digits=digits)
    if tooltip:
        row.set_tooltip_text(tooltip)
    return row


def _switch_row(title: str, active: bool, subtitle: str = "") -> Adw.SwitchRow:
    row = Adw.SwitchRow(title=title, active=active)
    if subtitle:
        row.set_subtitle(subtitle)
    return row


class SettingsWindow(Adw.PreferencesWindow):
    """Edit connection, stream, UI and camera names; save to config.yaml."""

    def __init__(
        self,
        parent: Gtk.Window,
        config: Config,
        on_saved: Callable[[Config, bool], None] | None = None,
        on_names_synced: Callable[[Config], None] | None = None,
    ):
        super().__init__()
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_title("CCTV Lite Settings")
        self.set_search_enabled(True)
        self.set_default_size(560, 720)
        self.config = config
        self.on_saved = on_saved
        self.on_names_synced = on_names_synced
        self._draft = deepcopy(config.data)
        self._cam_name_rows: list[tuple[dict, Adw.EntryRow]] = []
        self._order_rows: list[Adw.ActionRow] = []
        self._layout_order: list[str] = self._initial_layout_order()
        self._need_reconnect = False
        self._order_group: Adw.PreferencesGroup | None = None

        self._build_connection_page()
        self._build_stream_page()
        self._build_ui_page()
        self._build_cameras_page()
        self._build_actions_page()


    @staticmethod
    def _clean_cam(cam: dict) -> dict:
        """Strip UI-only keys (e.g. Gtk widgets) so YAML/deepcopy work."""
        out = {}
        for k, v in cam.items():
            if str(k).startswith("_"):
                continue
            # Widgets / callables must never enter config
            if callable(v):
                continue
            try:
                from gi.repository import GObject as _GObject

                if isinstance(v, _GObject.Object):
                    continue
            except Exception:
                pass
            out[k] = v
        return out

    def _make_save_group(self, subtitle: str = "") -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Save",
            description=subtitle or "Write config and refresh the live grid",
        )
        row = Adw.ActionRow(
            title="Save and Apply",
            subtitle="Saves order, names and connection, then rebuilds the viewer",
        )
        btn = Gtk.Button(label="Save and Apply")
        btn.add_css_class("suggested-action")
        btn.add_css_class("pill")
        btn.set_valign(Gtk.Align.CENTER)
        btn.connect("clicked", lambda *_: self._save(apply_reconnect=True))
        row.add_suffix(btn)
        row.set_activatable_widget(btn)
        group.add(row)

        soft = Adw.ActionRow(
            title="Save only",
            subtitle="Write file without reconnecting streams",
        )
        soft_btn = Gtk.Button(label="Save")
        soft_btn.set_valign(Gtk.Align.CENTER)
        soft_btn.connect("clicked", lambda *_: self._save(apply_reconnect=False))
        soft.add_suffix(soft_btn)
        group.add(soft)
        return group

    # ---- pages ----

    def _build_connection_page(self) -> None:
        page = Adw.PreferencesPage(title="Connection", icon_name="network-server-symbolic")
        group = Adw.PreferencesGroup(title="NVR / Recorder", description="RTSP base connection")

        d = self._draft
        self.host_row = _entry_row(
            "RTSP Host / IP",
            str(d.get("host") or ""),
            tooltip="Recorder address for video streams",
        )
        self.port_row = _spin_row(
            "RTSP port", float(d.get("port") or 554), 1, 65535, 1
        )
        self.http_host_row = _entry_row(
            "HTTP host (names)",
            str(d.get("http_host") or ""),
            tooltip="Optional LAN IP for ISAPI names if RTSP uses a public IP. Empty = same as RTSP host.",
        )
        self.http_port_row = _spin_row(
            "HTTP / ISAPI port",
            float(d.get("http_port") or 80),
            1,
            65535,
            1,
            tooltip="For camera names from NVR (often 80 or 8000). Must be reachable.",
        )
        self.http_scheme_row = Adw.ComboRow(title="HTTP scheme")
        scheme_model = Gtk.StringList.new(["http", "https"])
        self.http_scheme_row.set_model(scheme_model)
        scheme = str(d.get("http_scheme") or "http").lower()
        self.http_scheme_row.set_selected(1 if scheme == "https" else 0)
        self.user_row = _entry_row("Username", str(d.get("user") or ""))
        self.pass_row = _password_row("Password", str(d.get("password") or ""))
        self.path_row = _entry_row(
            "Path template",
            str(d.get("path_template") or "/ISAPI/Streaming/channels/{channel}"),
            tooltip="Use {channel} for the stream number (e.g. 102 = cam1 sub)",
        )

        group.add(self.host_row)
        group.add(self.port_row)
        group.add(self.http_host_row)
        group.add(self.http_port_row)
        group.add(self.http_scheme_row)
        group.add(self.user_row)
        group.add(self.pass_row)
        group.add(self.path_row)
        page.add(group)
        self.add(page)

    def _build_stream_page(self) -> None:
        page = Adw.PreferencesPage(title="Stream", icon_name="video-display-symbolic")
        rtsp = dict(self._draft.get("rtsp") or {})
        group = Adw.PreferencesGroup(
            title="RTSP / Performance",
            description="Lower latency = snappier, more network load",
        )

        self.latency_row = _spin_row(
            "Latency (ms)",
            float(rtsp.get("latency_ms") or 120),
            0,
            5000,
            10,
            tooltip="Jitter buffer latency",
        )
        self.stagger_row = _spin_row(
            "Stagger start (ms)",
            float(rtsp.get("stagger_ms") or 150),
            0,
            2000,
            10,
            tooltip="Delay between starting each camera (reduces connection storms)",
        )
        self.reconnect_row = _spin_row(
            "Reconnect delay (s)",
            float(rtsp.get("reconnect_delay_s") or 3),
            0.5,
            60,
            0.5,
            digits=1,
        )

        # Protocol combo
        self.proto_row = Adw.ComboRow(title="Protocol")
        proto_model = Gtk.StringList.new(["tcp", "udp", "udp-mcast", "http"])
        self.proto_row.set_model(proto_model)
        cur = str(rtsp.get("protocols") or "tcp").lower()
        try:
            idx = ["tcp", "udp", "udp-mcast", "http"].index(cur)
        except ValueError:
            idx = 0
        self.proto_row.set_selected(idx)

        self.drop_row = _switch_row(
            "Drop on latency",
            bool(rtsp.get("drop_on_latency", True)),
            "Drop late frames instead of buffering",
        )
        self.mute_row = _switch_row(
            "Mute audio",
            bool(rtsp.get("mute_audio", True)),
            "Disable audio sinks (saves CPU)",
        )
        self.hw_row = _switch_row(
            "Prefer hardware decode",
            bool(rtsp.get("use_hardware_decode", True)),
            "Decode and scale on the GPU (VA-API, RX 7600 / Intel)",
        )

        group.add(self.latency_row)
        group.add(self.stagger_row)
        group.add(self.reconnect_row)
        group.add(self.proto_row)
        group.add(self.drop_row)
        group.add(self.mute_row)
        group.add(self.hw_row)
        page.add(group)
        self.add(page)

    def _build_ui_page(self) -> None:
        page = Adw.PreferencesPage(title="Interface", icon_name="preferences-desktop-display-symbolic")
        ui = dict(self._draft.get("ui") or {})
        group = Adw.PreferencesGroup(title="Window")

        self.labels_row = _switch_row(
            "Show camera labels",
            bool(ui.get("show_labels", True)),
        )
        self.pause_unmap_row = _switch_row(
            "Pause when window hidden",
            bool(ui.get("pause_when_unmapped", True)),
            "Stops streams when minimized/unmapped",
        )
        self.dark_row = _switch_row("Dark theme", bool(ui.get("dark", True)))
        self.width_row = _spin_row(
            "Default width", float(ui.get("window_width") or 1600), 640, 7680, 10
        )
        self.height_row = _spin_row(
            "Default height", float(ui.get("window_height") or 900), 480, 4320, 10
        )

        group.add(self.labels_row)
        group.add(self.pause_unmap_row)
        group.add(self.dark_row)
        group.add(self.width_row)
        group.add(self.height_row)
        page.add(group)
        self.add(page)

    def _initial_layout_order(self) -> list[str]:
        """Camera id order for the multi-view grid (first layout, or all cameras)."""
        layouts = self._draft.get("layouts") or []
        cams = self._draft.get("cameras") or []
        all_ids = [str(c.get("id")) for c in cams if isinstance(c, dict) and c.get("id")]
        if layouts and isinstance(layouts[0], dict) and layouts[0].get("cameras"):
            order = [str(x) for x in layouts[0]["cameras"]]
            # append any missing
            for i in all_ids:
                if i not in order:
                    order.append(i)
            return order
        return all_ids

    def _cam_by_id(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for c in self._draft.get("cameras") or []:
            if isinstance(c, dict) and c.get("id"):
                out[str(c["id"])] = c
        return out

    def _build_cameras_page(self) -> None:
        page = Adw.PreferencesPage(title="Cameras", icon_name="camera-web-symbolic")
        self._cameras_page = page

        # --- Grid order (rearrange) ---
        self._order_group = Adw.PreferencesGroup(
            title="Grid arrangement",
            description="Drag rows to reorder, or use up/down. Then Save and Apply.",
        )
        btn_ch = Gtk.Button(label="Sort 1–16")
        btn_ch.set_tooltip_text("Order by channel number (1 … 16)")
        btn_ch.connect("clicked", lambda *_: self._sort_order("channel"))
        btn_az = Gtk.Button(label="Sort A–Z")
        btn_az.set_tooltip_text("Order by display name")
        btn_az.connect("clicked", lambda *_: self._sort_order("name"))
        self._sort_row = Adw.ActionRow(title="Quick sort")
        self._sort_row.set_subtitle("or drag cameras below")
        self._sort_row.add_suffix(btn_ch)
        self._sort_row.add_suffix(btn_az)
        self._order_group.add(self._sort_row)

        self._rebuild_order_rows()
        page.add(self._order_group)

        # Save at top of Cameras (where reorder happens)
        page.add(
            self._make_save_group(
                "After reordering — press Save and Apply here"
            )
        )

        # --- Names ---
        group = Adw.PreferencesGroup(
            title="Names and channels",
            description="Hikvision: main = N01, sub = N02 · edit labels below",
        )
        self._cam_name_rows.clear()
        by_id = self._cam_by_id()
        ordered_cams = []
        for cid in self._layout_order:
            if cid in by_id:
                ordered_cams.append(by_id[cid])
        for cam in by_id.values():
            if cam not in ordered_cams:
                ordered_cams.append(cam)

        for cam in ordered_cams:
            name = str(cam.get("name") or cam.get("id") or "Camera")
            subtitle = (
                f"id={cam.get('id')}  main={cam.get('channel_main')}  "
                f"sub={cam.get('channel_sub')}"
            )
            row = Adw.EntryRow(title=str(cam.get("id") or "cam"))
            row.set_text(name)
            row.set_tooltip_text(subtitle)
            en = Gtk.CheckButton()
            en.set_active(bool(cam.get("enabled", True)))
            en.set_valign(Gtk.Align.CENTER)
            en.set_tooltip_text("Enabled")
            row.add_prefix(en)
            cam["_ui_enabled_btn"] = en
            self._cam_name_rows.append((cam, row))
            group.add(row)
        page.add(group)
        self.add(page)

    def _clear_order_rows(self) -> None:
        """Remove only tracked order rows (never walk PreferencesGroup children)."""
        if self._order_group is None:
            return
        for row in list(self._order_rows):
            try:
                self._order_group.remove(row)
            except Exception:
                log.debug("remove order row failed", exc_info=True)
        self._order_rows.clear()

    def _rebuild_order_rows(self) -> None:
        if self._order_group is None:
            return
        self._clear_order_rows()
        by_id = self._cam_by_id()
        n = len(self._layout_order)
        for i, cid in enumerate(self._layout_order):
            cam = by_id.get(cid) or {"id": cid, "name": cid}
            title = str(cam.get("name") or cid)
            row = Adw.ActionRow(
                title=f"{i + 1}. {title}",
                subtitle=(
                    f"{cid}  ·  main={cam.get('channel_main', '?')}  "
                    f"sub={cam.get('channel_sub', '?')}  ·  drag to move"
                ),
            )
            row.set_activatable(False)

            grip = Gtk.Image.new_from_icon_name("open-menu-symbolic")
            grip.set_opacity(0.55)
            grip.set_tooltip_text("Drag to reorder")
            row.add_prefix(grip)

            up = Gtk.Button(icon_name="go-up-symbolic")
            up.set_valign(Gtk.Align.CENTER)
            up.set_tooltip_text("Move up")
            up.set_sensitive(i > 0)
            up.connect("clicked", lambda _b, idx=i: self._move_order(idx, -1))
            down = Gtk.Button(icon_name="go-down-symbolic")
            down.set_valign(Gtk.Align.CENTER)
            down.set_tooltip_text("Move down")
            down.set_sensitive(i < n - 1)
            down.connect("clicked", lambda _b, idx=i: self._move_order(idx, +1))
            row.add_suffix(up)
            row.add_suffix(down)

            self._attach_row_dnd(row)
            self._order_group.add(row)
            self._order_rows.append(row)

        # Keep draft layouts in sync immediately (so Save always sees latest order)
        self._apply_order_to_draft()

    def _attach_row_dnd(self, row: Adw.ActionRow) -> None:
        """Drag-and-drop reorder between camera rows."""
        # --- drag source ---
        drag = Gtk.DragSource()
        drag.set_actions(Gdk.DragAction.MOVE)

        def on_prepare(_src, _x, _y):
            try:
                idx = self._order_rows.index(row)
            except ValueError:
                return None
            # STRING content — DropTarget reads as Python str
            return Gdk.ContentProvider.new_for_value(str(idx))

        def on_drag_begin(src, _drag_obj):
            try:
                paintable = Gtk.WidgetPaintable.new(row)
                src.set_icon(paintable, 24, 16)
            except Exception:
                pass
            row.add_css_class("dim-label")

        def on_drag_end(_src, _drag, _delete):
            row.remove_css_class("dim-label")

        drag.connect("prepare", on_prepare)
        drag.connect("drag-begin", on_drag_begin)
        drag.connect("drag-end", on_drag_end)
        row.add_controller(drag)

        # --- drop target ---
        drop = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE)
        drop.set_preload(True)

        def on_drop(_target, value, _x, _y):
            try:
                if hasattr(value, "get_string"):
                    value = value.get_string()
                src_idx = int(value)
                dest_idx = self._order_rows.index(row)
            except (TypeError, ValueError):
                return False
            if src_idx == dest_idx:
                return True
            self._reorder_indices(src_idx, dest_idx)
            return True

        def on_enter(_target, _x, _y):
            row.add_css_class("accent")
            return Gdk.DragAction.MOVE

        def on_leave(_target):
            row.remove_css_class("accent")

        drop.connect("drop", on_drop)
        drop.connect("enter", on_enter)
        drop.connect("leave", on_leave)
        row.add_controller(drop)

    def _reorder_indices(self, src_idx: int, dest_idx: int) -> None:
        """Move item from src_idx onto dest_idx (drop target row).

        Moving down inserts *after* the target row; moving up inserts *before*.
        Do not adjust dest after pop when going down — that made down-by-one a no-op.
        """
        if (
            src_idx < 0
            or dest_idx < 0
            or src_idx >= len(self._layout_order)
            or dest_idx >= len(self._layout_order)
            or src_idx == dest_idx
        ):
            return
        item = self._layout_order.pop(src_idx)
        # After pop(src) with src < dest, the drop target sits at dest-1.
        # Inserting at original dest places the item *after* that target.
        # When src > dest, dest is unchanged and insert-before is correct.
        if src_idx < dest_idx:
            # insert after target row (index already correct as original dest)
            pass
        self._layout_order.insert(dest_idx, item)
        self._rebuild_order_rows()
        self._toast("Order updated — Save and Apply to use on grid")

    def _move_order(self, index: int, delta: int) -> None:
        j = index + delta
        if j < 0 or j >= len(self._layout_order):
            return
        self._layout_order[index], self._layout_order[j] = (
            self._layout_order[j],
            self._layout_order[index],
        )
        self._rebuild_order_rows()
        self._toast("Order updated — Save and Apply to use on grid")

    def _sort_order(self, mode: str) -> None:
        by_id = self._cam_by_id()

        def key_channel(cid: str) -> int:
            cam = by_id.get(cid) or {}
            try:
                n = int(cam.get("channel_main") or 0)
                return n // 100 if n >= 100 else n
            except (TypeError, ValueError):
                return 999

        def key_name(cid: str) -> str:
            cam = by_id.get(cid) or {}
            return str(cam.get("name") or cid).lower()

        before = list(self._layout_order)
        if mode == "name":
            self._layout_order = sorted(self._layout_order, key=key_name)
        else:
            self._layout_order = sorted(self._layout_order, key=key_channel)
        self._rebuild_order_rows()
        if self._layout_order == before:
            self._toast("Already in this order")
        else:
            self._toast("Sorted — Save and Apply to use on grid")

    def _apply_order_to_draft(self) -> None:
        """Write current _layout_order into draft cameras + layouts (for Save)."""
        by_id = self._cam_by_id()
        if not self._layout_order or not by_id:
            return
        ordered: list[dict] = []
        seen: set[str] = set()
        for cid in self._layout_order:
            if cid in by_id and cid not in seen:
                ordered.append(self._clean_cam(by_id[cid]))
                seen.add(cid)
        for cid, cam in by_id.items():
            if cid not in seen:
                ordered.append(self._clean_cam(cam))
                seen.add(cid)
                if cid not in self._layout_order:
                    self._layout_order.append(cid)
        self._draft["cameras"] = ordered
        cam_ids = [str(c.get("id")) for c in ordered if c.get("id")]
        layouts = list(self._draft.get("layouts") or [])
        if not layouts:
            self._draft["layouts"] = [
                {
                    "name": "All 4×4 (sub)",
                    "cols": 4,
                    "rows": 4,
                    "quality": "sub",
                    "cameras": cam_ids,
                }
            ]
            return
        lay0 = dict(layouts[0]) if isinstance(layouts[0], dict) else {}
        lay0["cameras"] = cam_ids
        layouts[0] = lay0
        rank = {cid: i for i, cid in enumerate(cam_ids)}
        for i in range(1, len(layouts)):
            if not isinstance(layouts[i], dict):
                continue
            lay = dict(layouts[i])
            old = [str(x) for x in (lay.get("cameras") or [])]
            lay["cameras"] = sorted(old, key=lambda x: rank.get(x, 999))
            layouts[i] = lay
        self._draft["layouts"] = layouts

    def _build_actions_page(self) -> None:
        page = Adw.PreferencesPage(title="Actions", icon_name="emblem-system-symbolic")
        page.add(self._make_save_group("Also available on the Cameras page"))

        group = Adw.PreferencesGroup(title="Tools")

        names_row = Adw.ActionRow(
            title="Load names from NVR (ISAPI)",
            subtitle="Requires HTTP access to the recorder (not only RTSP)",
        )
        names_btn = Gtk.Button(label="Sync names")
        names_btn.set_valign(Gtk.Align.CENTER)
        names_btn.connect("clicked", self._on_sync_names)
        names_row.add_suffix(names_btn)
        group.add(names_row)

        imp_row = Adw.ActionRow(
            title="Import from cctv-viewer snap",
            subtitle="Overwrite host, password and camera list from snap config",
        )
        imp_btn = Gtk.Button(label="Import")
        imp_btn.set_valign(Gtk.Align.CENTER)
        imp_btn.connect("clicked", self._on_import_snap)
        imp_row.add_suffix(imp_btn)
        group.add(imp_row)

        open_row = Adw.ActionRow(
            title="Config file",
            subtitle=str(self.config.path),
        )
        open_btn = Gtk.Button(label="Copy path")
        open_btn.set_valign(Gtk.Align.CENTER)

        def _copy(*_):
            display = self.get_display()
            if display:
                display.get_clipboard().set(str(self.config.path))

        open_btn.connect("clicked", _copy)
        open_row.add_suffix(open_btn)
        group.add(open_row)

        page.add(group)
        self.add(page)

    # ---- collect / save ----

    def _collect(self) -> dict:
        # Never deepcopy draft cameras while they may still hold Gtk widget refs
        d: dict = {}
        for k, v in self._draft.items():
            if k == "cameras":
                continue
            try:
                d[k] = deepcopy(v)
            except Exception:
                d[k] = v

        d["host"] = self.host_row.get_text().strip()
        d["port"] = int(self.port_row.get_value())
        d["http_host"] = self.http_host_row.get_text().strip()
        d["http_port"] = int(self.http_port_row.get_value())
        scheme_item = self.http_scheme_row.get_selected_item()
        d["http_scheme"] = (
            scheme_item.get_string() if scheme_item is not None else "http"
        )
        d["user"] = self.user_row.get_text().strip()
        d["password"] = self.pass_row.get_text()
        d["path_template"] = self.path_row.get_text().strip() or d.get("path_template")

        rtsp = dict(d.get("rtsp") or {})
        rtsp["latency_ms"] = int(self.latency_row.get_value())
        rtsp["stagger_ms"] = int(self.stagger_row.get_value())
        rtsp["reconnect_delay_s"] = float(self.reconnect_row.get_value())
        proto_item = self.proto_row.get_selected_item()
        if proto_item is not None:
            rtsp["protocols"] = proto_item.get_string()
        rtsp["drop_on_latency"] = bool(self.drop_row.get_active())
        rtsp["mute_audio"] = bool(self.mute_row.get_active())
        rtsp["use_hardware_decode"] = bool(self.hw_row.get_active())
        d["rtsp"] = rtsp

        ui = dict(d.get("ui") or {})
        ui["show_labels"] = bool(self.labels_row.get_active())
        ui["pause_when_unmapped"] = bool(self.pause_unmap_row.get_active())
        ui["dark"] = bool(self.dark_row.get_active())
        ui["window_width"] = int(self.width_row.get_value())
        ui["window_height"] = int(self.height_row.get_value())
        d["ui"] = ui

        cameras: list[dict] = []
        by_collected: dict[str, dict] = {}
        for cam, row in self._cam_name_rows:
            c = self._clean_cam(cam)
            c["name"] = row.get_text().strip() or c.get("name") or c.get("id")
            btn = cam.get("_ui_enabled_btn")
            if btn is not None:
                try:
                    c["enabled"] = bool(btn.get_active())
                except Exception:
                    c["enabled"] = bool(c.get("enabled", True))
            cameras.append(c)
            if c.get("id"):
                by_collected[str(c["id"])] = c

        # Apply grid order from drag or arrows list
        if self._layout_order and by_collected:
            ordered: list[dict] = []
            for cid in self._layout_order:
                if cid in by_collected:
                    ordered.append(by_collected.pop(cid))
            ordered.extend(by_collected.values())
            cameras = ordered
            d["cameras"] = cameras
            cam_ids = [str(c["id"]) for c in cameras if c.get("id")]
            layouts = list(d.get("layouts") or [])
            if not layouts:
                layouts = [
                    {
                        "name": "All 4×4 (sub)",
                        "cols": 4,
                        "rows": 4,
                        "quality": "sub",
                        "cameras": cam_ids,
                    }
                ]
            else:
                lay0 = dict(layouts[0]) if isinstance(layouts[0], dict) else {}
                lay0["cameras"] = cam_ids
                layouts[0] = lay0
                rank = {cid: i for i, cid in enumerate(cam_ids)}
                for i in range(1, len(layouts)):
                    if not isinstance(layouts[i], dict):
                        continue
                    lay = dict(layouts[i])
                    old = [str(x) for x in (lay.get("cameras") or [])]
                    lay["cameras"] = sorted(old, key=lambda x: rank.get(x, 999))
                    layouts[i] = lay
            d["layouts"] = layouts
        elif cameras:
            d["cameras"] = cameras
        return d

    def _save(self, apply_reconnect: bool = True) -> None:
        try:
            data = self._collect()
            # Sanity: must be YAML-serializable
            import yaml as _yaml

            _yaml.safe_dump(data)
            self.config.data = data
            path = self.config.save()
            log.info("saved settings to %s", path)
        except Exception as e:
            log.exception("save failed")
            self._toast(f"Save failed: {type(e).__name__}: {e}")
            return
        if self.on_saved:
            try:
                self.on_saved(self.config, apply_reconnect)
            except Exception as e:
                log.exception("on_saved failed")
                self._toast(f"Saved file, but apply failed: {e}")
                return
        self._toast("Settings saved")
        self.close()

    def _on_sync_names(self, *_):
        import threading
        from copy import deepcopy

        self._toast("Syncing names from NVR…")
        data = deepcopy(self._collect())

        def worker():
            try:
                from .nvr import sync_names

                n, msg = sync_names(data)

                def apply():
                    self.config.data = data
                    self._draft = deepcopy(data)
                    if n > 0:
                        try:
                            self.config.save()
                        except Exception:
                            log.exception("save names failed")
                        name_by_id = {
                            c.get("id"): c.get("name")
                            for c in data.get("cameras") or []
                            if isinstance(c, dict)
                        }
                        for cam, row in self._cam_name_rows:
                            nm = name_by_id.get(cam.get("id"))
                            if nm:
                                row.set_text(str(nm))
                                cam["name"] = nm
                        if self.on_names_synced:
                            self.on_names_synced(self.config)
                    self._toast(msg)
                    return False

                GLib.idle_add(apply)
            except Exception as e:
                log.exception("name sync failed")
                err = str(e)
                GLib.idle_add(lambda: (self._toast(f"Name sync failed: {err}") or False))

        threading.Thread(target=worker, name="cctv-nvr-names", daemon=True).start()

    def _on_import_snap(self, *_):
        try:
            from .import_snap import import_from_snap

            imported = import_from_snap()
            if imported is None:
                self._toast("No snap config found")
                return
            ui = dict(self.config.data.get("ui") or {})
            rtsp = dict(self.config.data.get("rtsp") or {})
            self.config.data = imported.data
            self.config.data["ui"] = {**self.config.data.get("ui", {}), **ui}
            self.config.data["rtsp"] = {**self.config.data.get("rtsp", {}), **rtsp}
            self.config.save()
            if self.on_saved:
                self.on_saved(self.config, True)
            self._toast("Imported from snap")
            self.close()
        except Exception as e:
            log.exception("import failed")
            self._toast(f"Import failed: {e}")

    def _toast(self, message: str) -> None:
        try:
            toast = Adw.Toast(title=message)
            self.add_toast(toast)
        except Exception:
            log.info("toast: %s", message)
