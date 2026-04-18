# ERRORS.md — colab-automation quick reference

| Error | Cause | Fix |
|-------|-------|-----|
| `TargetClosedError` | Two scripts connecting to same CDP port | Kill first script before starting second |
| `GpuQuotaError` | GPU usage limit on this account | Switch to different `authuser` |
| `no-status` when notebook should be running | `runtime.unassign()` called at end OR not loaded | If `was_executing` was True → completed. Otherwise check page URL |
| `pivot_cell_pattern` matched no cell | Cell content doesn't match regex | Check pattern with `READ_CELLS_JS`; falls back to 30s dense phase |
| `output_extractor` returns None | Extractor ran after session ended (outputs cleared) | Call `extract_output` immediately after `run_and_monitor` returns |
| Drive dialog reappears after OAuth | OAuth tab left unresolved | Framework handles automatically; if manual: complete ALL `accounts.google.com` tabs before Ctrl+F9 |
| Notebook goes Idle immediately after Ctrl+F9 | Drive mount timeout on 1st run | Normal — framework fires second Ctrl+F9 automatically after OAuth |
| `patch_cells` returns `{error: 'no match'}` | Cell pattern not found or notebook not loaded | Verify notebook is open and loaded; check regex with `READ_CELLS_JS` |
| `replace_fn` receives wrong cell | Multiple cells match pattern | Make pattern more specific |
| `'Connecting'` appears for >5 min | Stuck or GPU quota error | Check GPU error dialog — switch authuser if present |
| TAIL_JS shows errors that didn't happen | TAIL_JS uses `innerText` — picks up source code | TAIL_JS is for progress monitoring only; don't use it for error detection |
| `ensure_browser()` raises RuntimeError | Chromium not installed | Run: `playwright install chromium` |
| viz notebook on CPU runtime | No GPU available on authuser | Change runtime type manually in browser before running viz script |
