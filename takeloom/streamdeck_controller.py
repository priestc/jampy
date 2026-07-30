"""Optional StreamDeck integration for recording session controls."""

from __future__ import annotations

import threading
from typing import Callable

try:
    from StreamDeck.DeviceManager import DeviceManager
    from StreamDeck.ImageHelpers import PILHelper
    from StreamDeck.Devices.StreamDeck import DialEventType
    _HAVE_STREAMDECK = True
except ImportError:
    _HAVE_STREAMDECK = False

try:
    from PIL import ImageDraw, ImageFont
    _HAVE_PIL = True
except ImportError:
    _HAVE_PIL = False


def _skip_hidapi_exit_crash() -> None:
    """python-elgato-streamdeck registers libhidapi's hid_exit() via atexit the
    first time it opens the HID transport (StreamDeck/Transport/LibUSBHIDAPI.py).
    On recent macOS (Apple Silicon, pointer authentication) calling hid_exit()
    during Python's interpreter-shutdown atexit phase reliably crashes with
    SIGTRAP inside IOHIDManagerUnscheduleFromRunLoop — by that point the run
    loop the HID devices were scheduled on is already gone. We already close
    our own device handle in disconnect(); skip the library's redundant
    global teardown call so the process exits cleanly instead (the OS
    reclaims the HID subsystem's handles on process exit regardless)."""
    try:
        import atexit
        from StreamDeck.Transport.LibUSBHIDAPI import LibUSBHIDAPI
        hidapi = LibUSBHIDAPI.Library.HIDAPI_INSTANCE
        if hidapi is not None:
            atexit.unregister(hidapi.hid_exit)
    except Exception:
        pass


def _probe_deck(deck) -> tuple[str, str] | None:
    """Read an already-open deck's stable serial number + model name (the
    (id, label) shape used throughout this module and by list_streamdecks()).
    Serial number isn't available before open() — caller owns open()/close().
    Returns None if either read fails."""
    try:
        serial = deck.get_serial_number()
        deck_type = deck.deck_type()
    except Exception:
        return None
    label = f"{deck_type} ({serial[-4:]})" if len(serial) >= 4 else deck_type
    return serial, label


# HID transports enforce exclusive access, so if a StreamDeckController in
# this same process already has a device open (e.g. the Record tab's
# persistent connection), a second open() from list_streamdecks() below
# would fail and silently drop that device from the settings dropdown —
# exactly the "Stream Deck shows as None" bug. deck.id() (the HID device
# path) is readable straight from enumerate() results with no open() call,
# so it's a safe key for recognizing "this is a device I already have
# open" and serving its cached (serial, label) instead of re-opening it.
_open_by_id: dict[str, tuple[str, str]] = {}
_open_by_id_lock = threading.Lock()


def list_streamdecks() -> list[tuple[str, str]]:
    """Enumerate every currently attached Stream Deck as (serial_number,
    label) pairs, for a settings dropdown. Each device not already held
    open by this process is briefly opened to read its serial (not
    available before open()) and closed again — this is a one-off listing
    call, not a live connection."""
    if not _HAVE_STREAMDECK:
        return []
    try:
        decks = DeviceManager().enumerate()
        _skip_hidapi_exit_crash()
    except Exception:
        return []
    results = []
    for deck in decks:
        try:
            device_key = deck.id()
        except Exception:
            device_key = None
        with _open_by_id_lock:
            cached = _open_by_id.get(device_key) if device_key is not None else None
        if cached is not None:
            results.append(cached)
            continue
        try:
            deck.open()
        except Exception:
            continue
        try:
            probe = _probe_deck(deck)
            if probe is not None:
                results.append(probe)
        finally:
            try:
                deck.close()
            except Exception:
                pass
    return results


# Button tuple: (key_index, icon_name, label, key_char, active_state_name, active_color, dim_color)
# active_state_name=None → always shown in active color.
_INSPIRATION_BUTTONS: list[tuple] = [
    (0, None,     None,   " ", None, None,            None),           # play/pause — rendered by update_inspiration
    (1, "skip",   "Skip", "s", None, (0,  120, 200),  (0,  120, 200)),
    (2, "quit",   "Quit", "q", None, (200,  30,  30), (200, 30,  30)),
]

_INSPIRATION_RESTART_BUTTON: tuple = (3, "prev", "Restart", "b", None, (255, 140, 0), (255, 140, 0))

_INSPIRATION_VOLUME_BUTTONS: list[tuple] = [
    (4, "vol_dn", "Vol -", "l", None, (0,  120, 200), (0,  120, 200)),
    (5, "vol_up", "Vol +", "u", None, (0,  120, 200), (0,  120, 200)),
]

