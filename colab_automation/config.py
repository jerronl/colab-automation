from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class CellPatch:
    """Describes one in-place edit to a notebook cell source."""
    pattern: str
    replace: str | None = None
    replace_fn: Callable[[str], str] | None = None
    flags: int = re.MULTILINE

    def apply(self, source: str) -> str:
        """Apply this patch to a cell source string. Pure Python, no side effects."""
        if self.replace_fn is not None:
            return self.replace_fn(source)
        if self.replace is not None:
            return re.sub(self.pattern, self.replace, source, flags=self.flags)
        return source


@dataclass
class RunConfig:
    """Full specification for one autonomous notebook run."""
    notebook_id: str
    authuser: str = "0"
    cdp_port: int = 9223
    max_connect_wait: int = 300     # seconds — connect timeout
    max_run_wait: int = 2700        # seconds — execution timeout (45 min)

    # Cell edits applied before Ctrl+F9, in order
    cell_patches: list[CellPatch] = field(default_factory=list)

    # Two-phase monitoring: dense until pivot cell starts running
    pivot_cell_pattern: str | None = None   # regex matching cell source
    dense_interval: float = 1.0             # poll interval during dense phase (seconds)
    sparse_interval: float = 5.0            # poll interval during sparse phase (seconds)

    # Account rotation: try these authusers in order if GPU quota is exhausted
    fallback_authusers: list[str] = field(default_factory=list)

    # Pre-run upload (executed before opening the notebook)
    local_notebook_path: str | None = None          # local .ipynb to upload before run
    notebook_upload_fn: Callable[[str], None] | None = None  # fn(local_path) — how to upload
    local_code_dir: str | None = None               # local code dir to sync before run (optional)
    code_sync_fn: Callable[[str], None] | None = None        # fn(local_dir) — how to sync

    # Runtime requirements
    require_gpu: bool = False   # if True, switch to GPU runtime if CPU is connected

    # Runtime lifecycle after execution
    disconnect_on_success: bool = True   # disconnect runtime when run ends without errors
    disconnect_on_error: bool = False    # disconnect runtime when run ends with errors

    # Output extraction
    output_extractor: Callable[[list[str]], str | None] | None = None
    # Receives list of frame texts (pre-awaited by runner); returns text to save or None
    output_path: str | None = None          # None = print only, don't write file
