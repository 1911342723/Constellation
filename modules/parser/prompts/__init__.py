"""Prompt template loader for Cursor-Caliper LLM Router."""

from pathlib import Path
from typing import Optional

_TEMPLATE_DIR = Path(__file__).parent


def load_prompt(name: str) -> str:
    """Load a prompt template by name.

    Args:
        name: Template filename without extension (e.g. ``"router_system"``).

    Returns:
        The raw text content of the template file.

    Raises:
        FileNotFoundError: If the template does not exist.
    """
    path = _TEMPLATE_DIR / f"{name}.txt"
    if not path.exists():
        raise FileNotFoundError(f"Prompt template not found: {path}")
    return path.read_text(encoding="utf-8")
