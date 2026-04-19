# colab_automation/runner.py
from __future__ import annotations
import asyncio
import json
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .config import RunConfig
from .js import STATUS_JS
from .notebook_utils import apply_patches_to_notebook
from .session import ColabSession, GpuQuotaError, CellPatchError, NotebookError, _is_connected

def _p(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# Serializes concurrent _select_authuser calls so parallel run_notebook coroutines
# don't all read the same state and pick the same account before any claim is saved.
_SELECT_LOCK: asyncio.Lock | None = None

def _get_select_lock() -> asyncio.Lock:
    global _SELECT_LOCK
    if _SELECT_LOCK is None:
        _SELECT_LOCK = asyncio.Lock()
    return _SELECT_LOCK


@dataclass
class RunResult:
    status: str         # "completed" | "gpu_error" | "timeout" | "error"
    final_status: str   # last STATUS_JS value
    output: str | None
    elapsed: float      # seconds
    authuser: str = ""  # which account actually ran (or last attempted)


# ── Account state (LRU tracking) ──────────────────────────────────────────────

_STATE_FILE    = Path.home() / ".colab_automation_state.json"
_ACCOUNTS_FILE = Path.home() / ".colab_automation_accounts"

def _load_account_state() -> dict[str, float]:
    try:
        return json.loads(_STATE_FILE.read_text())
    except Exception:
        return {}

def _save_account_state(state: dict[str, float]) -> None:
    try:
        _STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception:
        pass

def _load_known_accounts() -> list[str]:
    """Return authuser indices from ~/.colab_automation_accounts (one per line, # = comment)."""
    try:
        lines = _ACCOUNTS_FILE.read_text().splitlines()
        return [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]
    except FileNotFoundError:
        return []
    except Exception:
        return []


def _save_known_accounts(accounts: list[str]) -> None:
    try:
        _ACCOUNTS_FILE.write_text(
            "# colab authuser indices — auto-discovered\n"
            + "\n".join(accounts) + "\n"
        )
    except Exception:
        pass


async def _discover_accounts(cdp_port: int) -> list[str]:
    """
    Discover all logged-in Colab accounts in the running browser.

    Reads the current ~/.colab_automation_accounts list, then probes the
    next authuser index (max+1, max+2, …) one at a time — stopping as soon
    as one is not logged in.  Newly found accounts are appended to the file
    and returned in the combined list.

    "Not logged in" is detected by the page title containing
    "Welcome To Colab" (Colab's unauthenticated landing page).
    """
    known = _load_known_accounts()
    # If file is empty start probing from 0; otherwise probe from max+1
    max_known = max((int(a) for a in known if a.isdigit()), default=-1)
    start = max_known + 1 if known else 0

    new_found: list[str] = []

    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
            ctx = browser.contexts[0] if browser.contexts else None
            if not ctx:
                return known

            n = start
            while True:
                page = await ctx.new_page()
                try:
                    await page.goto(
                        f"https://colab.research.google.com/?authuser={n}",
                        wait_until="domcontentloaded",
                        timeout=10000,
                    )
                    await asyncio.sleep(1.5)
                    title = await page.title()
                    if "Welcome To Colab" in title or "Sign in" in title:
                        break  # first miss — stop
                    new_found.append(str(n))
                    _p(f"  new account found: authuser={n}")
                except Exception:
                    break
                finally:
                    await page.close()
                n += 1
    except Exception as e:
        _p(f"  account discovery error: {e}")
        return known

    if new_found:
        all_accounts = known + [a for a in new_found if a not in known]
        _save_known_accounts(all_accounts)
        return all_accounts

    return known


async def _probe_busy_authusers_and_tabs(cdp_port: int) -> tuple[set[str], set[str]]:
    """
    Check all open Colab tabs via STATUS_JS.
    Returns (busy, all_tab_authusers):
      busy            — authusers with an active runtime (connected/executing)
      all_tab_authusers — authusers visible in any Colab tab (regardless of runtime state)
    Both sets only include accounts where ?authuser=N is present in the URL.
    """
    busy: set[str] = set()
    all_tab_authusers: set[str] = set()
    active_unknown = 0

    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
            ctx = browser.contexts[0] if browser.contexts else None
            if not ctx:
                return busy, all_tab_authusers
            for page in ctx.pages:
                if "colab.research.google.com" not in page.url:
                    continue
                params = urllib.parse.parse_qs(urllib.parse.urlparse(page.url).query)
                authuser = params["authuser"][0] if "authuser" in params else None
                if authuser:
                    all_tab_authusers.add(authuser)
                try:
                    status = await asyncio.wait_for(page.evaluate(STATUS_JS), timeout=3.0)
                    if not isinstance(status, str) or not _is_connected(status):
                        continue
                    # Active runtime found
                    if authuser:
                        busy.add(authuser)
                    else:
                        active_unknown += 1
                except Exception:
                    pass
    except Exception as e:
        _p(f"  account probe error: {e}")

    if busy:
        _p(f"  active runtime accounts: {sorted(busy)}")
    if active_unknown:
        _p(f"  {active_unknown} active tab(s) with unknown authuser (no authuser in URL)")
    return busy, all_tab_authusers


async def _select_authuser(candidates: list[str], cdp_port: int) -> list[str]:
    """
    Reorder candidates using LRU + busy detection.
    Pool = specified candidates ∪ ~/.colab_automation_accounts ∪ browser tabs ∪ LRU file.
    Busy accounts go last; among free accounts, LRU (oldest/never-used) goes first.
    Never-used accounts have implicit timestamp=0 → always preferred over recently used ones.
    Returns reordered list (first = best choice).

    Serialized via _SELECT_LOCK so parallel run_notebook coroutines each see an
    up-to-date state file after the previous caller has claimed its account.
    """
    async with _get_select_lock():
        busy, tab_authusers = await _probe_busy_authusers_and_tabs(cdp_port)
        state = _load_account_state()
        known = await _discover_accounts(cdp_port)

        # Expand pool: candidates ∪ all known accounts ∪ browser tab accounts ∪ LRU-tracked
        pool: list[str] = list(candidates)
        for au in known + list(tab_authusers) + list(state.keys()):
            if au not in pool:
                pool.append(au)

        free = [au for au in pool if au not in busy]
        occupied = [au for au in pool if au in busy]

        if not free:
            _p("  warning: all known accounts have active runtimes — trying in LRU order")
            free = pool
            occupied = []

        # Sort each group by last-used time (oldest/never-used first = LRU)
        free.sort(key=lambda au: state.get(au, 0.0))
        occupied.sort(key=lambda au: state.get(au, 0.0))

        chosen = free[0]
        last = state.get(chosen, 0.0)
        if last > 0:
            ago = int((time.time() - last) // 60)
            _p(f"  selected authuser={chosen} (last used {ago} min ago)")
        else:
            _p(f"  selected authuser={chosen} (never used)")

        # Claim before releasing lock — the next caller will see this account as used
        # and pick a different one.
        state[chosen] = time.time()
        _save_account_state(state)

        return free + occupied


async def _run_once(config: RunConfig, authuser: str, t0: float) -> RunResult:
    """Single attempt: open notebook as `authuser`, run, extract output."""
    final_status = "no-status"
    output = None
    try:
        async with ColabSession(cdp_port=config.cdp_port) as session:
            if config.local_notebook_path:
                # Per-account upload workflow:
                # 1. Apply patches to a local temp copy (so each account gets its own variant)
                # 2. Upload via notebook_upload_fn (e.g. rclone) or Colab browser UI
                # 3. Use the resulting page; skip in-browser patch_cells
                tmp_path = None
                try:
                    if config.cell_patches:
                        tmp_path = apply_patches_to_notebook(
                            config.local_notebook_path, config.cell_patches
                        )
                    upload_src = tmp_path or config.local_notebook_path

                    if config.notebook_upload_fn:
                        _p(f"uploading notebook (authuser={authuser}): {upload_src} ...")
                        config.notebook_upload_fn(upload_src)
                        _p("notebook upload complete")
                        page = await session.open_notebook(config.notebook_id, authuser)
                    else:
                        notebook_id, page = await session.upload_notebook(authuser, upload_src)
                        # Update config.notebook_id isn't needed — page is already open
                        _ = notebook_id  # available if caller needs it later
                finally:
                    if tmp_path:
                        os.unlink(tmp_path)
            else:
                page = await session.open_notebook(config.notebook_id, authuser)
                if config.cell_patches:
                    await session.patch_cells(page, config.cell_patches)

            try:
                await session.ensure_connected(page, max_wait=config.max_connect_wait,
                                              require_gpu=config.require_gpu)
                final_status = await session.run_and_monitor(page, config)
                output = await session.extract_output(
                    page, config.output_extractor, config.output_path
                )
                if config.disconnect_on_success and final_status != "no-status":
                    await session.disconnect_and_delete_runtime(page)
            except GpuQuotaError:
                _p(f"  GPU quota exceeded — disconnecting and closing tab (authuser={authuser})")
                await session.disconnect_and_delete_runtime(page)
                await page.close()
                raise
            except NotebookError as e:
                _p(f"[NOTEBOOK ERROR] Execution stopped: {e}")
                if config.disconnect_on_error:
                    await session.disconnect_and_delete_runtime(page)
                return RunResult(status="error", final_status=f"NotebookError: {e}",
                                 output=None, elapsed=time.time() - t0, authuser=authuser)

    except GpuQuotaError:
        return RunResult(status="gpu_error", final_status=final_status,
                         output=None, elapsed=time.time() - t0, authuser=authuser)
    except TimeoutError:
        return RunResult(status="timeout", final_status=final_status,
                         output=None, elapsed=time.time() - t0, authuser=authuser)
    except Exception as e:
        return RunResult(status="error", final_status=f"{type(e).__name__}: {e}",
                         output=None, elapsed=time.time() - t0, authuser=authuser)

    return RunResult(status="completed", final_status=final_status,
                     output=output, elapsed=time.time() - t0, authuser=authuser)


async def run_notebook(config: RunConfig) -> RunResult:
    """
    Run one notebook autonomously. Uploads/syncs first (if configured), then
    probes for busy accounts, picks LRU free account, falls through fallbacks
    on GpuQuotaError. Returns the first non-gpu_error result.
    """
    t0 = time.time()

    # Sync code once — shared across all authusers for this config
    if config.local_code_dir and config.code_sync_fn:
        _p(f"syncing code: {config.local_code_dir} → Drive (may take a few minutes)...")
        config.code_sync_fn(config.local_code_dir)
        _p("code sync complete")

    candidates = [config.authuser] + list(config.fallback_authusers)
    _p(f"probing available accounts (candidates: {candidates})...")
    authusers = await _select_authuser(candidates, config.cdp_port)
    # Note: _select_authuser already claimed authusers[0] in the state file.
    # For fallbacks, claim each account before _run_once so kills don't leave
    # them with timestamp=0.

    result = RunResult(status="gpu_error", final_status="no-status",
                       output=None, elapsed=0.0, authuser=authusers[0])

    tried: set[str] = set()

    while True:
        # Pick next untried account; on first iteration authusers[0] is already claimed.
        authuser = next((au for au in authusers if au not in tried), None)
        if authuser is None:
            break
        tried.add(authuser)

        if len(tried) > 1:  # primary was already claimed in _select_authuser
            state = _load_account_state()
            state[authuser] = time.time()
            _save_account_state(state)

        result = await _run_once(config, authuser, t0)
        if result.status != "gpu_error":
            return result
        _p(f"GPU quota on authuser={authuser}, trying next...")

        # Re-probe: other parallel tasks may have claimed accounts since our
        # initial _select_authuser call. A fresh call sees current runtime busy
        # state and timestamps, so the next pick is a genuinely free account.
        authusers = await _select_authuser(candidates, config.cdp_port)

    return result  # all accounts exhausted


async def run_notebooks(configs: list[RunConfig]) -> list[RunResult]:
    """
    Run multiple RunConfigs concurrently in the same browser (different tabs).
    Each config can use a different authuser and cell_patches to cover different
    parts of the same notebook, or run entirely separate notebooks.

    Account selection is handled per-config by run_notebook → _select_authuser,
    which claims each chosen account in the state file immediately (before any
    await). Because asyncio is single-threaded, the claim is atomic: concurrent
    calls will read the updated state and pick different accounts naturally.

    Note: all configs must share the same cdp_port (same browser session).
    """
    return list(await asyncio.gather(*[run_notebook(c) for c in configs]))
