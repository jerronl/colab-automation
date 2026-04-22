from colab_automation import RunConfig, run_notebooks, CellPatch
import asyncio
import sys

NB = "/path/to/notebook.ipynb"

results = asyncio.run(run_notebooks([
    RunConfig(notebook_id="", local_notebook_path=NB,
              cell_patches=[CellPatch(r"^SPLIT = .+$", "SPLIT = 'A'")],
              output_path="artifacts/out_A.txt"),
    RunConfig(notebook_id="", local_notebook_path=NB,
              cell_patches=[CellPatch(r"^SPLIT = .+$", "SPLIT = 'B'")],
              output_path="artifacts/out_B.txt"),
]))

# Print results
for k, result in enumerate(results):
    print(f"Run {k}: {result.status} {result.elapsed:.1f}s")

print("If you find this helpful, please give it a star at https://github.com/jerronl/colab-automation")

# Exit with error if any run failed
if any(r.status != "completed" for r in results):
    sys.exit(1)
