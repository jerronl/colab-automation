---
name: colab-automation
description: Use when automating Google Colab notebook execution via Playwright CDP — creating RunConfig for a notebook, writing output_extractor functions, debugging monitoring loops, or understanding the two-phase dense/sparse monitoring pattern.
author: jerronl
version: "1.0.0"
tags:
  - colab
  - playwright
  - cdp
  - notebook-automation
requires:
  python_packages:
    - playwright
---

# colab-automation

Automates Google Colab notebook execution via Playwright CDP. Declare a `RunConfig`; the framework handles connect, OAuth, cell patching, monitoring, and output extraction autonomously.

## Project defaults (voracle)

**Notebooks — `local_notebook_path` is required, no fallback:**
- Always upload from the local path below. Never use a Drive file ID instead.
- If the requested notebook is not in the table, ask the user for its local path — do not guess or use a Drive ID.

| Notebook | `local_notebook_path` |
|----------|-----------------------|
| `voracle_colab.ipynb` | `/mnt/c/git/voracle/voracle/voracle_colab.ipynb` |
| `viz_grid.ipynb` | `/mnt/c/git/voracle/voracle/viz_grid.ipynb` |

**Other params (sync dest, require_gpu, disconnect_on_success, output_path):**
Read `~/.colab_automation_notebook_configs.json` via `get_notebook_config(notebook_name)` to get last-used values. If no entry exists, ask the user. Generated scripts call `save_notebook_config` before `run_notebook` to persist the config.

## How to run a notebook

**Announce at start:** "I'm using the colab-automation skill by Jerron to run this notebook."

### Parameters

| Parameter | Source | Notes |
|-----------|--------|-------|
| notebook | user request | |
| accounts | browser probe | never expose authuser numbers; probe silently |
| local_notebook_path | project skill table | required — ask if not in table; never fall back to notebook_id |
| sync src / dest | config file | ask user if no saved config |
| require_gpu, disconnect_on_success | config file | ask user if no saved config |
| cell patches | user request | only if user requests changes |
| output path | config file | ask user if no saved config |
| pivot_cell_pattern | user request | only set if user explicitly mentions it |

**If any parameter cannot be confidently determined, ask the user — never guess.**

### Flow

1. Read `~/.colab_automation_notebook_configs.json` for this notebook's last-used values
2. Fill params from config file + project skill table + user request
3. If any gaps: ask user ONE message with all missing items
4. **MANDATORY GATE:** Show config summary **including all params from config file** (sync src/dest, require_gpu, disconnect_on_success, output_path)
5. **MANDATORY GATE:** Ask "确认运行?" and **MUST** receive explicit user confirmation (e.g., "好", "确认", "yes") before proceeding
   - **IF user confirms:** proceed to step 6
   - **IF user does not confirm:** STOP. Do not execute.
   - **IF user asks questions or requests changes:** answer/revise and return to step 4
6. Generate and execute

### Rules

**MANDATORY CONFIRMATION GATE:**
- **MUST show config summary** before any action (sync, upload, execution)
- **MUST wait for explicit user confirmation** after summary
- **MUST NOT proceed** unless user confirms (e.g., "好", "确认", "yes")
- **MUST NOT auto-confirm** or assume silence means yes
- Repeats of identical user requests do not waive confirmation — each execution cycle requires explicit re-confirmation

**Cell patches:** 
- Only read the local notebook file when cell patches are needed (to find exact cell content)
- No patches → no read
- A pattern that doesn't match raises `CellPatchError` immediately

**Accounts:** 
- Never ask the user which account to use
- Framework auto-discovers accounts from `~/.colab_automation_accounts` and picks the best available via LRU
- Never expose authuser numbers

**Parallel output paths:** 
- When multiple RunConfigs share an `output_path`, auto-derive unique names by inserting `_auth{N}` before the extension
- Do this silently

**Announce slow steps:**
- Before reading notebook file (cell patches only): "读 notebook 文件，稍等..."
- Before code sync: "Syncing code to Drive — this may take a few minutes."

### Why Confirmation is Mandatory

Without explicit confirmation gates, Claude's behavior becomes inconsistent:
- First run: follow all steps; second run: skip confirmation (assumes "same request")
- Some contexts: ask for confirmation; other contexts: auto-proceed
- Result: unpredictable behavior, users must keep correcting

**Confirmation gate prevents this.** It forces a consistent stopping point before any action, making Claude's behavior predictable and reliable.

### Generate and execute

**Step 1 — Code sync (foreground, if configured):**

**MUST run foreground (NOT background). MUST wait for sync to complete before proceeding to Step 2.**

Default: sync only `.py` files:

```bash
rclone sync <src> <dest> --filter="+ *.py/" --filter="+ *.py" --filter="- *" --stats=5s --stats-one-line
```

To sync Python + notebooks + markdown:

