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

## Install

```bash
~/voracle-env/bin/pip install -e /mnt/c/git/colab-automation
```

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

**Announce at start:** "I'm using the colab-automation skill to run this notebook."

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
4. Show config summary → ask "确认运行？"
5. Wait for explicit confirmation — **never skip this step**
6. Generate and execute

### Rules

**Cell patches:** Only read the local notebook file when cell patches are needed (to find exact cell content). No patches → no read. A pattern that doesn't match raises `CellPatchError` immediately.

**Accounts:** Never ask the user which account to use. Framework auto-discovers accounts from `~/.colab_automation_accounts` and picks the best available via LRU. Never expose authuser numbers.

**Parallel output paths:** When multiple RunConfigs share an `output_path`, auto-derive unique names by inserting `_auth{N}` before the extension. Do this silently.

**Announce slow steps:**
- Before reading notebook file (cell patches only): "读 notebook 文件，稍等..."
- Before code sync: "Syncing code to Drive — this may take a few minutes."

### Generate and execute

**Step 1 — Code sync (foreground, if configured):**

```bash
rclone sync <src> <dest> --stats=5s --stats-one-line \
    --exclude ".git/**" --exclude "__pycache__/**" --exclude "*.egg-info/**" \
    --exclude "*.pyc" --exclude ".ipynb_checkpoints/**" --exclude "*.pt" \
    --exclude "*.pth" --exclude "artifacts/**" --exclude "logs/**" --exclude "checkpoints/**"
```

Wait for sync to finish. Then generate the notebook script **without** `code_sync_fn`.

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

**Never run two scripts simultaneously** — both connect to port 9223 and cause `TargetClosedError`.

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
    RunConfig(notebook_id="", local_notebook_path=NB, authuser="0",
              cell_patches=[CellPatch(r"^SPLIT = .+$", "SPLIT = 'A'")],
              output_path="artifacts/out_A.txt"),
    RunConfig(notebook_id="", local_notebook_path=NB, authuser="1",
              cell_patches=[CellPatch(r"^SPLIT = .+$", "SPLIT = 'B'")],
              output_path="artifacts/out_B.txt"),
]))
```

Each account uploads its own patched copy via Colab UI — no race conditions.

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