# Shared layout for every recording context (Tk UI, headless `takeloom
# server`, and the CLI) — one table, one set of semantics, so the physical
# deck behaves identically no matter which one is driving it. `active_state`
# here is a recording *phase* ("idle"/"waiting"/"recording"); Next and Sound
# Check are always available so their active_state is None, while Restart
# only means anything mid-take and is dimmed otherwise.
_RECORDING_TOGGLE: tuple = (0, None, None, "r", None, None, None)  # rendered by update_recording_page
_RECORDING_NEXT: tuple = (1, "skip", "Next", "n", None, (0, 160, 220), (0, 160, 220))
_RECORDING_RESTART: tuple = (2, "prev", "Restart", "b", "recording", (255, 140, 0), (55, 35, 10))
_RECORDING_SOUND_CHECK_KEY_INDEX = 3
_RECORDING_SOUND_CHECK: tuple = (
    _RECORDING_SOUND_CHECK_KEY_INDEX, None, None, "c", None, None, None,
)  # rendered by update_recording_page

_RECORDING_BUTTONS: list[tuple] = [_RECORDING_TOGGLE, _RECORDING_NEXT, _RECORDING_RESTART, _RECORDING_SOUND_CHECK]

_RECORDING_VOLUME_BUTTONS: list[tuple] = [
    (4, "vol_dn",   "Vol -",   "l", None, (0,   120, 200), (0,   120, 200)),
    (5, "vol_up",   "Vol +",   "u", None, (0,   120, 200), (0,   120, 200)),
    (6, "takes_dn", "Takes -", "[", None, (120,   0, 200), (120,   0, 200)),
    (7, "takes_up", "Takes +", "]", None, (120,   0, 200), (120,   0, 200)),
]

_SESSION_DIAL_MAP: dict[int, tuple[str, str, str]] = {
    0: ("l", "u", "Backing\nVol"),
    1: ("[", "]", "Takes\nVol"),
}

_INSPIRATION_DIAL_MAP: dict[int, tuple[str, str, str]] = {
    0: ("l", "u", "Volume"),
}

_FONT_PATHS = (
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)


