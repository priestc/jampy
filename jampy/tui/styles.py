"""Textual CSS styles for Jam.py."""

APP_CSS = """
Screen {
    background: $surface;
}

#title-bar {
    dock: top;
    height: 3;
    background: $primary;
    color: $text;
    content-align: center middle;
    text-style: bold;
    padding: 0 2;
}

/* Home Screen */
#home-container {
    align: center middle;
    width: 100%;
    height: 100%;
}

#home-menu {
    width: 60;
    height: auto;
    border: thick $primary;
    padding: 2 4;
}

#home-menu Button {
    width: 100%;
    margin: 1 0;
}

/* Wizard Screen */
#wizard-container {
    align: center middle;
    width: 100%;
    height: 100%;
}

#wizard-form {
    width: 70;
    height: auto;
    border: thick $primary;
    padding: 2 4;
    max-height: 90%;
    overflow-y: auto;
}

#wizard-form .form-label {
    margin-top: 1;
    text-style: bold;
}

/* Project Screen */
#project-header {
    dock: top;
    height: 3;
    background: $primary;
    padding: 0 2;
    content-align: left middle;
}

#project-content {
    height: 1fr;
    padding: 1 2;
    overflow-y: auto;
}

#project-tracklist {
    height: auto;
    max-height: 15;
}

#project-content Input {
    margin: 0 0 1 0;
}

#project-content Button {
    width: auto;
    margin: 0 0 1 0;
}

#project-actions {
    dock: bottom;
    height: 5;
    padding: 1 2;
    layout: horizontal;
}

#project-actions Button {
    margin: 0 1;
}

/* Session Screen */
#session-header {
    dock: top;
    height: 3;
    background: $primary;
    padding: 0 2;
}

#session-body {
    layout: grid;
    grid-size: 2 2;
    grid-gutter: 1;
    padding: 1;
    height: 1fr;
}

#transport-panel {
    row-span: 1;
    column-span: 2;
    height: 10;
    border: thick $accent;
    padding: 1 2;
}

#vu-panel {
    height: 1fr;
    border: thick $secondary;
    padding: 1 2;
    min-width: 30;
}

#log-panel {
    height: 1fr;
    border: thick $secondary;
    padding: 1 2;
}

#session-footer {
    dock: bottom;
    height: 3;
    background: $surface-darken-1;
    padding: 0 2;
    content-align: center middle;
}

/* Post Session Screen */
#post-container {
    align: center middle;
    width: 100%;
    height: 100%;
}

#post-content {
    width: 70;
    height: auto;
    border: thick $success;
    padding: 2 4;
    max-height: 90%;
    overflow-y: auto;
}

/* Widgets */
.track-row {
    height: 3;
    padding: 0 1;
}

.track-row.current {
    background: $accent 30%;
}

.track-row.completed {
    color: $success;
}

.state-label {
    text-style: bold;
    width: 100%;
    content-align: center middle;
}

.state-idle { color: $text-muted; }
.state-waiting { color: $warning; }
.state-playing { color: $success; }
.state-between { color: $accent; }
.state-ended { color: $error; }

.vu-bar {
    height: 1;
    width: 100%;
}

.log-entry {
    height: 1;
}
"""
