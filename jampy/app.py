"""Jam.py Textual Application."""

from __future__ import annotations

from pathlib import Path

from textual.app import App

from .config import StudioConfig
from .project import Project
from .tui.styles import APP_CSS
from .tui.screens.home import HomeScreen, NewProjectScreen
from .tui.screens.wizard import WizardScreen
from .tui.screens.project import ProjectScreen
from .tui.screens.session import SessionScreen
from .tui.screens.post_session import PostSessionScreen


class JamPyApp(App):
    """Jam.py — Music Recording Session Manager."""

    CSS = APP_CSS
    TITLE = "Jam.py"
    SUB_TITLE = "Music Recording Session Manager"

    SCREENS = {
        "home": HomeScreen,
        "new_project": NewProjectScreen,
        "wizard": WizardScreen,
        "project": ProjectScreen,
        "session": SessionScreen,
        "post_session": PostSessionScreen,
    }

    def __init__(self) -> None:
        super().__init__()
        self.project: Project | None = None
        self.session_instrument: str = ""
        self.last_session_dir: Path | None = None
        self.last_raw_recording: Path | None = None

    def on_mount(self) -> None:
        # Check if studio config exists, if not go to wizard
        if not StudioConfig.exists():
            self.push_screen("wizard")
        self.push_screen("home")