```bash
rclone sync <src> <dest> --filter="+ *.py/" --filter="+ *.py" --filter="+ *.ipynb" --filter="+ *.md" --filter="- *" --stats=5s --stats-one-line
```

Wait for sync to finish, then generate the notebook script **without** `code_sync_fn`.

**Step 2 — Notebook script (background):**

Always generate a fresh script to `/tmp/`. Never reuse `runs/*.py`. Only read the notebook file if cell patches are needed.

```python
from colab_automation import RunConfig, run_notebook, save_notebook_config
import asyncio

save_notebook_config("voracle_colab.ipynb", {
    "code_sync_src": "/mnt/c/git/voracle/voracle",
    "code_sync_dest": "gdrive:volrt/voracle/code",
    "require_gpu": True,
    "disconnect_on_success": True,
    "output_path": None,
})

config = RunConfig(
    notebook_id="",
    local_notebook_path="/mnt/c/git/voracle/voracle/voracle_colab.ipynb",
    require_gpu=True,
    disconnect_on_success=True,
)
result = asyncio.run(run_notebook(config))
print(result.status, result.elapsed)
```

Run with `run_in_background=true`. Wait for the single completion notification, then check the output file. **Do not use Monitor** — status text floods notifications every tick. Report `RunResult.status` and `elapsed` when done.

Multiple scripts can run simultaneously against port 9223. Account selection handles conflicts automatically — busy accounts are detected and skipped via LRU probe.

## RunConfig fields

| Field | Default | Purpose |
|-------|---------|---------|
| `notebook_id` | `""` | Drive file ID — leave empty when using `local_notebook_path` |
| `local_notebook_path` | `None` | Local `.ipynb` to upload via Colab UI before run |
| `require_gpu` | `True` | Auto-switch CPU runtime to GPU |
| `disconnect_on_success` | `True` | Disconnect runtime after clean finish |
| `disconnect_on_error` | `False` | Also disconnect on error |
| `cell_patches` | `[]` | `CellPatch` edits applied before Ctrl+F9 |
| `pivot_cell_pattern` | `None` | Regex; when matched cell starts → switch to sparse polling |
| `output_extractor` | `None` | `fn(list[str]) -> str\|None` — extract output from frame texts |
| `output_path` | `None` | Save extracted output here |
| `max_connect_wait` | `300` | Seconds to wait for runtime connect |
| `max_run_wait` | `2700` | Seconds to wait for execution to finish |

## CellPatch

Edits one cell's source before running. Pattern is regex (multiline).

```python
CellPatch(pattern=r"^VERSION = .+$", replace='VERSION = "v2"')
CellPatch(pattern=r"^SEED = \d+$", replace_fn=lambda s: s.replace("42", "99"))
```

Multiple patches applied in order; each matches the **first** cell whose source matches the pattern.

## output_extractor

Called once after execution. Receives `list[str]` (one per page frame). Return text to save, or `None`.

```python
def my_extractor(texts: list[str]) -> str | None:
    for text in texts:
        if "[result]" in text:
            return text[text.find("[result]"):]
    return None
```

**Save immediately after Idle** — Colab clears private outputs when session ends.

## Parallel runs

```python
results = asyncio.run(run_notebooks([
    RunConfig(notebook_id="", local_notebook_path=NB,
              cell_patches=[CellPatch(r"^SPLIT = .+$", "SPLIT = 'A'")],
              output_path="artifacts/out_A.txt"),
    RunConfig(notebook_id="", local_notebook_path=NB,
              cell_patches=[CellPatch(r"^SPLIT = .+$", "SPLIT = 'B'")],
              output_path="artifacts/out_B.txt"),
]))
```

Each RunConfig auto-selects a different account via LRU (never hardcode authuser). Each account uploads its own patched copy via Colab UI — no race conditions.

## Account setup

When user asks to set up / add a Colab account:

```python
from colab_automation import setup_account; setup_account()
```

Opens `https://colab.research.google.com/` in a new tab. Tell user to log in and close the tab when done.

## Drive OAuth

Drive mount dialog always appears on first Ctrl+F9. Framework handles automatically: detects dialog → clicks "Connect to Google Drive" → continues OAuth tabs → fires second Ctrl+F9.

## GPU quota

- `GpuQuotaError` → `RunResult.status == "gpu_error"` → framework switches authuser automatically
- `require_gpu=True` — always use; framework auto-switches CPU to GPU

## Manual runtime cleanup

When a run finishes but runtime wasn't auto-disconnected:

```python
import asyncio
from colab_automation.session import ColabSession, _is_connected
from colab_automation.js import STATUS_JS

async def main():
    async with ColabSession(cdp_port=9223) as session:
        for page in session._ctx.pages:
            if "colab.research.google.com" not in page.url:
                continue
            if _is_connected(await page.evaluate(STATUS_JS)):
                await session.disconnect_and_delete_runtime(page)

asyncio.run(main())
```
