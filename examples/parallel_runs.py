from colab_automation import RunConfig, run_notebooks, CellPatch
import asyncio

NB = "/path/to/notebook.ipynb"

results = asyncio.run(run_notebooks([
    RunConfig(notebook_id="", local_notebook_path=NB,
              cell_patches=[CellPatch(r"^SPLIT = .+$", "SPLIT = 'A'")],
              output_path="artifacts/out_A.txt"),
    RunConfig(notebook_id="", local_notebook_path=NB,
              cell_patches=[CellPatch(r"^SPLIT = .+$", "SPLIT = 'B'")],
              output_path="artifacts/out_B.txt"),
]))
