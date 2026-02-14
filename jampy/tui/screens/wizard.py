"""Studio config setup wizard screen."""

from __future__ import annotations

import sounddevice as sd
from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Header, Footer, Button, Label, Select, Input
from textual.containers import Vertical

from ...config import StudioConfig, VALID_SAMPLE_RATES, VALID_BUFFER_SIZES


def _get_devices() -> list[tuple[str, int]]:
    """Get available audio devices as (name, index) pairs."""
    try:
        devices = sd.query_devices()
        return [(f"{i}: {d['name']}", i) for i, d in enumerate(devices)]
    except Exception:
        return [("Default", -1)]


def _get_input_devices() -> list[tuple[str, int]]:
    try:
        devices = sd.query_devices()
        return [
            (f"{i}: {d['name']} ({d['max_input_channels']}ch)", i)
            for i, d in enumerate(devices) if d["max_input_channels"] > 0
        ]
    except Exception:
        return [("Default", -1)]


def _get_output_devices() -> list[tuple[str, int]]:
    try:
        devices = sd.query_devices()
        return [
            (f"{i}: {d['name']} ({d['max_output_channels']}ch)", i)
            for i, d in enumerate(devices) if d["max_output_channels"] > 0
        ]
    except Exception:
        return [("Default", -1)]


class WizardScreen(Screen):
    """Studio configuration wizard."""

    BINDINGS = [("escape", "pop_screen", "Back")]

    def compose(self) -> ComposeResult:
        config = StudioConfig.load()

        input_devs = _get_input_devices()
        output_devs = _get_output_devices()

        yield Header()
        with Vertical(id="wizard-container"):
            with Vertical(id="wizard-form"):
                yield Label("Studio Setup Wizard", id="title-bar")
                yield Label("")

                yield Label("Sample Rate:", classes="form-label")
                yield Select(
                    [(f"{sr} Hz", sr) for sr in VALID_SAMPLE_RATES],
                    value=config.sample_rate,
                    id="sel-sample-rate",
                )

                yield Label("Buffer Size:", classes="form-label")
                yield Select(
                    [(f"{bs} frames", bs) for bs in VALID_BUFFER_SIZES],
                    value=config.buffer_size,
                    id="sel-buffer-size",
                )

                yield Label("Input Device:", classes="form-label")
                yield Select(
                    input_devs,
                    value=config.input_device if config.input_device is not None else (input_devs[0][1] if input_devs else -1),
                    id="sel-input-device",
                )

                yield Label("Output Device:", classes="form-label")
                yield Select(
                    output_devs,
                    value=config.output_device if config.output_device is not None else (output_devs[0][1] if output_devs else -1),
                    id="sel-output-device",
                )

                yield Label("Projects Directory:", classes="form-label")
                yield Input(value=config.projects_dir, id="input-projects-dir")

                yield Label("")
                yield Button("Save Configuration", id="btn-save", variant="primary")
                yield Button("Cancel", id="btn-cancel", variant="error")
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-save":
            sr_sel = self.query_one("#sel-sample-rate", Select)
            bs_sel = self.query_one("#sel-buffer-size", Select)
            in_sel = self.query_one("#sel-input-device", Select)
            out_sel = self.query_one("#sel-output-device", Select)
            proj_dir = self.query_one("#input-projects-dir", Input)

            config = StudioConfig(
                sample_rate=sr_sel.value if sr_sel.value != Select.BLANK else 48000,
                buffer_size=bs_sel.value if bs_sel.value != Select.BLANK else 512,
                input_device=in_sel.value if in_sel.value not in (Select.BLANK, -1) else None,
                output_device=out_sel.value if out_sel.value not in (Select.BLANK, -1) else None,
                projects_dir=proj_dir.value,
            )

            errors = config.validate()
            if errors:
                self.notify("\n".join(errors), severity="error")
            else:
                config.save()
                self.notify("Studio configuration saved!", severity="information")
                self.dismiss()

        elif event.button.id == "btn-cancel":
            self.dismiss()
