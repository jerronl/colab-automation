# colab_automation/config_store.py
"""
Per-notebook run config persistence.

Last-used values for each notebook are stored in ~/.colab_automation_notebook_configs.json.
The skill reads this file to populate config defaults; generated scripts write to it
before calling run_notebook() so the config is always saved on the way in.
"""
from __future__ import annotations
import json
from pathlib import Path

_CONFIG_FILE = Path.home() / ".colab_automation_notebook_configs.json"


def _load_all() -> dict:
    try:
        return json.loads(_CONFIG_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def get_notebook_config(notebook_name: str) -> dict:
    """Return last-used config for *notebook_name* (basename), or {} if none."""
    return _load_all().get(notebook_name, {})


def save_notebook_config(notebook_name: str, config: dict) -> None:
    """Persist *config* as the last-used config for *notebook_name* (basename)."""
    all_configs = _load_all()
    all_configs[notebook_name] = config
    _CONFIG_FILE.write_text(json.dumps(all_configs, indent=2))
