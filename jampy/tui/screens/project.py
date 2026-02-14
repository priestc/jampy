"""Project screen: view setlist, add tracks, start session."""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Header, Footer, Button, Label, Input, Static
from textual.containers import Vertical, Horizontal

from ...project import Project
from ...audio.formats import get_duration
from ..widgets.track_list import TrackListWidget


class ProjectScreen(Screen):
    """View and manage a project's setlist, start recording sessions."""

    BINDINGS = [("escape", "go_home", "Home")]

    def compose(self) -> ComposeResult:
        project: Project = self.app.project
        yield Header()
        yield Label(f"Project: {project.name}", id="project-header")
        with Vertical(id="project-content"):
            yield TrackListWidget(project.setlist, id="project-tracklist")
            yield Label("")
            yield Label("Add Backing Track (enter full path):")
            yield Input(placeholder="/path/to/song.mp3", id="input-backing-track")
            yield Label("")
            yield Label("Instrument for session:")
            yield Input(placeholder="acoustic guitar", id="input-instrument")
        with Horizontal(id="project-actions"):
            yield Button("Add Track", id="btn-add-track", variant="primary")
            yield Button("Start Session", id="btn-start-session", variant="success")
            yield Button("Back", id="btn-back", variant="default")
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        project: Project = self.app.project

        if event.button.id == "btn-add-track":
            path_input = self.query_one("#input-backing-track", Input)
            path_str = path_input.value.strip()
            if not path_str:
                self.notify("Enter a file path", severity="warning")
                path_input.focus()
                return
            path = Path(path_str).expanduser()
            if not path.exists():
                self.notify(f"File not found: {path}", severity="error")
                return
            try:
                entry = project.add_backing_track(path)
                entry.duration_seconds = get_duration(path)
                project.save_setlist()
                self.notify(f"Added: {entry.name}")
                # Refresh track list
                tl = self.query_one("#project-tracklist", TrackListWidget)
                tl.update_setlist(project.setlist)
                path_input.value = ""
            except Exception as e:
                self.notify(f"Error: {e}", severity="error")

        elif event.button.id == "btn-start-session":
            inst_input = self.query_one("#input-instrument", Input)
            instrument = inst_input.value.strip()
            if not instrument:
                self.notify("Enter an instrument name", severity="warning")
                inst_input.focus()
                return
            if not project.setlist.tracks:
                self.notify("Add at least one track first", severity="warning")
                return
            self.app.session_instrument = instrument
            self.app.push_screen("session")

        elif event.button.id == "btn-back":
            self.action_go_home()

    def action_go_home(self) -> None:
        self.app.pop_screen()
