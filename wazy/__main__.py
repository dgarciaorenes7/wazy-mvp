"""Start Wazy: python3 -m wazy (reads the settings, then runs the web server until Ctrl+C)."""
from __future__ import annotations

from .server import main

if __name__ == "__main__":
    main()
