"""Post-session screen: splice takes, show results."""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Header, Footer, Button, Label, RichLog
from textual.containers import Vertical

from ...processing.splicer import splice_takes, parse_session_log
from ...project import Project


class PostSessionScreen(Screen):
    """Post-session processing: splice completed takes and show results."""

    BINDINGS = [("escape", "go_home", "Home"), ("enter", "go_home", "Home")]

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="post-container"):
            with Vertical(id="post-content"):
                yield Label("Post-Session Processing", id="title-bar")
                yield Label("")
                yield RichLog(id="post-log", wrap=True, markup=True, max_lines=100)
                yield Label("")
                yield Button("Back to Project", id="btn-back", variant="primary")
        yield Footer()

    def on_mount(self) -> None:
        log = self.query_one("#post-log", RichLog)
        project: Project = self.app.project
        session_dir: Path | None = getattr(self.app, "last_session_dir", None)
        raw_path: Path | None = getattr(self.app, "last_raw_recording", None)

        if not session_dir or not raw_path:
            log.write("[red]No session data found.[/red]")
            return

        log_path = session_dir / "session_log.json"
        if not log_path.exists():
            log.write("[red]Session log not found.[/red]")
            return

        # Parse and show what we found
        try:
            instrument, completed = parse_session_log(log_path)
            log.write(f"Instrument: [bold]{instrument}[/bold]")
            log.write(f"Completed takes found: [bold]{len(completed)}[/bold]")
            log.write("")

            if not completed:
                log.write("[yellow]No completed takes to splice.[/yellow]")
                log.write("(Takes with back-to-start are not spliced)")
                return

            if not raw_path.exists():
                log.write("[red]Raw recording file not found.[/red]")
                return

            # Splice takes
            log.write("Splicing takes...")
            saved = splice_takes(project, session_dir, raw_path)

            log.write("")
            log.write(f"[green]Saved {len(saved)} take(s):[/green]")
            for path in saved:
                log.write(f"  {path.name}")

            log.write("")
            log.write("[green]Setlist updated with new preferred takes.[/green]")

        except Exception as e:
            log.write(f"[red]Error during processing: {e}[/red]")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-back":
            self.action_go_home()

    def action_go_home(self) -> None:
        # Pop back to project screen
        self.app.switch_screen("project")
