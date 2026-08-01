"""Cross-platform ttk normalization.

macOS gets a generous-looking UI for free: the Aqua ttk theme draws every
button, tab, and field with native Cocoa chrome, at native Cocoa font
metrics. Linux has no equivalent native ttk theme — it falls back to Tk's
own Clam/Alt/Default themes, whose default fonts and padding were never
tuned to match what Aqua gives away for free, so identical widget code
renders with visibly smaller text and almost no padding around tabs and
buttons. This module pins Linux to one explicitly-styled look tuned to
match macOS's apparent size, so no other UI file needs a platform check.
"""

from __future__ import annotations

import sys
import tkinter as tk
import tkinter.font as tkFont
from tkinter import ttk

IS_LINUX = sys.platform.startswith("linux")

# Multiplier for raw pixel sizes that live outside ttk's styling system
# entirely (e.g. rendered button images), so `normalize()`'s font/style
# changes below don't reach them.
_PIXEL_SCALE = 1.3


def scaled_px(pixels: int) -> int:
    """Scale a raw pixel dimension to visually match macOS on Linux; a
    no-op on every other platform."""
    return round(pixels * _PIXEL_SCALE) if IS_LINUX else pixels


def normalize(root: tk.Misc) -> None:
    """Apply once at startup, before any widgets are built. A no-op on
    non-Linux platforms, which already look right via their native theme."""
    if not IS_LINUX:
        return

    # Many Linux setups (bad EDID physical-size data, VMs, some laptop
    # panels) make Tk miscompute the display DPI, so point-sized fonts
    # render much smaller than the same code produces on macOS. Pin the
    # DPI assumption to 96, what a 100%-scaled Linux desktop actually
    # uses, so text comes out the same size as on macOS.
    root.tk.call("tk", "scaling", 96 / 72)

    # Clam is the only built-in theme where every style below reliably
    # honors an explicit `padding` option (Default/Alt mostly ignore it),
    # which is what makes the padding pass further down actually work.
    style = ttk.Style(root)
    style.theme_use("clam")

    # Xft's default named-font sizes are tuned for dense desktop chrome,
    # not this app's fairly sparse forms — bump them so rendered text
    # comes out the same apparent size as macOS's system font.
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
        tkFont.nametofont(name).configure(size=11)

    # Aqua's button/tab/field chrome is drawn natively with generous
    # built-in padding baked in; Clam's is nearly flush against the label
    # text. Add it back explicitly so tabs and buttons stop looking like
    # text crammed against a border.
    style.configure("TButton", padding=(12, 6))
    style.configure("TNotebook.Tab", padding=(16, 8))
    style.configure("TEntry", padding=(6, 4))
    style.configure("TCombobox", padding=(6, 4))
    style.configure("TCheckbutton", padding=(4, 4))
    style.configure("TRadiobutton", padding=(4, 4))
    style.configure("TLabelframe", padding=(8, 8))
