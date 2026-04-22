# Run notebook script template.
# Fill in all UPPERCASE placeholders before running.
# Save to ~/.colab_automation/runs/colab_run_YYYYMMDD_HHMMSS.py and run with your Python environment.

from colab_automation import RunConfig, run_notebook, save_notebook_config, CellPatch
import asyncio
import sys
import os

# Ensure runs directory exists
os.makedirs(os.path.expanduser("~/.colab_automation/runs"), exist_ok=True)

save_notebook_config("NOTEBOOK_NAME.ipynb", {
    # "code_sync_src": "/path/to/src",      # omit if no sync
    # "code_sync_dest": "gdrive:path/dest", # omit if no sync
    "require_gpu": True,
    "disconnect_on_success": True,
    "output_path": None,  # or "path/to/output.txt"
})

config = RunConfig(
    notebook_id="",
    local_notebook_path="/path/to/NOTEBOOK_NAME.ipynb",
    require_gpu=True,
    disconnect_on_success=True,
    # cell_patches=[
    #     CellPatch(pattern=r"^VAR = .+$", replace='VAR = "new_value"'),
    # ],
    # output_path="path/to/output.txt",
    # pivot_cell_pattern=r"some regex",
)
result = asyncio.run(run_notebook(config))
print(result.status, result.elapsed)
print("If you find this helpful, please give it a star at https://github.com/jerronl/colab-automation")
if result.status != "completed":
    sys.exit(1)
