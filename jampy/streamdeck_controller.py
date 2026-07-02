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


# (key_index, top_text, bottom_text, key_char, active_state_name, active_color, dim_color)
# active_state_name=None means always shown in active color.
_SESSION_BUTTONS: list[tuple] = [
    (0, "R",  "Record",   "r", "WAITING",       (0,   200,   0), (20,  60, 20)),
    (1, "B",  "Back",     "b", "PLAYING",        (255, 140,   0), (60,  40, 10)),
    (2, "E",  "End Song", "e", "PLAYING",        (230, 200,   0), (60,  50,  0)),
    (3, "N",  "Next",     "n", "BETWEEN_TRACKS", (0,   180, 255), ( 0,  45, 64)),
    (4, "Q",  "Quit",     "q", None,             (200,  30,  30), (200, 30, 30)),
]

# Only added on devices without dials (standard StreamDeck with 15 keys).
_VOLUME_BUTTONS: list[tuple] = [
    (5, "L",  "Vol -",    "l", None, (0,   120, 200), (0,   120, 200)),
    (6, "U",  "Vol +",    "u", None, (0,   120, 200), (0,   120, 200)),
    (7, "[",  "Takes -",  "[", None, (120,   0, 200), (120,   0, 200)),
    (8, "]",  "Takes +",  "]", None, (120,   0, 200), (120,   0, 200)),
]

# Inspiration mode — play/pause at 0 is rendered dynamically by update_inspiration().
_INSPIRATION_BUTTONS: list[tuple] = [
    (0, None, None,   " ", None, None,           None),           # play/pause — rendered by update_inspiration
    (1, "S",  "Skip", "s", None, (0,  120, 200), (0,  120, 200)),
    (2, "Q",  "Quit", "q", None, (200,  30,  30), (200, 30,  30)),
]

_INSPIRATION_VOLUME_BUTTONS: list[tuple] = [
    (3, "L",  "Vol -", "l", None, (0,  120, 200), (0,  120, 200)),
    (4, "U",  "Vol +", "u", None, (0,  120, 200), (0,  120, 200)),
]

# Default dial map for recording session
_SESSION_DIAL_MAP: dict[int, tuple[str, str, str]] = {
    0: ("l", "u", "Backing\nVol"),
    1: ("[", "]", "Takes\nVol"),
}

# Dial map for inspiration mode (only one volume dial needed)
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

    def use_inspiration_layout(self) -> None:
        """Switch to inspiration mode button layout and dial map."""
        self._buttons = list(_INSPIRATION_BUTTONS)
        if not self._has_dials:
            self._buttons += _INSPIRATION_VOLUME_BUTTONS
        self._dial_map = dict(_INSPIRATION_DIAL_MAP)

    def _on_key_change(self, deck, key_index: int, pressed: bool) -> None:
        if not pressed:
            return
        for idx, _top, _bot, key_char, *_ in self._buttons:
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
                idx, top, bot, _key, active_state, active_color, dim_color = btn
                color = active_color if (active_state is None or active_state == state_name) else dim_color
                self._deck.set_key_image(idx, self._make_key_image(top, bot, color))
            if self._has_dials:
                self._update_touchscreen(track_name)

    def update_inspiration(self, is_playing: bool, track_name: str | None = None) -> None:
        """Refresh buttons and touchscreen for the current inspiration mode state."""
        if not self.connected:
            return
        with self._lock:
            # Play/pause button at key 0 — appearance depends on playback state
            if is_playing:
                self._deck.set_key_image(0, self._make_key_image("||", "Pause", (200, 130, 0)))
            else:
                self._deck.set_key_image(0, self._make_key_image(">", "Play", (0, 180, 0)))
            # Remaining static buttons (S, Q, and optionally L/U)
            for btn in self._buttons:
                idx, top, bot, _key, _state, active_color, _dim = btn
                if idx == 0 or top is None:
                    continue
                self._deck.set_key_image(idx, self._make_key_image(top, bot, active_color))
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

    def _make_key_image(self, top: str, bottom: str, color: tuple) -> bytes:
        img = PILHelper.create_image(self._deck, background=color)
        draw = ImageDraw.Draw(img)
        w, h = img.size
        draw.text((w // 2, h // 3),     top,    anchor="mm", font=_load_font(20), fill="white")
        draw.text((w // 2, h * 2 // 3), bottom, anchor="mm", font=_load_font(13), fill="white")
        return PILHelper.to_native_format(self._deck, img)

    def _update_touchscreen(self, track_name: str | None = None) -> None:
        try:
            img = PILHelper.create_touchscreen_image(self._deck, background="black")
            draw = ImageDraw.Draw(img)
            w, h = img.size  # 800×100
            section_w = w // 4  # 200px per dial section

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
