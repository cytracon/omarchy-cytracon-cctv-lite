"""Per-camera RTSP pipeline.

VA-API decodes, vapostproc converts to BGRA on the GPU, and GTK shows that
BGRA frame. OpenGL is not in the path: on the RX 7600, glcolorconvert reads
the NV12 chroma plane as empty and the picture turns green.

Teardown never drops a pipeline from the bus callback or from the UI thread.
A worker calls set_state(NULL) and holds the last reference until that call
returns, so rtspsrc cannot free its connection while still reading it.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

import gi

gi.require_version("Gst", "1.0")
gi.require_version("Gtk", "4.0")
from gi.repository import GLib, GObject, Gst, Gtk  # noqa: E402

log = logging.getLogger("cctv_lite.player")

Gst.init(None)

_NULL_THREADS = 0
_NULL_LOCK = threading.Lock()


def _protocols_value(name: str) -> int:
    mapping = {
        "udp": 1,
        "udp-mcast": 2,
        "tcp": 4,
        "http": 8,
    }
    return mapping.get((name or "tcp").lower(), 4)


def _factory(name: str) -> bool:
    return Gst.ElementFactory.find(name) is not None


def select_mode(use_hw: bool, have_va: bool) -> str:
    """gpu: VA decode and VA colour convert to BGRA. cpu: software decode."""
    if use_hw and have_va:
        return "gpu"
    return "cpu"


def next_mode(mode: str) -> str | None:
    if mode == "gpu":
        return "cpu"
    return None


def mode_label(mode: str) -> str:
    if mode == "gpu":
        return "gpu"
    return "cpu"


def is_setup_failure(factory: str, message: str) -> bool:
    """True when VA-API cannot accept the stream.

    Ordinary RTSP drops must not permanently abandon the GPU path.
    """
    msg = (message or "").lower()
    if "not-negotiated" in msg or "not negotiated" in msg:
        return True
    if "no va display" in msg or "vaapi" in msg or "could not initialize" in msg:
        return True
    return False


def _structure_string(structure: Gst.Structure, field: str) -> str:
    try:
        if not structure.has_field(field):
            return ""
    except Exception:
        return ""
    try:
        value = structure.get_string(field)
    except Exception:
        return ""
    if isinstance(value, tuple):
        if len(value) == 2 and isinstance(value[0], bool):
            return str(value[1] or "") if value[0] else ""
        return str(value[-1] or "")
    return str(value or "")


def _structure_int(structure: Gst.Structure, field: str) -> int:
    try:
        got = structure.get_int(field)
    except Exception:
        got = None
    if isinstance(got, tuple) and len(got) == 2 and got[0]:
        return int(got[1])
    if isinstance(got, int):
        return got
    try:
        return int(structure.get_value(field))
    except Exception:
        return 0


def classify_rtp_caps(caps: Gst.Caps | None) -> tuple[str, str]:
    """Return (media, codec) for an RTP pad. Codec is H264, H265, or ""."""
    if caps is None or caps.get_size() == 0:
        return "", ""
    structure = caps.get_structure(0)
    try:
        if structure.get_name() != "application/x-rtp":
            return "", ""
    except Exception:
        return "", ""
    media = _structure_string(structure, "media").lower()
    codec = _structure_string(structure, "encoding-name").upper()
    if codec == "HEVC":
        codec = "H265"
    return media, codec


def fit_even(
    src_w: int,
    src_h: int,
    box_w: int,
    box_h: int,
    allow_upscale: bool,
) -> tuple[int, int]:
    """Even output size that fits the box and keeps the source aspect."""
    src_w -= src_w % 2
    src_h -= src_h % 2
    box_w -= box_w % 2
    box_h -= box_h % 2
    if src_w < 2 or src_h < 2 or box_w < 2 or box_h < 2:
        return 0, 0
    scale = min(box_w / src_w, box_h / src_h)
    if not allow_upscale:
        scale = min(scale, 1.0)
    width = int(src_w * scale)
    height = int(src_h * scale)
    width -= width % 2
    height -= height % 2
    width = min(max(2, width), box_w)
    height = min(max(2, height), box_h)
    return width, height


def _set_if(element: Gst.Element, name: str, value: Any) -> bool:
    try:
        spec = element.find_property(name)
        if spec is None or not (spec.flags & GObject.ParamFlags.WRITABLE):
            return False
        element.set_property(name, value)
        return True
    except Exception:
        return False


def gst_set_state_released(element: Gst.Element, state: Gst.State) -> int:
    """Run set_state on a worker so the GTK thread does not block.

    The call keeps the GIL. rtspsrc's streaming thread is in C while it
    reads, so it can be joined. Releasing the GIL around a ctypes call is
    not safe: the ctypes wrapper still touches Python error state.
    """
    return int(element.set_state(state))


def _make(factory: str, name: str) -> Gst.Element:
    element = Gst.ElementFactory.make(factory, name)
    if element is None:
        raise RuntimeError(f"{factory} is not available")
    return element


def pick_va_device() -> str | None:
    """Render node of the GPU with the most VRAM. On this desktop that is the RX 7600."""
    from pathlib import Path

    best_path = None
    best_vram = -1
    drm = Path("/sys/class/drm")
    if not drm.is_dir():
        return None
    for node in drm.glob("renderD*"):
        devnode = Path("/dev/dri") / node.name
        if not devnode.exists():
            continue
        try:
            vram = int((node / "device" / "mem_info_vram_total").read_text().strip())
        except Exception:
            vram = 0
        if vram > best_vram:
            best_vram = vram
            best_path = str(devnode)
    return best_path


class CameraPlayer:
    """Owns one RTSP pipeline and a Gtk.Picture for display."""

    def __init__(
        self,
        cam_id: str,
        name: str,
        url: str,
        rtsp_opts: dict[str, Any] | None = None,
        on_status: Callable[[str, str], None] | None = None,
    ):
        self.cam_id = cam_id
        self.name = name
        self.url = url
        self.opts = dict(rtsp_opts or {})
        self.on_status = on_status
        self._pipeline: Gst.Pipeline | None = None
        self._sink: Gst.Element | None = None
        self._src: Gst.Element | None = None
        self._decoder: Gst.Element | None = None
        self._scale_caps: Gst.Element | None = None
        self._bus = None
        self._bus_handler_id = None
        self._reconnect_id = None
        self._size_source = None
        self._null_thread: threading.Thread | None = None
        self._wanted_playing = False
        self._play_when_idle = False
        self._nulling = False
        self._closing = False
        self._linked = False
        self._status = "idle"
        self._epoch = 0
        self._codec_fails = 0
        self._waiters: list[Callable[[], None]] = []
        self._video_w = 0
        self._video_h = 0
        self._box_w = 0
        self._box_h = 0
        self._applied_size = (0, 0)
        self._detail = cam_id.endswith("-detail")
        have_va = _factory("vah264dec") and _factory("vapostproc") and _factory("vah265dec")
        self._mode = select_mode(
            bool(self.opts.get("use_hardware_decode", True)),
            have_va,
        )
        self._va_device = pick_va_device()
        self.picture = Gtk.Picture()
        self.picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.picture.set_hexpand(True)
        self.picture.set_vexpand(True)
        self.picture.add_css_class("camera-picture")
        self.picture.connect("notify::width", self._schedule_scale)
        self.picture.connect("notify::height", self._schedule_scale)
        if hasattr(Gtk, "GraphicsOffload"):
            off = Gtk.GraphicsOffload(child=self.picture)
            try:
                off.set_enabled(Gtk.GraphicsOffloadEnabled.ENABLED)
            except Exception:
                pass
            try:
                off.set_black_background(True)
            except Exception:
                pass
            off.set_hexpand(True)
            off.set_vexpand(True)
            self.widget: Gtk.Widget = off
        else:
            self.widget = self.picture
        self.widget.connect("notify::width", self._schedule_scale)
        self.widget.connect("notify::height", self._schedule_scale)

    @property
    def status(self) -> str:
        return self._status

    def _set_status(self, status: str) -> None:
        self._status = status
        if self.on_status:
            try:
                self.on_status(self.cam_id, status)
            except Exception:
                log.exception("status callback failed")

    def set_name(self, name: str) -> None:
        self.name = name

    def play(self) -> None:
        self._wanted_playing = True
        self._cancel_reconnect()
        if self._nulling:
            self._play_when_idle = True
            return
        if self._pipeline is None:
            try:
                self._build()
            except Exception as exc:
                log.error("build failed %s: %s", self.cam_id, exc)
                self._set_status(f"error: {exc}")
                self._schedule_reconnect()
                return
        self._closing = False
        ret = self._pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            self._set_status("error: start failed")
            self._schedule_reconnect()
        else:
            self._set_status("connecting")

    def pause(self) -> None:
        self._wanted_playing = False
        self._play_when_idle = False
        self._cancel_reconnect()
        if self._pipeline is not None and not self._nulling:
            self._pipeline.set_state(Gst.State.PAUSED)
            self._set_status("paused")

    def stop(self) -> None:
        """Release the RTSP session. The widget stays; play() opens a new pipeline."""
        self.suspend()

    def suspend(self, on_settled: Callable[[], None] | None = None) -> None:
        self._wanted_playing = False
        self._play_when_idle = False
        self._cancel_reconnect()
        self._set_status("stopped")
        if on_settled is not None:
            self._waiters.append(on_settled)
        if self._nulling:
            return
        if self._pipeline is None:
            GLib.idle_add(self._flush_waiters)
            return
        self._begin_null()

    def destroy(self, on_dead: Callable[[], None] | None = None) -> None:
        self.suspend(on_settled=on_dead)

    def hard_restart(self) -> None:
        self._wanted_playing = True
        self._cancel_reconnect()
        self._epoch += 1
        if self._nulling:
            self._play_when_idle = True
            return
        if self._pipeline is None:
            self.play()
            return
        self._play_when_idle = True
        self._begin_null()

    def join_null(self, timeout: float) -> None:
        thread = self._null_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)

    def set_url(self, url: str, autoplay: bool = True) -> None:
        self.url = url
        if autoplay and self._wanted_playing:
            self.hard_restart()

    def _cancel_reconnect(self) -> None:
        if self._reconnect_id is not None:
            try:
                GLib.source_remove(self._reconnect_id)
            except Exception:
                pass
            self._reconnect_id = None

    def _schedule_reconnect(self) -> None:
        if not self._wanted_playing:
            return
        self._cancel_reconnect()
        self._epoch += 1
        epoch = self._epoch
        delay = float(self.opts.get("reconnect_delay_s") or 3.0)
        delay_ms = max(1000, int(delay * 1000))

        def _re(_user=None, epoch=epoch):
            self._reconnect_id = None
            if epoch != self._epoch or not self._wanted_playing:
                return False
            log.info("reconnecting %s (%s)", self.cam_id, self._mode)
            self.hard_restart()
            return False

        self._set_status(f"reconnect in {delay:.0f}s")
        self._reconnect_id = GLib.timeout_add(delay_ms, _re)

    def _degrade(self) -> bool:
        nxt = next_mode(self._mode)
        if nxt is None:
            return False
        log.warning("%s decode path %s -> %s", self.cam_id, self._mode, nxt)
        self._mode = nxt
        return True

    def _begin_null(self) -> None:
        pipe = self._pipeline
        if pipe is None or self._nulling:
            return
        self._nulling = True
        self._closing = True
        self._linked = False
        self._disconnect_bus()
        self._clear_paintable()
        self._pipeline = None
        self._sink = None
        self._src = None
        self._decoder = None
        self._scale_caps = None
        self._video_w = 0
        self._video_h = 0
        self._applied_size = (0, 0)
        thread = threading.Thread(
            target=self._null_worker,
            args=(pipe,),
            name=f"cctv-null-{self.cam_id}",
            daemon=True,
        )
        self._null_thread = thread
        thread.start()

    def _null_worker(self, pipe: Gst.Pipeline) -> None:
        global _NULL_THREADS
        with _NULL_LOCK:
            _NULL_THREADS += 1
        try:
            gst_set_state_released(pipe, Gst.State.NULL)
        except Exception:
            log.exception("NULL failed for %s", self.cam_id)
        finally:
            del pipe
            with _NULL_LOCK:
                _NULL_THREADS -= 1
            GLib.idle_add(self._finish_null)

    def _finish_null(self) -> bool:
        self._nulling = False
        self._closing = False
        waiters = self._waiters
        self._waiters = []
        play = self._play_when_idle and self._wanted_playing
        self._play_when_idle = False
        for callback in waiters:
            try:
                callback()
            except Exception:
                log.exception("settle callback failed for %s", self.cam_id)
        if play:
            self.play()
        return False

    def _flush_waiters(self) -> bool:
        if self._nulling:
            return False
        waiters = self._waiters
        self._waiters = []
        for callback in waiters:
            try:
                callback()
            except Exception:
                log.exception("settle callback failed for %s", self.cam_id)
        return False

    def _clear_paintable(self) -> None:
        try:
            self.picture.set_paintable(None)
        except Exception:
            pass

    def _disconnect_bus(self) -> None:
        if self._bus is not None and self._bus_handler_id is not None:
            try:
                self._bus.disconnect(self._bus_handler_id)
            except Exception:
                pass
        self._bus_handler_id = None
        if self._bus is not None:
            try:
                self._bus.remove_signal_watch()
            except Exception:
                pass
        self._bus = None

    def _build(self) -> None:
        safe_id = "".join(ch if ch.isalnum() else "-" for ch in self.cam_id)
        pipeline = Gst.Pipeline.new(f"cam-{safe_id}")
        if pipeline is None:
            raise RuntimeError("could not create pipeline")
        src = _make("rtspsrc", f"src-{safe_id}")
        latency = int(self.opts.get("latency_ms") or 120)
        timeout = int(self.opts.get("timeout_us") or 5_000_000)
        _set_if(src, "location", self.url)
        _set_if(src, "latency", latency)
        _set_if(src, "drop-on-latency", bool(self.opts.get("drop_on_latency", True)))
        _set_if(src, "protocols", _protocols_value(str(self.opts.get("protocols") or "tcp")))
        _set_if(src, "timeout", timeout)
        _set_if(src, "tcp-timeout", timeout)
        _set_if(src, "do-rtsp-keep-alive", True)
        src.connect("pad-added", self._on_pad_added)

        sink = _make("gtk4paintablesink", f"sink-{safe_id}")
        _set_if(sink, "sync", False)
        _set_if(sink, "qos", False)
        _set_if(sink, "max-lateness", 20_000_000)
        # Scale is done upstream. The sink must not renegotiate over that.
        _set_if(sink, "reconfigure-on-window-resize", 0)
        self._attach_paintable(sink)

        pipeline.add(src)
        pipeline.add(sink)
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        handler = bus.connect("message", self._on_bus_message)
        self._pipeline = pipeline
        self._src = src
        self._sink = sink
        self._bus = bus
        self._bus_handler_id = handler
        self._linked = False
        self._closing = False
        self._set_status("ready")

    def _attach_paintable(self, sink: Gst.Element) -> None:
        paintable = sink.get_property("paintable")
        try:
            paintable.set_property("force-aspect-ratio", True)
        except Exception:
            pass
        try:
            paintable.set_property("use-scaling-filter", True)
            paintable.set_property("scaling-filter", 0)
        except Exception:
            pass
        self.picture.set_paintable(paintable)

    def _on_pad_added(self, _src, pad) -> None:
        if self._closing:
            return
        # Block before linking so the first segment cannot overtake caps.
        pad.add_probe(Gst.PadProbeType.BLOCK_DOWNSTREAM, self._on_blocked_pad)

    def _on_blocked_pad(self, pad, _info) -> Gst.PadProbeReturn:
        caps = pad.get_current_caps()
        if caps is None:
            return Gst.PadProbeReturn.OK
        self._handle_pad(pad, caps)
        return Gst.PadProbeReturn.REMOVE

    def _handle_pad(self, pad, caps: Gst.Caps) -> None:
        if self._closing or self._pipeline is None:
            return
        media, codec = classify_rtp_caps(caps)
        if media == "audio":
            self._link_audio(pad)
            return
        if self._linked or media != "video":
            return
        self._linked = True
        if codec not in ("H264", "H265"):
            log.warning("%s unsupported codec %r", self.cam_id, codec or "?")
            GLib.idle_add(self._fail_codec, codec or "?")
            return
        try:
            chain = self._build_chain(codec)
        except Exception as exc:
            log.error("chain %s: %s", self.cam_id, exc)
            GLib.idle_add(self._fail_setup, str(exc))
            return
        for element in chain:
            self._pipeline.add(element)
        sink_pad = chain[0].get_static_pad("sink")
        if pad.link(sink_pad) != Gst.PadLinkReturn.OK:
            log.error("RTP pad link failed for %s", self.cam_id)
            GLib.idle_add(self._fail_setup, "could not link RTP pad")
            return
        for left, right in zip(chain, chain[1:]):
            if not left.link(right):
                log.error(
                    "link failed %s: %s -> %s",
                    self.cam_id,
                    left.get_factory().get_name() if left.get_factory() else "?",
                    right.get_factory().get_name() if right.get_factory() else "?",
                )
                GLib.idle_add(self._fail_setup, "could not link decoder")
                return
        if self._sink is None or not chain[-1].link(self._sink):
            log.error("sink link failed for %s", self.cam_id)
            GLib.idle_add(self._fail_setup, "could not link video sink")
            return
        for element in chain:
            element.sync_state_with_parent()
        self._linked = True
        if self._decoder is not None:
            src = self._decoder.get_static_pad("src")
            if src is not None:
                src.add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, self._on_decoder_event)

    def _link_audio(self, pad) -> None:
        if self._pipeline is None:
            return
        try:
            fake = _make("fakesink", None)
        except RuntimeError:
            return
        _set_if(fake, "sync", False)
        _set_if(fake, "async", False)
        self._pipeline.add(fake)
        sink_pad = fake.get_static_pad("sink")
        if pad.link(sink_pad) == Gst.PadLinkReturn.OK:
            fake.sync_state_with_parent()

    def _build_chain(self, codec: str) -> list[Gst.Element]:
        safe_id = "".join(ch if ch.isalnum() else "-" for ch in self.cam_id)
        if codec == "H265":
            depay_name, parse_name = "rtph265depay", "h265parse"
            hw_name, sw_name = "vah265dec", "avdec_h265"
        else:
            depay_name, parse_name = "rtph264depay", "h264parse"
            hw_name, sw_name = "vah264dec", "avdec_h264"

        depay = _make(depay_name, f"depay-{safe_id}")
        parse = _make(parse_name, f"parse-{safe_id}")
        _set_if(parse, "config-interval", -1)

        mode = self._mode
        if mode == "gpu" and not _factory(hw_name):
            mode = "cpu"
            self._mode = mode

        scale = _make("capsfilter", f"scale-{safe_id}")
        scale.set_property("caps", Gst.Caps.from_string("video/x-raw,format=BGRA"))
        self._scale_caps = scale

        if mode == "gpu":
            decoder = _make(hw_name, f"dec-{safe_id}")
            if self._va_device:
                _set_if(decoder, "device-path", self._va_device)
            post = _make("vapostproc", f"post-{safe_id}")
            _set_if(post, "qos", False)
            self._decoder = decoder
            return [depay, parse, decoder, post, scale]

        decoder = _make(sw_name, f"dec-{safe_id}")
        scale_el = _make("videoscale", f"vscale-{safe_id}")
        _set_if(scale_el, "method", 3 if self._detail else 1)
        convert = _make("videoconvert", f"vconv-{safe_id}")
        self._decoder = decoder
        return [depay, parse, decoder, scale_el, convert, scale]

    def _on_decoder_event(self, pad, info) -> Gst.PadProbeReturn:
        event = info.get_event()
        if event is not None and event.type == Gst.EventType.CAPS:
            caps = pad.get_current_caps()
            if caps is not None and caps.get_size():
                structure = caps.get_structure(0)
                width = _structure_int(structure, "width")
                height = _structure_int(structure, "height")
                if width > 1 and height > 1:
                    GLib.idle_add(self._note_video_size, width, height)
        return Gst.PadProbeReturn.OK

    def _note_video_size(self, width: int, height: int) -> bool:
        self._video_w = width
        self._video_h = height
        self._apply_scale()
        return False

    def _fail_codec(self, codec: str) -> bool:
        self._codec_fails += 1
        if self._codec_fails >= 3:
            self._wanted_playing = False
            self._set_status(f"error: unsupported {codec}")
            return False
        self._set_status(f"error: unsupported {codec}")
        self._schedule_reconnect()
        return False

    def _fail_setup(self, message: str) -> bool:
        if self._degrade():
            self._set_status(f"error: {message}")
            self._schedule_reconnect()
        else:
            self._set_status(f"error: {message}")
            self._schedule_reconnect()
        return False

    def _schedule_scale(self, *_args) -> None:
        if self._size_source is not None:
            return
        self._size_source = GLib.timeout_add(80, self._apply_widget_size)

    def _apply_widget_size(self) -> bool:
        self._size_source = None
        try:
            scale = max(1, int(self.picture.get_scale_factor()))
            width = max(0, int(self.picture.get_width()) * scale)
            height = max(0, int(self.picture.get_height()) * scale)
        except Exception:
            return False
        if width <= 1 or height <= 1:
            return False
        self._box_w = width
        self._box_h = height
        self._apply_scale()
        return False

    def _apply_scale(self) -> None:
        # vapostproc turns the frame green when width and height are part of
        # the first BGRA negotiation. GTK scales the native frame.
        return

    def _log_negotiated_caps(self) -> None:
        sink = self._sink
        if sink is None:
            return
        try:
            pad = sink.get_static_pad("sink")
            caps = pad.get_current_caps() if pad is not None else None
            device = ""
            if self._decoder is not None:
                try:
                    device = str(self._decoder.get_property("device-path") or "")
                except Exception:
                    device = ""
            log.info(
                "%s mode=%s device=%s video=%sx%s caps=%s",
                self.cam_id,
                self._mode,
                device or "-",
                self._video_w,
                self._video_h,
                caps.to_string() if caps is not None else "none",
            )
        except Exception:
            log.exception("caps log failed for %s", self.cam_id)

    def _on_bus_message(self, _bus, message) -> None:
        if self._closing or self._pipeline is None:
            return
        kind = message.type
        if kind == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            text = err.message if err else "unknown"
            factory = ""
            source = message.src
            if source is not None:
                try:
                    made = source.get_factory()
                    factory = made.get_name() if made is not None else ""
                except Exception:
                    factory = ""
            log.warning("%s error: %s (%s) [%s]", self.cam_id, text, debug, factory)
            short = text
            if "Could not read from resource" in text or "timed out" in text.lower():
                short = "stream lost — reconnecting"
            if is_setup_failure(factory, text):
                self._degrade()
            self._set_status(f"error: {short}")
            if self._wanted_playing:
                self._schedule_reconnect()
        elif kind == Gst.MessageType.EOS:
            log.info("%s EOS — reconnect", self.cam_id)
            if self._wanted_playing:
                self._schedule_reconnect()
        elif kind == Gst.MessageType.STATE_CHANGED and message.src == self._pipeline:
            _old, new, _pending = message.parse_state_changed()
            if new == Gst.State.PLAYING:
                self._set_status(f"playing · {mode_label(self._mode)}")
                self._schedule_scale()
                self._log_negotiated_caps()
            elif new == Gst.State.PAUSED and self._wanted_playing:
                self._set_status("buffering")
        elif kind == Gst.MessageType.BUFFERING and self._wanted_playing:
            percent = message.parse_buffering()
            if percent < 100:
                self._set_status(f"buffering {percent}%")
            else:
                self._set_status(f"playing · {mode_label(self._mode)}")
