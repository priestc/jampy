"""Entry point for Jam.py."""

from .app import JamPyApp


def main() -> None:
    app = JamPyApp()
    app.run()


if __name__ == "__main__":
    main()
