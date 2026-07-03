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


# Button tuple: (key_index, icon_name, label, key_char, active_state_name, active_color, dim_color)
# active_state_name=None → always shown in active color.
_SESSION_BUTTONS: list[tuple] = [
    (0, "record", "Record",   "r", "WAITING",       (0,   200,   0), (20,  60, 20)),
    (1, "prev",   "Back",     "b", "PLAYING",        (255, 140,   0), (60,  40, 10)),
    (2, "stop",   "End Song", "e", "PLAYING",        (230, 200,   0), (60,  50,  0)),
    (3, "skip",   "Next",     "n", "BETWEEN_TRACKS", (0,   180, 255), ( 0,  45, 64)),
    (4, "quit",   "Quit",     "q", None,             (200,  30,  30), (200, 30, 30)),
]

_VOLUME_BUTTONS: list[tuple] = [
    (5, "vol_dn",    "Vol -",   "l", None, (0,   120, 200), (0,   120, 200)),
    (6, "vol_up",    "Vol +",   "u", None, (0,   120, 200), (0,   120, 200)),
    (7, "takes_dn",  "Takes -", "[", None, (120,   0, 200), (120,   0, 200)),
    (8, "takes_up",  "Takes +", "]", None, (120,   0, 200), (120,   0, 200)),
]

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
        self._has_dials = False
        self._buttons: list[tuple] = []
        self._dial_map: dict[int, tuple[str, str, str]] = dict(_SESSION_DIAL_MAP)
        self._lock = threading.Lock()

    @property
    def connected(self) -> bool:
        return self._deck is not None

    def connect(self, key_callback: Callable[[str], None]) -> bool:
        """Open the first available StreamDeck. Returns True if connected."""
        if not _HAVE_STREAMDECK or not _HAVE_PIL:
            return False
        try:
            decks = DeviceManager().enumerate()
            if not decks:
                return False
            self._deck = decks[0]
            self._deck.open()
            self._deck.reset()
            self._deck.set_brightness(70)
            self._key_callback = key_callback

            self._has_dials = getattr(self._deck, 'DIAL_COUNT', 0) > 0
            self._buttons = list(_SESSION_BUTTONS)
            if not self._has_dials:
                self._buttons += _VOLUME_BUTTONS

            self._deck.set_key_callback(self._on_key_change)
            if self._has_dials:
                self._deck.set_dial_callback(self._on_dial_change)

            return True
        except Exception:
            self._deck = None
            return False

    def use_inspiration_layout(self, recording: bool = False) -> None:
        """Switch to inspiration mode button layout and dial map."""
        self._buttons = list(_INSPIRATION_BUTTONS)
        if recording:
            self._buttons.append(_INSPIRATION_RESTART_BUTTON)
        if not self._has_dials:
            self._buttons += _INSPIRATION_VOLUME_BUTTONS
        self._dial_map = dict(_INSPIRATION_DIAL_MAP)

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

    def update_state(self, state_name: str, track_name: str | None = None) -> None:
        """Refresh buttons and touchscreen for the current recording session state."""
        if not self.connected:
            return
        with self._lock:
            for btn in self._buttons:
                idx, icon, label, _key, active_state, active_color, dim_color = btn
                color = active_color if (active_state is None or active_state == state_name) else dim_color
                self._deck.set_key_image(idx, self._make_key_image(icon, label, color))
            if self._has_dials:
                self._update_touchscreen(track_name)

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

    def disconnect(self) -> None:
        if self._deck:
            try:
                self._deck.reset()
                self._deck.close()
            except Exception:
                pass
            self._deck = None

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
            pass