def _load_font(size: int):
    for path in _FONT_PATHS:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _draw_icon(draw: "ImageDraw.ImageDraw", icon: str, cx: int, cy: int, size: int) -> None:
    """Draw a white icon centered at (cx, cy) within a size×size bounding box."""
    r = size // 2
    q = size // 4
    lw = max(2, size // 10)
    f = "white"

    if icon == "record":
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=f)

    elif icon == "play":
        draw.polygon([(cx - r, cy - r), (cx + r, cy), (cx - r, cy + r)], fill=f)

    elif icon == "pause":
        bw = max(3, r // 2)
        draw.rectangle([cx - bw - 2, cy - r, cx - 2, cy + r], fill=f)
        draw.rectangle([cx + 2, cy - r, cx + bw + 2, cy + r], fill=f)

    elif icon == "stop":
        draw.rectangle([cx - r, cy - r, cx + r, cy + r], fill=f)

    elif icon == "prev":       # |◀  (back to start / restart)
        bw = max(2, r // 3)
        draw.rectangle([cx - r, cy - r, cx - r + bw, cy + r], fill=f)
        draw.polygon([(cx + r, cy - r), (cx - r + bw * 2, cy), (cx + r, cy + r)], fill=f)

    elif icon == "skip":       # ▶|  (skip / next track)
        bw = max(2, r // 3)
        draw.polygon([(cx - r, cy - r), (cx + r - bw * 2, cy), (cx - r, cy + r)], fill=f)
        draw.rectangle([cx + r - bw, cy - r, cx + r, cy + r], fill=f)

    elif icon == "quit":       # ✕
        draw.line([cx - r, cy - r, cx + r, cy + r], fill=f, width=lw)
        draw.line([cx + r, cy - r, cx - r, cy + r], fill=f, width=lw)

    elif icon == "soundcheck":  # ✓
        draw.line([cx - r, cy, cx - q // 2, cy + r // 2], fill=f, width=lw)
        draw.line([cx - q // 2, cy + r // 2, cx + r, cy - r], fill=f, width=lw)

    elif icon in ("vol_dn", "vol_up"):
        # Speaker box + flared cone, then −/+
        bx = cx - r + r // 4      # right edge of box
        draw.rectangle([cx - r, cy - q, bx, cy + q], fill=f)
        draw.polygon([(bx, cy - q), (cx + q, cy - r), (cx + q, cy + r), (bx, cy + q)], fill=f)
        # − or + to the right of the cone
        sx1, sx2 = cx + q + 2, cx + r
        sy = cy
        draw.line([sx1, sy, sx2, sy], fill=f, width=lw)
        if icon == "vol_up":
            mx = (sx1 + sx2) // 2
            draw.line([mx, sy - q // 2, mx, sy + q // 2], fill=f, width=lw)

    elif icon in ("takes_dn", "takes_up"):
        # Three stacked horizontal bars (like track lanes in a DAW)
        bh = max(2, size // 10)
        for y_off, w in [(-q, r), (0, r * 3 // 4), (q, r // 2)]:
            draw.rectangle([cx - w, cy + y_off - bh, cx + w, cy + y_off + bh], fill=f)
        # − or + below the bars
        by = cy + r - bh
        bx1, bx2 = cx - r // 2, cx + r // 2
        draw.line([bx1, by, bx2, by], fill=f, width=lw)
        if icon == "takes_up":
            draw.line([cx, by - lw * 2, cx, by + lw * 2], fill=f, width=lw)


class StreamDeckController:
    """Manages an Elgato Stream Deck for recording session button display."""

    def __init__(self) -> None:
        self._deck = None
        self._device_key: str | None = None
        self._has_dials = False
        self._buttons: list[tuple] = []
        self._dial_map: dict[int, tuple[str, str, str]] = dict(_SESSION_DIAL_MAP)
        self._lock = threading.Lock()
        # Only set when a device was actually found but failed to open/
        # configure — "no device plugged in" (the common case for anyone
        # without a Stream Deck) deliberately leaves this unset, so callers
        # can log a real failure without nagging every user who's never
        # owned one.
        self.last_error: str | None = None

    @property
    def connected(self) -> bool:
        return self._deck is not None

    def connect(self, key_callback: Callable[[str], None], device_id: str = "") -> bool:
        """Open the Stream Deck whose serial number matches device_id (from
        list_streamdecks(), stored as StudioConfig.streamdeck_id). Returns
        False immediately with no hardware probing at all if device_id is
        empty — the app never auto-connects to "whichever Stream Deck
        happens to be plugged in"; the user selects one explicitly in
        Recording Devices settings."""
        self.last_error = None
        if not device_id:
            return False
        if not _HAVE_STREAMDECK or not _HAVE_PIL:
            return False
        try:
            decks = DeviceManager().enumerate()
            _skip_hidapi_exit_crash()
            target = None
            target_probe = None
            for deck in decks:
                try:
                    deck.open()
                except Exception:
                    continue
                probe = _probe_deck(deck)
                if probe is not None and probe[0] == device_id:
                    target = deck
                    target_probe = probe
                    break
                try:
                    deck.close()
                except Exception:
                    pass
            if target is None:
                # Unlike "no device_id configured" above, the user *did*
                # select a specific Stream Deck — not finding it now is
                # worth surfacing (e.g. it's unplugged), not staying silent.
                self.last_error = f"configured Stream Deck (serial ending {device_id[-4:]}) not found among connected devices"
                return False

            self._deck = target
            try:
                self._device_key = target.id()
            except Exception:
                self._device_key = None
            if self._device_key is not None:
                with _open_by_id_lock:
                    _open_by_id[self._device_key] = target_probe
            self._deck.reset()
            self._deck.set_brightness(70)
            self._key_callback = key_callback

            self._has_dials = getattr(self._deck, 'DIAL_COUNT', 0) > 0
            # No layout drawn yet — every caller picks one (use_recording_
            # layout()/use_inspiration_layout()) immediately after connect()
            # succeeds.
            self._buttons = []

            self._deck.set_key_callback(self._on_key_change)
            if self._has_dials:
                self._deck.set_dial_callback(self._on_dial_change)

            return True
        except Exception as e:
            self._deck = None
            if self._device_key is not None:
                with _open_by_id_lock:
                    _open_by_id.pop(self._device_key, None)
            self._device_key = None
            self.last_error = f"{type(e).__name__}: {e}"
            return False

    def use_inspiration_layout(self, recording: bool = False) -> None:
        """Switch to inspiration mode button layout and dial map."""
        self._buttons = list(_INSPIRATION_BUTTONS)
        if recording:
            self._buttons.append(_INSPIRATION_RESTART_BUTTON)
        if not self._has_dials:
            self._buttons += _INSPIRATION_VOLUME_BUTTONS
        self._dial_map = dict(_INSPIRATION_DIAL_MAP)

    def use_recording_layout(self) -> None:
        """Switch to the shared recording-context layout — used identically
        by the Tk UI, headless `takeloom server`, and the CLI: a Record/
        Unpause/Stop toggle, Next Track (always available — advances to the
        next untaken track, discarding an in-progress take first if one's
        active), Restart (dimmed unless actually recording), a Sound Check
        toggle, and volume controls (dials if available, else buttons).
        Buttons whose index doesn't fit the connected deck are dropped
        rather than drawn out of range (e.g. the 6-key Mini)."""
        self._buttons = list(_RECORDING_BUTTONS)
        if not self._has_dials:
            self._buttons += _RECORDING_VOLUME_BUTTONS
        self._dial_map = dict(_SESSION_DIAL_MAP)
        if not self.connected:
            return
        key_count = getattr(self._deck, "KEY_COUNT", 0)
        self._buttons = [btn for btn in self._buttons if btn[0] < key_count]
        with self._lock:
            # Blank every key this layout doesn't use — on a dial deck (e.g. the
            # Stream Deck Plus) that's most of them, and left untouched they'd
            # keep showing the device's own default/branded image instead of
            # looking deliberately off.
            used_indices = {btn[0] for btn in self._buttons}
            for idx in range(key_count):
                if idx not in used_indices:
                    self._deck.set_key_image(idx, self._make_key_image(None, None, (0, 0, 0)))
            for btn in self._buttons:
                idx, icon, label, _key, active_state, active_color, dim_color = btn
                if idx in (0, _RECORDING_SOUND_CHECK_KEY_INDEX) or icon is None:
                    continue
                # Freshly applying the layout with no phase known yet — treat
                # as "idle" (dimmed) until the first update_recording_page().
                color = dim_color if active_state is not None else active_color
                self._deck.set_key_image(idx, self._make_key_image(icon, label, color))
            if self._has_dials:
                self._update_touchscreen()

    def update_recording_page(self, phase: str, sound_check_phase: str = "idle") -> None:
        """Refresh the Record/Unpause/Stop toggle, the Sound Check toggle,
        and dim/light Next/Restart/volume for the current phase. Each toggle
        is dimmed while the other holds the audio/camera hardware — the two
        are mutually exclusive at the backend level. `phase` is one of
        "idle"/"waiting"/"recording"; `sound_check_phase` is
        "idle"/"recording". Shared verbatim by the Tk UI, headless server,
        and CLI drivers."""
        if not self.connected:
            return
        record_icon, record_label, record_color = {
            "idle":      ("record", "Start Recording", (0,   200, 0)),
            "waiting":   ("play",   "Unpause",         (230, 160, 0)),
            "recording": ("stop",   "Stop Recording",  (230, 60,  60)),
        }[phase]
        if sound_check_phase == "recording":
            record_color = (40, 40, 40)
        check_icon, check_label, check_color = {
            "idle":      ("soundcheck", "Sound Check", (0,   160, 200)),
            "recording": ("stop",       "Stop",        (230, 60,  60)),
        }[sound_check_phase]
        if phase != "idle":
            check_color = (40, 40, 40)
        with self._lock:
            self._deck.set_key_image(0, self._make_key_image(record_icon, record_label, record_color))
            if any(btn[0] == _RECORDING_SOUND_CHECK_KEY_INDEX for btn in self._buttons):
                self._deck.set_key_image(
                    _RECORDING_SOUND_CHECK_KEY_INDEX, self._make_key_image(check_icon, check_label, check_color)
                )
            for btn in self._buttons:
                idx, icon, label, _key, active_state, active_color, dim_color = btn
                if idx in (0, _RECORDING_SOUND_CHECK_KEY_INDEX) or icon is None:
                    continue
                color = active_color if (active_state is None or active_state == phase) else dim_color
                self._deck.set_key_image(idx, self._make_key_image(icon, label, color))
            if self._has_dials:
                self._update_touchscreen()

    def _on_key_change(self, deck, key_index: int, pressed: bool) -> None:
        if not pressed:
            return
        for idx, _icon, _label, key_char, *_ in self._buttons:
            if idx == key_index:
                self._key_callback(key_char)
                return

    def _on_dial_change(self, deck, dial_index: int, event, value) -> None:
        if not _HAVE_STREAMDECK:
            return
        if event != DialEventType.TURN:
            return
        mapping = self._dial_map.get(dial_index)
        if mapping is None:
            return
        ccw_key, cw_key, _ = mapping
        key = cw_key if value > 0 else ccw_key
        for _ in range(abs(value)):
            self._key_callback(key)

    def update_inspiration(self, is_playing: bool, track_name: str | None = None) -> None:
        """Refresh buttons and touchscreen for the current inspiration mode state."""
        if not self.connected:
            return
        with self._lock:
            icon = "pause" if is_playing else "play"
            label = "Pause" if is_playing else "Play"
            color = (200, 130, 0) if is_playing else (0, 180, 0)
            self._deck.set_key_image(0, self._make_key_image(icon, label, color))
            for btn in self._buttons:
                idx, icon, label, _key, _state, active_color, _dim = btn
                if idx == 0 or icon is None:
                    continue
                self._deck.set_key_image(idx, self._make_key_image(icon, label, active_color))
            if self._has_dials:
                self._update_touchscreen(track_name)

    def update_record_page(self, phase: str, sound_check_phase: str = "idle") -> None:
        """Refresh the Record/Unpause/Stop toggle, the Sound Check toggle (if
        the connected deck has room for one), and static buttons for the UI
        Record page. Each toggle is dimmed while the other holds the audio/
        camera hardware — the two are mutually exclusive at the backend level.
        `phase` is one of "idle"/"waiting"/"recording"; `sound_check_phase`
        is "idle"/"recording"."""
        if not self.connected:
            return
        record_icon, record_label, record_color = {
            "idle":      ("record", "Record",  (0,   200, 0)),
            "waiting":   ("play",   "Unpause",  (230, 160, 0)),
            "recording": ("stop",   "Stop",     (230, 60,  60)),
        }[phase]
        if sound_check_phase == "recording":
            record_color = (40, 40, 40)
        check_icon, check_label, check_color = {
            "idle":      ("soundcheck", "Sound Check", (0,   160, 200)),
            "recording": ("stop",       "Stop",        (230, 60,  60)),
        }[sound_check_phase]
        if phase != "idle":
            check_color = (40, 40, 40)
        with self._lock:
            self._deck.set_key_image(0, self._make_key_image(record_icon, record_label, record_color))
            if any(btn[0] == _SOUND_CHECK_KEY_INDEX for btn in self._buttons):
                self._deck.set_key_image(
                    _SOUND_CHECK_KEY_INDEX, self._make_key_image(check_icon, check_label, check_color)
                )
            for btn in self._buttons:
                idx, icon, label, _key, _state, active_color, _dim = btn
                if idx in (0, _SOUND_CHECK_KEY_INDEX) or icon is None:
                    continue
                self._deck.set_key_image(idx, self._make_key_image(icon, label, active_color))
            if self._has_dials:
                self._update_touchscreen()

    def disconnect(self) -> None:
        if self._deck:
            if self._device_key is not None:
                with _open_by_id_lock:
                    _open_by_id.pop(self._device_key, None)
            try:
                self._deck.reset()
                self._deck.close()
            except Exception:
                pass
            self._deck = None
            self._device_key = None

    def _make_key_image(self, icon: str | None, label: str | None, color: tuple) -> bytes:
        img = PILHelper.create_image(self._deck, background=color)
        draw = ImageDraw.Draw(img)
        w, h = img.size
        if icon:
            icon_size = int(h * 0.42)
            icon_cy = int(h * 0.38)
            _draw_icon(draw, icon, w // 2, icon_cy, icon_size)
        if label:
            label_y = h - int(h * 0.14)
            draw.text((w // 2, label_y), label, anchor="mm", font=_load_font(11), fill="white")
        return PILHelper.to_native_format(self._deck, img)

    def _update_touchscreen(self, track_name: str | None = None) -> None:
        try:
            img = PILHelper.create_touchscreen_image(self._deck, background="black")
            draw = ImageDraw.Draw(img)
            w, h = img.size  # 800×100
            section_w = w // 4
            if track_name:
                draw.text((w // 2, h // 3), track_name, anchor="mm",
                          font=_load_font(20), fill="white")
                label_y = h * 3 // 4
            else:
                label_y = h // 2
            for dial_idx, (_ccw, _cw, label) in self._dial_map.items():
                x = section_w * dial_idx + section_w // 2
                draw.text((x, label_y), label, anchor="mm",
                          font=_load_font(14), fill=(160, 160, 160))
            img_bytes = PILHelper.to_native_touchscreen_format(self._deck, img)
            self._deck.set_touchscreen_image(img_bytes, x_pos=0, y_pos=0, width=w, height=h)
        except Exception:
            import traceback
            traceback.print_exc()
