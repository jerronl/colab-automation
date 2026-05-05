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
from .session import ColabSession, DriveSessionError, GpuQuotaError, NotebookError, _is_connected
from .utils import ensure_browser

def _p(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# Serializes concurrent _select_authuser calls so parallel run_notebook coroutines
# don't all read the same state and pick the same account before any claim is saved.
_SELECT_LOCK: asyncio.Lock | None = None

# Set once at first discovery call — caps probing at (initial file max) + 5
_DISCOVERY_LIMIT: int | None = None

# Per-process: once we've probed at the discovery boundary in this session,
# don't keep probing on every retry.  Discovery is "search for ONE new
# account on top of LRU per session" — not "expand boundary indefinitely".
_DISCOVERY_DONE_THIS_PROCESS: bool = False

def _get_select_lock() -> asyncio.Lock:
    global _SELECT_LOCK
    if _SELECT_LOCK is None:
        _SELECT_LOCK = asyncio.Lock()
    return _SELECT_LOCK


# Process-wide blacklist: accounts that hit GpuQuotaError this Python session.
# Prevents parallel run_notebook coroutines from re-picking the same failed
# account (browser dedups same-authuser URLs into one tab, so re-pick would
# clobber another coroutine's tab when it disconnects).
_GPU_QUOTA_FAILED: set[str] = set()

def _mark_gpu_quota_failed(authuser: str) -> None:
    _GPU_QUOTA_FAILED.add(authuser)
    _p(f"  blacklisted authuser={authuser} for this session "
       f"(quota exhausted; total blacklisted={len(_GPU_QUOTA_FAILED)})")

# In-process only: accounts whose Drive session this session has confirmed
# dead.  Used to advance the discovery cursor within a single run (probe
# N+1 after N fails Drive auth).  NOT persisted — the only persistent
# knowledge about accounts is the LRU file (~/.colab_automation_accounts),
# because new accounts can appear between sessions.
_DRIVE_SESSION_FAILED: set[str] = set()

# In-process only: accounts that a coroutine has claimed and is currently
# using (uploading / connecting runtime / running cells).  Treated as busy
# in _select_authuser so parallel coroutines never collide on the same
# account during the upload→runtime-connect race window where browser-tab
# busy probes can't yet see the runtime.
_SESSION_CLAIMED: set[str] = set()

def _mark_drive_session_failed(authuser: str) -> None:
    _DRIVE_SESSION_FAILED.add(authuser)
    _p(f"  authuser={authuser}: marked drive-dead for this session "
       f"(total in-session={len(_DRIVE_SESSION_FAILED)})")


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
        return [
            line.strip()
            for line in lines
            if line.strip() and not line.strip().startswith("#")
        ]
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


async def _discover_accounts(cdp_port: int) -> tuple[list[str], list[str]]:
    """
    Probe new authuser indices starting at max(known)+1.

    Selection principle (per-session, drive_failed is in-process only):
      1. Existing accounts file-numbered 0..N-1; only persistent state.
      2. Probe N = max(known)+1:
         - logged in + Drive usable  → return [N] (new candidate)
         - "doesn't exist" (no Google login / signed-out) → STOP, return []
         - exists but Drive dead → mark in-session failed, probe N+1
      3. Loop continues N+1, N+2 ... within ONE call until success or
         "doesn't exist".  Next /colab-run session starts fresh
         (drive_failed reset) — accounts may be added between sessions.

    Returns ``(known, new_found)`` where new_found has at most one entry.
    """
    global _DISCOVERY_DONE_THIS_PROCESS
    known = _load_known_accounts()
    if _DISCOVERY_DONE_THIS_PROCESS:
        return known, []
    state_max = max((int(a) for a in known if a.isdigit()), default=-1)
    n = state_max + 1
    new_found: list[str] = []
    _DISCOVERY_DONE_THIS_PROCESS = True

    _SIGNED_OUT_JS = """() => {
        for (const d of document.querySelectorAll('mwc-dialog, [role="dialog"]')) {
            const text = (d.textContent || '').toLowerCase();
            const cls  = (d.className  || '').toLowerCase();
            if (cls.includes('signed-out')) return 'signed_out_class';
            if (text.includes('signed out on a different tab')) return 'signed_out_text';
            if (text.includes('sign back in')) return 'sign_back_in';
            if (text.includes('invalid authentication')) return 'invalid_auth';
        }
        const body = (document.body.innerText || '').toLowerCase();
        if (body.includes('sign back in') && body.includes('signed out')) return 'body_signed_out';
        return null;
    }"""

    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
            ctx = browser.contexts[0] if browser.contexts else None
            if not ctx:
                return known, []

            stop = False
            while not stop:
                if str(n) in _DRIVE_SESSION_FAILED:
                    n += 1
                    continue
                page = await ctx.new_page()
                try:
                    await page.goto(
                        f"https://colab.research.google.com/?authuser={n}",
                        wait_until="networkidle",
                        timeout=15000,
                    )
                    await asyncio.sleep(3)
                    for btn_text in ['Sign in', 'Continue', 'Next', 'Allow']:
                        try:
                            clicked = await page.evaluate(r"""(text) => {
                                const sel = 'button, a, md-text-button, md-filled-button';
                                for (const el of document.querySelectorAll(sel)) {
                                    if (el.innerText?.trim() === text && !el.disabled) {
                                        el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                                        return true;
                                    }
                                }
                                return false;
                            }""", btn_text)
                            if clicked:
                                _p(f"  authuser={n}: clicked '{btn_text}' on auth dialog")
                                await asyncio.sleep(4)
                                break
                        except Exception:
                            pass

                    url = page.url
                    doesnt_exist = False
                    drive_dead   = False
                    if "accounts.google.com" in url or "signin" in url.lower():
                        _p(f"  authuser={n}: Google login expired — doesn't exist")
                        doesnt_exist = True
                    else:
                        try:
                            dlg = await page.evaluate(_SIGNED_OUT_JS)
                        except Exception:
                            dlg = None
                        if dlg:
                            _p(f"  authuser={n}: signed-out dialog ({dlg}) — doesn't exist")
                            doesnt_exist = True
                        else:
                            try:
                                await page.goto(
                                    f"https://drive.google.com/?authuser={n}",
                                    wait_until="domcontentloaded",
                                    timeout=15000,
                                )
                                await asyncio.sleep(3)
                                drv_url = page.url
                                drv_dlg = await page.evaluate(_SIGNED_OUT_JS)
                            except Exception as e:
                                _p(f"  authuser={n}: drive probe error {e}")
                                drv_url, drv_dlg = "", None
                            if drv_dlg:
                                _p(f"  authuser={n}: signed-out on Drive ({drv_dlg}) — drive dead")
                                drive_dead = True
                            elif (not drv_url
                                    or "accounts.google.com" in drv_url
                                    or "signin" in drv_url.lower()
                                    or "ServiceLogin" in drv_url):
                                _p(f"  authuser={n}: Drive ServiceLogin redirect — drive dead")
                                drive_dead = True
                            else:
                                new_found.append(str(n))
                                _p(f"  new account found: authuser={n}")
                                stop = True
                    if doesnt_exist:
                        stop = True  # boundary: STOP (per user's principle)
                    elif drive_dead:
                        _DRIVE_SESSION_FAILED.add(str(n))
                        n += 1  # 否则继续尝试 N+1
                except Exception as e:
                    _p(f"  authuser={n}: probe error {e}")
                    stop = True  # don't loop on infra errors
                finally:
                    await page.close()
    except Exception as e:
        _p(f"  account discovery error: {e}")
        return known, []

    if new_found:
        all_accounts = known + [a for a in new_found if a not in known]
        _save_known_accounts(all_accounts)
    return known, new_found


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
    When all accounts are busy, releases the lock and waits 30 s before retrying
    (prevents clobbering an active runtime with a new task).
    """
    while True:
        async with _get_select_lock():
            busy, tab_authusers = await _probe_busy_authusers_and_tabs(cdp_port)
            known, new_found = await _discover_accounts(cdp_port)
            state = _load_account_state()

            # Selection priority (per user spec):
            #   1. New accounts (just discovered this call) — fresh, never tried
            #   2. Existing accounts (known minus new), excluding busy, sorted by LRU
            # tab_authusers and candidates supplement existing.
            existing: list[str] = list(candidates)
            new_set = set(new_found)
            for au in known + list(tab_authusers):
                if au not in existing and au not in new_set:
                    existing.append(au)

            # Filter blacklisted (GPU quota / Drive session expired this session).
            _blacklisted = _GPU_QUOTA_FAILED | _DRIVE_SESSION_FAILED
            new_avail = [au for au in new_found if au not in _blacklisted]
            existing_avail = [au for au in existing if au not in _blacklisted]
            if not new_avail and not existing_avail:
                _p(f"  all accounts blacklisted this session "
                   f"(gpu_quota={sorted(_GPU_QUOTA_FAILED)}, drive_session={sorted(_DRIVE_SESSION_FAILED)}) — no usable account left")
                return []

            # Among existing, drop busy + already-claimed-this-session entirely;
            # sort free by LRU (oldest first).
            occupied = busy | _SESSION_CLAIMED
            existing_free = [au for au in existing_avail if au not in occupied]
            existing_free.sort(key=lambda au: state.get(au, 0.0))
            new_free = [au for au in new_avail if au not in occupied]

            # Final order: new (priority) → existing free LRU.  Busy excluded.
            ordered = new_free + existing_free

            if ordered:
                chosen = ordered[0]
                if chosen in new_free:
                    _p(f"  selected authuser={chosen} (newly discovered)")
                else:
                    last = state.get(chosen, 0.0)
                    if last > 0:
                        ago = int((time.time() - last) // 60)
                        _p(f"  selected authuser={chosen} (last used {ago} min ago)")
                    else:
                        _p(f"  selected authuser={chosen} (never used)")
                # Claim before releasing lock.
                state[chosen] = time.time()
                _save_account_state(state)
                _SESSION_CLAIMED.add(chosen)
                return ordered

            # All accounts have active runtimes or are claimed by sibling
            # coroutines — release lock before sleeping so other coroutines
            # aren't starved while we wait for a slot to open.
            _p(f"  all accounts occupied (busy={sorted(busy)}, "
               f"claimed={sorted(_SESSION_CLAIMED)}) — waiting 30s for a free slot...")

        await asyncio.sleep(30)


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

    except DriveSessionError as e:
        return RunResult(status="drive_error", final_status=str(e),
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
    ensure_browser(cdp_port=config.cdp_port)

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

    if not authusers:
        _p("[FATAL] no usable accounts (all GPU quota exhausted this session)")
        return RunResult(status="gpu_error", final_status="no-accounts",
                         output=None, elapsed=time.time() - t0, authuser="")

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

        try:
            result = await _run_once(config, authuser, t0)
        finally:
            # Release the claim — account is no longer in active use by this
            # coroutine.  GPU/drive blacklists handle re-pick prevention if needed.
            _SESSION_CLAIMED.discard(authuser)

        if result.status == "gpu_error":
            _mark_gpu_quota_failed(authuser)
            _p(f"GPU quota on authuser={authuser}, trying next...")
        elif result.status == "drive_error":
            _mark_drive_session_failed(authuser)
            _p(f"Drive session expired on authuser={authuser}, trying next...")
        else:
            return result

        # Re-probe: other parallel tasks may have claimed accounts since our
        # initial _select_authuser call. A fresh call sees current runtime busy
        # state and timestamps, so the next pick is a genuinely free account.
        authusers = await _select_authuser(candidates, config.cdp_port)
        if not authusers:
            _p("[FATAL] retry: no usable accounts (all quota/session exhausted)")
            break

    return result  # all accounts exhausted


async def _cleanup_orphan_tabs(cdp_port: int) -> None:
    """Close orphan tabs (Welcome / about:blank / OAuth pages) at startup.

    Failed/abandoned uploads from previous runs leave Welcome and blank tabs
    in the browser; they pollute account-discovery probes and waste memory.
    Keep only ``/drive/`` notebook tabs and Drive home.
    """
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
            ctx = browser.contexts[0] if browser.contexts else None
            if not ctx:
                return
            n_closed = 0
            for p in list(ctx.pages):
                url = p.url
                if "/drive/" in url and "colab" in url:
                    continue  # active notebook tab
                if url == "https://drive.google.com/drive/u/0/home":
                    continue  # drive home — keep
                # Close everything else (about:blank, Welcome, OAuth)
                try:
                    await p.close()
                    n_closed += 1
                except Exception:
                    pass
            if n_closed:
                _p(f"[cleanup] closed {n_closed} orphan tabs at startup")
    except Exception as e:
        _p(f"[cleanup] orphan tab cleanup error (ignored): {e}")


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
    if configs:
        ensure_browser(cdp_port=configs[0].cdp_port)
        await _cleanup_orphan_tabs(configs[0].cdp_port)
    return list(await asyncio.gather(*[run_notebook(c) for c in configs]))
