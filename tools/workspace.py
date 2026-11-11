"""Workspace selection shared by optional offline build tools."""

import os
from pathlib import Path


def workspace_root():
    configured = os.environ.get("RESEARCH_LIBRARY_DATA_ROOT")
    return Path(configured).expanduser().resolve() if configured else Path(__file__).resolve().parents[1]
