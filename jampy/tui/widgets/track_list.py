"""Setlist display widget."""

from __future__ import annotations

from textual.widgets import Static, ListView, ListItem, Label
from textual.containers import Vertical

from ...project import Setlist


class TrackListWidget(Static):
    """Displays the setlist with track names and status indicators."""

    def __init__(self, setlist: Setlist | None = None, current_index: int = -1, instrument: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self._setlist = setlist
        self._current_index = current_index
        self._instrument = instrument

    def compose(self):
        yield Vertical(id="track-list-inner")

    def on_mount(self) -> None:
        self.refresh_tracks()

    def refresh_tracks(self) -> None:
        container = self.query_one("#track-list-inner")
        container.remove_children()
        if not self._setlist:
            container.mount(Label("No tracks loaded"))
            return

        for i, track in enumerate(self._setlist.tracks):
            has_take = self._instrument and self._instrument in track.preferred_takes
            marker = "[*]" if has_take else "[ ]"
            prefix = ">> " if i == self._current_index else "   "
            label = Label(f"{prefix}{marker} {i + 1}. {track.name}")
            if i == self._current_index:
                label.add_class("current")
            if has_take:
                label.add_class("completed")
            label.add_class("track-row")
            container.mount(label)

    def update_setlist(self, setlist: Setlist, current_index: int = -1, instrument: str = "") -> None:
        self._setlist = setlist
        self._current_index = current_index
        self._instrument = instrument
        self.refresh_tracks()
