"""Home screen: open/create project, studio wizard."""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Header, Footer, Button, Static, Label, Input, DirectoryTree
from textual.containers import Vertical, Horizontal

from ...config import StudioConfig
from ...project import Project


class HomeScreen(Screen):
    """Main menu: open project, create project, or configure studio."""

    BINDINGS = [("q", "quit", "Quit")]

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="home-container"):
            with Vertical(id="home-menu"):
                yield Label("Jam.py", id="title-bar")
                yield Label("Music Recording Session Manager")
                yield Label("")

                config = StudioConfig.load()
                projects_dir = Path(config.projects_dir)
                existing = Project.list_projects(projects_dir) if projects_dir.exists() else []

                if existing:
                    yield Label("Recent Projects:")
                    for proj_path in existing[-5:]:
                        yield Button(f"  {proj_path.name}", id=f"open-{proj_path.name}", variant="default")
                    yield Label("")

                yield Button("New Project", id="btn-new-project", variant="primary")
                yield Button("Studio Setup", id="btn-wizard", variant="warning")
                yield Button("Quit", id="btn-quit", variant="error")
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id or ""
        if btn_id == "btn-new-project":
            self.app.push_screen("new_project")
        elif btn_id == "btn-wizard":
            self.app.push_screen("wizard")
        elif btn_id == "btn-quit":
            self.app.exit()
        elif btn_id.startswith("open-"):
            project_name = btn_id[5:]
            config = StudioConfig.load()
            proj_path = Path(config.projects_dir) / project_name
            if proj_path.exists():
                project = Project.open(proj_path)
                self.app.project = project
                self.app.push_screen("project")


class NewProjectScreen(Screen):
    """Screen for creating a new project."""

    BINDINGS = [("escape", "pop_screen", "Back")]

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="home-container"):
            with Vertical(id="home-menu"):
                yield Label("Create New Project", id="title-bar")
                yield Label("")
                yield Label("Project Name:")
                yield Input(placeholder="My Album", id="project-name-input")
                yield Label("")
                yield Horizontal(
                    Button("Create", id="btn-create", variant="primary"),
                    Button("Cancel", id="btn-cancel", variant="error"),
                )
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-create":
            name_input = self.query_one("#project-name-input", Input)
            name = name_input.value.strip()
            if name:
                config = StudioConfig.load()
                project = Project.create_new(Path(config.projects_dir), name)
                self.app.project = project
                self.dismiss()
                self.app.push_screen("project")
        elif event.button.id == "btn-cancel":
            self.dismiss()
