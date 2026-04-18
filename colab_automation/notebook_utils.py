# colab_automation/notebook_utils.py
from __future__ import annotations
import json
import os
import re
import tempfile

from .config import CellPatch
from .session import CellPatchError


def apply_patches_to_notebook(ipynb_path: str, patches: list[CellPatch]) -> str:
    """
    Apply CellPatch list to a local .ipynb file.

    Mirrors PATCH_CELL_JS logic: for each patch, finds the first cell whose
    source matches the pattern and applies the replacement. Raises CellPatchError
    if any patch matches no cell.

    Returns the path to a temporary .ipynb file with patches applied.
    Caller is responsible for deleting the temp file when done.
    """
    with open(ipynb_path, "r", encoding="utf-8") as f:
        nb = json.load(f)

    cells = nb.get("cells", [])

    for patch in patches:
        matched = False
        for cell in cells:
            raw = cell.get("source", "")
            src = "".join(raw) if isinstance(raw, list) else raw

            if not re.search(patch.pattern, src, flags=patch.flags):
                continue

            new_src = patch.apply(src)
            # Preserve original storage format (list vs string)
            cell["source"] = (
                new_src.splitlines(keepends=True) if isinstance(raw, list) else new_src
            )
            matched = True
            break

        if not matched:
            raise CellPatchError(
                f"Pattern {patch.pattern!r} matched no cell in {ipynb_path}. "
                f"Check the notebook for the exact current content."
            )

    base = os.path.basename(ipynb_path)
    fd, tmp_path = tempfile.mkstemp(suffix=".ipynb", prefix=f"patched_{base}_")
    os.close(fd)
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(nb, f, ensure_ascii=False, indent=1)

    return tmp_path
