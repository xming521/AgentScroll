"""Command-line entry point for the standalone collector."""

from .collector import main


if __name__ == "__main__":
    raise SystemExit(main())
