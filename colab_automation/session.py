# colab_automation/session.py
from __future__ import annotations
import asyncio
import re
import time

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from .js import (
    STATUS_JS, CONNECT_JS, GPU_ERR_JS, TOO_MANY_SESSIONS_JS, CLICK_TEXT_JS,
    DRIVE_JS, TAIL_JS, PATCH_CELL_JS, CELL_EXEC_STATE_JS, CELL_ERROR_JS,
    GENERIC_DIALOG_BTN_JS, FIND_VISIBLE_TEXT_JS,
    STILL_THERE_JS,
)
from .config import RunConfig, CellPatch

def _p(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def _is_connected(status: str) -> bool:
    return bool(status) and 'GB/' in str(status)

def _is_gpu(status: str) -> bool:
    return '(GPU)' in str(status) if status else False

def _is_executing(status: str) -> bool:
    s = str(status) if status else ''
    return 'Executing' in s or 'Waiting' in s


# Serialize concurrent Colab UI uploads — two sessions navigating to the
# welcome dialog simultaneously interfere with each other's file input.
_upload_sem = asyncio.Semaphore(1)


class GpuQuotaError(Exception):
    pass

class CellPatchError(Exception):
    """Raised when a CellPatch pattern matches no cell in the notebook."""
    pass

class NotebookError(Exception):
    """Raised when a notebook cell errors out during execution."""
    pass


class ColabSession:
    """
    Low-level Colab CDP session. Use as async context manager.

    All state-changing browser actions are internal methods.
    Callers use: open_notebook, ensure_connected, patch_cells,
    run_and_monitor, extract_output.
    """

    def __init__(self, cdp_port: int = 9223):
        self._cdp_port = cdp_port
        self._pw = None
        self._browser: Browser | None = None
        self._ctx: BrowserContext | None = None

    async def __aenter__(self) -> "ColabSession":
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.connect_over_cdp(
            f"http://127.0.0.1:{self._cdp_port}"
        )
        self._ctx = (
            self._browser.contexts[0]
            if self._browser.contexts
            else await self._browser.new_context()
        )
        _p(f"CDP connected (port {self._cdp_port}): {len(self._ctx.pages)} page(s)")
        return self

    async def __aexit__(self, *_) -> None:
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    async def upload_notebook(self, authuser: str, local_path: str) -> tuple[str, "Page"]:
        """
        Upload a local .ipynb via Colab's 'Open notebook' dialog → 'Upload' sidebar item.

        Flow:
          1. Ctrl+O opens the mwc-dialog with sidebar items (Examples, Recent,
             Google Drive, GitHub, Upload).  All items take ~5 s to load.
          2. Click the 'Upload' md-list-item.
          3. Colab renders an input[type="file"] in the dialog host's light DOM.
          4. ElementHandle.set_input_files() injects the file via CDP's
             DOM.setFileInputFiles — no OS file picker opens.
          5. Colab navigates to /drive/<id>.
        """
        assert self._ctx is not None
        _p(f"  [authuser={authuser}] Uploading {local_path} via Colab UI...")

        page = await self._ctx.new_page()

        async with _upload_sem:
            await page.goto(
                f"https://colab.research.google.com/?authuser={authuser}",
                wait_until="domcontentloaded",
            )
            await asyncio.sleep(8)
            await page.bring_to_front()

            # Check if the 'Open notebook' dialog is already visible (some
            # browser profiles auto-open it on the Colab homepage).  If not,
            # press Ctrl+O to open it.  Either way, wait up to 5 s for all
            # sidebar items (including 'Upload') to finish rendering.
            _FIND_HOST = """
                function findHost(root, depth) {
                    if (depth > 30) return null;
                    for (const el of root.querySelectorAll('*')) {
                        if (el.shadowRoot &&
                            el.shadowRoot.querySelector('[role="dialog"]')) return el;
                        if (el.shadowRoot) {
                            const f = findHost(el.shadowRoot, depth + 1);
                            if (f) return f;
                        }
                    }
                    return null;
                }
            """
            dialog_already_open = await page.evaluate("""() => {
                """ + _FIND_HOST + """
                return !!findHost(document, 0);
            }""")
            if not dialog_already_open:
                await page.keyboard.press("Control+o")
            await asyncio.sleep(5)

            upload_coords = await page.evaluate("""() => {
                """ + _FIND_HOST + """
                const host = findHost(document, 0);
                if (!host) return null;
                for (const item of host.querySelectorAll('md-list-item')) {
                    const span = item.querySelector('span');
                    if (span && span.textContent.trim() === 'Upload') {
                        const r = item.getBoundingClientRect();
                        return {x: r.left + r.width / 2, y: r.top + r.height / 2};
                    }
                }
                return null;
            }""")

            if not upload_coords:
                raise RuntimeError(
                    f"[authuser={authuser}] 'Upload' item not found in Open notebook "
                    "dialog — dialog may not have fully loaded (waited 5 s)"
                )

            await page.mouse.click(upload_coords["x"], upload_coords["y"])
            await asyncio.sleep(1.5)

            # After clicking Upload, Colab appends input[type="file"] to the
            # dialog host's light DOM.  CDP set_input_files() bypasses the OS picker.
            file_input_handle = await page.evaluate_handle("""() => {
                """ + _FIND_HOST + """
                const host = findHost(document, 0);
                return host ? host.querySelector('input[type="file"]') : null;
            }""")

            el = file_input_handle.as_element()
            if el is None:
                raise RuntimeError(
                    f"[authuser={authuser}] No input[type='file'] appeared after "
                    "clicking Upload in the dialog"
                )

            await el.set_input_files(local_path)
            await asyncio.sleep(2)

        _p(f"  [authuser={authuser}] File submitted, waiting for notebook to open...")
        await page.wait_for_url(
            re.compile(r"colab\.research\.google\.com/drive/"),
            timeout=120_000,
        )
        await asyncio.sleep(5)

        m = re.search(r"/drive/([^?#]+)", page.url)
        if not m:
            raise RuntimeError(
                f"Upload succeeded but notebook_id not found in URL: {page.url}"
            )
        notebook_id = m.group(1)
        _p(f"  [authuser={authuser}] Uploaded → notebook_id={notebook_id}")
        return notebook_id, page

    async def open_notebook(self, notebook_id: str, authuser: str) -> Page:
        """Find existing Colab tab or open a new one. Returns the Page."""
        assert self._ctx is not None
        colab_url = (
            f"https://colab.research.google.com/drive/{notebook_id}"
            f"?authuser={authuser}"
        )
        page = next(
            (
                pg for pg in self._ctx.pages
                if "colab.research.google.com" in pg.url
                and notebook_id in pg.url
                and f"authuser={authuser}" in pg.url
            ),
            None,
        )
        if page:
            _p(f"Found existing tab: {page.url[:80]}")
        else:
            _p(f"Opening {colab_url[:80]}")
            page = await self._ctx.new_page()
            await page.goto(colab_url, wait_until="domcontentloaded")
            _p("Waiting for notebook to load (30s)...")
            await asyncio.sleep(30)
        await page.bring_to_front()
        return page

    async def _change_runtime_to_gpu(self, page: Page) -> None:
        """Switch notebook runtime to GPU via Runtime > Change runtime type dialog."""
        _p("  Switching to GPU runtime...")

        # Close any stray open menus first.
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.5)

        # Find all "Runtime" text nodes in shadow DOM and click the one in the
        # top menu bar (getBoundingClientRect().top < 60px).  CLICK_TEXT_JS
        # does DFS and may find a hidden File-menu element first — position
        # filtering avoids that false match.
        CLICK_RUNTIME_MENUBAR_JS = r"""() => {
            const hits = [];
            function df(node, d=0) {
                if (d > 40) return;
                if (node.nodeType === 3 && node.textContent.trim() === 'Runtime') {
                    const p = node.parentElement;
                    if (p) {
                        const r = p.getBoundingClientRect();
                        hits.push({el: p, top: r.top, left: r.left, w: r.width, h: r.height});
                    }
                }
                if (node.shadowRoot) df(node.shadowRoot, d+1);
                for (const c of (node.childNodes||[])) df(c, d+1);
            }
            df(document);
            // Prefer a visible element in the top menu bar (top < 60px)
            const menubar = hits.filter(h => h.top >= 0 && h.top < 60 && h.w > 0 && h.h > 0);
            const target = menubar[0] || hits.find(h => h.w > 0 && h.h > 0);
            if (target) { target.el.click(); return {ok: true, top: target.top, left: target.left}; }
            return {ok: false, found: hits.length};
        }"""
        result = await page.evaluate(CLICK_RUNTIME_MENUBAR_JS)
        _p(f"  Runtime menu click: {result}")
        if result.get("ok"):
            # JS element.click() doesn't open dropdown menus properly —
            # items render with zero bounding boxes.  Use mouse.click() at
            # the element's screen coordinates for a real user interaction.
            await page.mouse.click(result["left"] + 20, result["top"] + 10)
        await asyncio.sleep(1.5)

        # Click "Change runtime type" in the dropdown.
        # Both CLICK_TEXT_JS (clicks hidden DOM nodes) and get_by_text (can't pierce
        # this shadow DOM) fail.  Instead: find the VISIBLE menu item by
        # getBoundingClientRect (non-zero bounds = actually rendered on screen),
        # extract its centre coordinates, and click by mouse position.
        CLICK_VISIBLE_TEXT_JS = r"""(text) => {
            function df(node, d=0) {
                if (d > 60) return null;
                if (node.nodeType === 3 && node.textContent.trim() === text) {
                    const p = node.parentElement;
                    if (p) {
                        const r = p.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0 && r.top > 60) {
                            return {x: r.left + r.width / 2, y: r.top + r.height / 2};
                        }
                    }
                }
                if (node.shadowRoot) { const r = df(node.shadowRoot, d+1); if (r) return r; }
                for (const c of (node.childNodes||[])) { const r = df(c, d+1); if (r) return r; }
                return null;
            }
            return df(document);
        }"""
        coords = await page.evaluate(CLICK_VISIBLE_TEXT_JS, "Change runtime type")
        if coords:
            await page.mouse.click(coords["x"], coords["y"])
            _p(f"  Change runtime type clicked at {coords}")
        else:
            _p("  WARNING: 'Change runtime type' not visible in dropdown")
        await asyncio.sleep(1)   # brief pause for dialog to open

        # GPU chip buttons load async.  Use Playwright's get_by_text which
        # pierces shadow DOM reliably — our custom JS DFS misses these chips.
        GPU_LABELS = ["T4 GPU", "T4", "A100 GPU", "A100", "V100 GPU", "V100", "L4 GPU", "L4"]

        gpu_selected = False
        deadline_gpu = time.time() + 20
        while not gpu_selected and time.time() < deadline_gpu:
            for label in GPU_LABELS:
                loc = page.get_by_text(label, exact=True)
                try:
                    if await loc.count() > 0:
                        await loc.first.click()
                        _p(f"  Selected GPU chip: {label!r}")
                        gpu_selected = True
                        break
                except Exception:
                    pass
            if not gpu_selected:
                await asyncio.sleep(2)

        if not gpu_selected:
            # No GPU chip appeared after 20s — account likely has no GPU quota.
            # Dump page text for diagnostics, then close dialog.
            DUMP_ALL_JS = r"""() => {
                const out = [];
                function df(node, d=0) {
                    if (d > 50) return;
                    if (node.nodeType === 3) {
                        const t = node.textContent.trim();
                        if (t.length > 1 && t.length < 40) out.push(t);
                    }
                    if (node.shadowRoot) df(node.shadowRoot, d+1);
                    for (const c of (node.childNodes||[])) df(c, d+1);
                }
                df(document);
                return [...new Set(out)];
            }"""
            labels = await page.evaluate(DUMP_ALL_JS)
            _p(f"  No GPU chip found. Dialog labels: {labels}")
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)
            _p("  No GPU available for this account — raising GpuQuotaError")
            raise GpuQuotaError("No GPU option in Change Runtime Type dialog (account may have exhausted quota)")

        await asyncio.sleep(1)
        # Click the Save button inside the dialog.
        # Playwright's get_by_role skips hidden elements (including File menu's 'Save'),
        # so it reliably targets the dialog's visible Save button.
        try:
            await page.get_by_role("button", name="Save").click(timeout=5_000)
            _p("  Dialog closed via button[role=button, name=Save]")
        except Exception:
            # Fallback: position-filtered CLICK_TEXT_JS — dialog Save is below y=100
            CLICK_SAVE_DIALOG_JS = r"""() => {
                function df(node, d=0) {
                    if (d > 40) return false;
                    if (node.nodeType === 3 && node.textContent.trim() === 'Save') {
                        const p = node.parentElement;
                        if (p) {
                            const r = p.getBoundingClientRect();
                            if (r.top > 100 && r.width > 0 && r.height > 0) {
                                p.click(); return true;
                            }
                        }
                    }
                    if (node.shadowRoot && df(node.shadowRoot, d+1)) return true;
                    for (const c of (node.childNodes||[])) { if (df(c, d+1)) return true; }
                    return false;
                }
                return df(document);
            }"""
            clicked = await page.evaluate(CLICK_SAVE_DIALOG_JS)
            _p(f"  Dialog closed via position-filtered Save: {clicked}")
        _p("  Runtime type saved — waiting for restart...")
        await asyncio.sleep(15)

    async def disconnect_and_delete_runtime(self, page: Page) -> None:
        """Disconnect and delete the Colab runtime via Runtime menu.
        Call before closing a tab to free cloud resources."""
        _p("  Disconnecting and deleting runtime...")
        try:
            # Close any stray menus first
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.3)

            # Step 1: open Runtime menu.
            # JS element.click() alone won't render dropdown items — must follow up
            # with page.mouse.click() at screen coordinates for a real interaction.
            _RUNTIME_MENU_JS = r"""() => {
                const hits = [];
                function df(node, d=0) {
                    if (d > 40) return;
                    if (node.nodeType === 3 && node.textContent.trim() === 'Runtime') {
                        const p = node.parentElement;
                        if (p) {
                            const r = p.getBoundingClientRect();
                            hits.push({el: p, top: r.top, left: r.left, w: r.width, h: r.height});
                        }
                    }
                    if (node.shadowRoot) df(node.shadowRoot, d+1);
                    for (const c of (node.childNodes||[])) df(c, d+1);
                }
                df(document);
                const menubar = hits.filter(h => h.top >= 0 && h.top < 60 && h.w > 0 && h.h > 0);
                const target = menubar[0] || hits.find(h => h.w > 0 && h.h > 0);
                if (target) { target.el.click(); return {ok: true, top: target.top, left: target.left}; }
                return {ok: false};
            }"""
            result = await page.evaluate(_RUNTIME_MENU_JS)
            _p(f"  Runtime menu: {result}")
            if result.get("ok"):
                await page.mouse.click(result["left"] + 20, result["top"] + 10)
            await asyncio.sleep(1.5)

            # Step 2: click "Disconnect and delete runtime" in the open dropdown.
            # Use FIND_VISIBLE_TEXT_JS (shadow-DOM walk, only returns visible items)
            # + mouse.click so the click registers on the rendered menu item.
            coords = await page.evaluate(FIND_VISIBLE_TEXT_JS, "Disconnect and delete runtime")
            if coords:
                _p(f"  Menu item found at {coords}, clicking...")
                await page.mouse.click(coords["x"], coords["y"])
            else:
                _p("  WARNING: menu item not visible — trying JS fallback")
                await page.evaluate(r"""() => {
                    function walk(root, d=0) {
                        if (d > 30) return;
                        const el = root.querySelector('[command="powerwash-current-vm"]');
                        if (el) { el.click(); return; }
                        for (const c of root.querySelectorAll('*'))
                            if (c.shadowRoot) walk(c.shadowRoot, d+1);
                    }
                    walk(document);
                }""")
            await asyncio.sleep(0.5)

            # Step 3: confirm the dialog (with retries).
            # The confirmation button has slot="primaryAction" in the dialog's light DOM.
            # Use FIND_VISIBLE_TEXT_JS to locate it by visible text, then mouse.click.
            # Common button texts: "Delete", "DELETE", "Disconnect".
            confirmed = False
            for retry in range(4):
                if retry > 0:
                    await asyncio.sleep(0.5)
                for btn_text in ("Delete", "DELETE", "Disconnect"):
                    btn_coords = await page.evaluate(FIND_VISIBLE_TEXT_JS, btn_text)
                    if btn_coords:
                        _p(f"  Confirm button {btn_text!r} at {btn_coords}")
                        await page.mouse.click(btn_coords["x"], btn_coords["y"])
                        confirmed = True
                        break
                if confirmed:
                    break
            if not confirmed:
                # Fallback: JS click on slot="primaryAction" element
                fallback = await page.evaluate(r"""() => {
                    function walk(root) {
                        let el = root.querySelector('[slot="primaryAction"]');
                        if (el) {
                            const btn = el.querySelector('button') ||
                                        (el.shadowRoot && el.shadowRoot.querySelector('button')) ||
                                        el;
                            btn.click();
                            return btn.textContent.trim();
                        }
                        for (const c of root.querySelectorAll('*'))
                            if (c.shadowRoot) { const r = walk(c.shadowRoot); if (r) return r; }
                        return null;
                    }
                    return walk(document);
                }""")
                if fallback:
                    _p(f"  Fallback confirm: {fallback!r}")
                else:
                    _p("  No confirm dialog found")
            await asyncio.sleep(2.0)
        except Exception as e:
            _p(f"  disconnect_and_delete_runtime failed (ignoring): {e}")

    async def ensure_connected(self, page: Page, max_wait: int = 300,
                               require_gpu: bool = False) -> None:
        """Connect runtime if not already connected, then switch to GPU if required.
        Raises GpuQuotaError if GPU quota is exhausted or unavailable."""
        status = await page.evaluate(STATUS_JS)
        _p(f"Runtime status: {status!r}  connected={_is_connected(status)}")

        # Phase 1: Connect if not already connected
        if not _is_connected(status):
            _p("Connecting runtime...")
            await page.evaluate(CONNECT_JS)
            await asyncio.sleep(3)

            deadline = time.time() + max_wait
            tick = 0
            while time.time() < deadline:
                await asyncio.sleep(5)
                tick += 1
                await self._handle_all_dialogs(page, label=f"connect {5*tick}s")
                # Re-click Connect each tick in case the button reappeared
                await page.evaluate(CONNECT_JS)
                status = await page.evaluate(STATUS_JS)
                _p(f"  [{5*tick}s] {status!r}")
                if _is_connected(status):
                    _p("Connected!")
                    break
            else:
                raise TimeoutError(
                    f"Runtime never connected after {max_wait}s. Last: {status!r}"
                )

        # Phase 2: Switch to GPU if required and currently on CPU
        if require_gpu and not _is_gpu(status):
            _p("CPU runtime detected — switching to GPU")
            await self._change_runtime_to_gpu(page)
            # Runtime disconnects and reconnects after type change; wait for it
            await asyncio.sleep(5)
            status = await page.evaluate(STATUS_JS)
            if not _is_connected(status):
                # After GPU type change, Colab shows "Click to connect" instead of
                # auto-connecting — click Connect explicitly, then poll.
                _p("  GPU runtime not yet connected — clicking Connect")
                await page.evaluate(CONNECT_JS)
                await asyncio.sleep(3)
                status = await page.evaluate(STATUS_JS)

                deadline = time.time() + 120
                tick = 0
                while not _is_connected(status) and time.time() < deadline:
                    await asyncio.sleep(5)
                    tick += 1
                    await self._handle_all_dialogs(page, label=f"gpu-reconnect {5*tick}s")
                    # Re-click Connect each tick in case it needs another prompt
                    await page.evaluate(CONNECT_JS)
                    status = await page.evaluate(STATUS_JS)
                    _p(f"  [gpu-reconnect {5*tick}s] {status!r}")

                if _is_connected(status):
                    _p(f"Reconnected after GPU switch: {status[:60]!r}")
                else:
                    # Dump page text to help diagnose why reconnect failed
                    try:
                        text = await page.inner_text("body")
                        _p(f"  [gpu-reconnect] page text at timeout: {text[:400]!r}")
                    except Exception:
                        pass
                    raise TimeoutError("Runtime never reconnected after GPU switch")

    async def patch_cells(self, page: Page, patches: list[CellPatch]) -> None:
        """Apply each CellPatch to the notebook in order, using notebookModel.setValue()."""
        for patch in patches:
            if patch.replace_fn is not None:
                result = await self._patch_with_fn(page, patch)
            else:
                flags_str = "m" if patch.flags & re.MULTILINE else ""
                result = await page.evaluate(
                    PATCH_CELL_JS, [patch.pattern, patch.replace or "", flags_str]
                )
            _p(f"  patch {patch.pattern!r}: {result}")
            if isinstance(result, dict) and "error" in result:
                raise CellPatchError(
                    f"Pattern {patch.pattern!r} matched no cell — "
                    f"check the notebook for the exact current content."
                )

    async def _patch_with_fn(self, page: Page, patch: CellPatch) -> dict:
        """Read cell source into Python, apply replace_fn, write back via JS."""
        FIND_CELL_JS = """(pattern) => {
            try {
                const nm = colab.global.notebookModel;
                const re = new RegExp(pattern, 'm');
                for (let i = 0; i < nm.cells.length; i++) {
                    const tm = nm.cells[i]?.textModel;
                    if (!tm) continue;
                    if (re.test(tm.getValue()))
                        return {i, source: tm.getValue()};
                }
                return null;
            } catch(e) { return {error: e.toString()}; }
        }"""
        READ_AND_PATCH_JS = """([pattern, flags, newSource]) => {
            try {
                const nm = colab.global.notebookModel;
                const re = new RegExp(pattern, flags);
                for (let i = 0; i < nm.cells.length; i++) {
                    const tm = nm.cells[i]?.textModel;
                    if (!tm) continue;
                    if (re.test(tm.getValue())) {
                        tm.setValue(newSource);
                        return {ok: true, cellIdx: i};
                    }
                }
                return {error: 'no match'};
            } catch(e) { return {error: e.toString()}; }
        }"""
        found = await page.evaluate(FIND_CELL_JS, patch.pattern)
        if not found or "error" in found:
            raise CellPatchError(
                f"Pattern {patch.pattern!r} matched no cell — "
                f"check the notebook for the exact current content."
            )
        new_source = patch.replace_fn(found["source"])
        flags_str = "m" if patch.flags & re.MULTILINE else ""
        return await page.evaluate(
            READ_AND_PATCH_JS, [patch.pattern, flags_str, new_source]
        )

    # ── Internal execution methods (not for callers) ───────────────────────

    async def _handle_oauth(self) -> None:
        """Handle all open accounts.google.com tabs: accountchooser → sign-in → Continue."""
        assert self._ctx is not None
        for rnd in range(20):
            tabs = [pg for pg in self._ctx.pages if "accounts.google.com" in pg.url]
            if not tabs:
                _p(f"  OAuth done ({rnd} rounds)")
                return
            _p(f"  OAuth round {rnd}: {len(tabs)} tab(s)")
            for tab in tabs:
                try:
                    await tab.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                url = tab.url
                # Try to click common OAuth buttons via dispatchEvent
                # (Google's jsaction buttons don't respond to mouse.click())
                action = 'no-action'
                for btn_text in ['Continue', 'Allow', 'Next', 'Sign in', 'Yes']:
                    try:
                        clicked = await tab.evaluate(r"""(text) => {
                            const buttons = document.querySelectorAll('button');
                            for (const b of buttons) {
                                if (b.innerText?.trim() === text && !b.disabled) {
                                    b.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                                    return true;
                                }
                            }
                            return false;
                        }""", btn_text)
                        if clicked:
                            action = f'clicked:{btn_text}'
                            break
                    except:
                        pass

                # Fallback to old DOM-based approach for account chooser
                if action == 'no-action' and 'accountchooser' in tab.url:
                    try:
                        action = await tab.evaluate(r"""() => {
                            const m = window.location.href.match(/authuser=(\d+)/);
                            const idx = m ? parseInt(m[1]) : 0;
                            const accounts = document.querySelectorAll('[data-identifier]');
                            const target = accounts[idx] || accounts[0];
                            if (target) { target.click(); return 'chooser:' + (target.getAttribute('data-identifier') || idx); }
                            return 'chooser:no-accounts';
                        }""")
                    except:
                        pass

                # Diagnostic: if no-action, print all visible text on the page
                if action == 'no-action' and rnd < 3:  # only on first few rounds to avoid spam
                    try:
                        visible_text = await tab.evaluate("""() => {
                            const texts = [];
                            const walk = (node) => {
                                if (node.nodeType === 3) {
                                    const t = node.textContent.trim();
                                    if (t.length > 3 && t.length < 100) texts.push(t);
                                } else {
                                    if (node.nodeType === 1) {
                                        const r = node.getBoundingClientRect();
                                        if (r.width > 0 && r.height > 0) {
                                            const t = node.innerText?.trim();
                                            if (t && t.length > 3 && t.length < 100) texts.push(t);
                                        }
                                    }
                                    if (node.shadowRoot) walk(node.shadowRoot);
                                    for (const c of node.childNodes) walk(c);
                                }
                            };
                            walk(document);
                            return [...new Set(texts)].slice(0, 20);
                        }""")
                        _p(f"    Visible text on page: {visible_text}")
                    except Exception as e:
                        _p(f"    Diagnostic error: {e}")

                _p(f"    {url[:60]}: {action}")
                await asyncio.sleep(3)
            await asyncio.sleep(2)
        _p("  Warning: OAuth not resolved after 20 rounds — proceeding anyway")

    async def _handle_drive_and_oauth(self, page: Page) -> None:
        """Click Drive dialog → handle OAuth → wait until all OAuth tabs gone."""
        _p("  Drive dialog — clicking 'Connect to Google Drive'")
        coords = await page.evaluate(FIND_VISIBLE_TEXT_JS, "Connect to Google Drive")
        if coords:
            await page.mouse.click(coords['x'], coords['y'])
        else:
            # Fallback to text search if coordinates not found
            await page.evaluate(CLICK_TEXT_JS, "Connect to Google Drive")
        await asyncio.sleep(4)
        await self._handle_oauth()

    async def _read_output_frames(self, page: Page) -> list[str]:
        """Read recent output lines from cell outputs.

        Reads from two sources (current notebook only):
        1. TAIL_JS — shadow DOM walk (works in normal mode, outputs in DOM)
        2. colab output iframes (works in private outputs mode)

        Returns a flat list of non-empty lines.
        """
        lines: list[str] = []

        # Source 1: shadow DOM in main frame (non-private outputs mode)
        try:
            result = await page.evaluate(TAIL_JS)
            if isinstance(result, list):
                lines.extend(result)
        except Exception as e:
            pass  # TAIL_JS failures are expected in some contexts

        # Source 2: colab output iframes (private outputs mode)
        # Note: outputframe iframes are cross-origin googleusercontent.com iframes
        # accessed via DOM query, not Playwright's frame list
        try:
            iframes = await page.query_selector_all("iframe")
            iframe_count = len(iframes) if iframes else 0
            for iframe in (iframes or []):
                try:
                    src = await iframe.get_attribute("src")
                    if src and "outputframe" in src:
                        # Access via content_frame() for cross-origin iframes
                        frame = await iframe.content_frame()
                        if frame:
                            text = await frame.inner_text("body")
                            for line in text.splitlines():
                                line = line.strip()
                                if line and line not in lines:
                                    lines.append(line)
                except Exception as e:
                    pass  # Skip iframes we can't read
        except Exception as e:
            pass  # Iframe query failures are acceptable

        return lines

    async def _handle_too_many_sessions(self, page: Page, label: str = "") -> None:
        """Handle 'Too many sessions' dialog: click 'Manage sessions' button inside dialog,
        then 'Terminate other sessions', then Escape to close the sessions panel."""
        pre = f"  [{label}] " if label else "  "
        _p(f"{pre}Too many sessions — clicking 'Manage sessions' in dialog")

        # 1. Click "Manage sessions" button that's inside the "Too many sessions" dialog
        coords = await page.evaluate(FIND_VISIBLE_TEXT_JS, "Manage sessions")
        if coords:
            await page.mouse.click(coords["x"], coords["y"])
            _p(f"{pre}  Manage sessions clicked at ({coords['x']:.0f}, {coords['y']:.0f})")
        else:
            _p(f"{pre}  'Manage sessions' not found in dialog")
        await asyncio.sleep(1.5)

        # 2. Click "Terminate other sessions" from the sessions panel
        coords2 = await page.evaluate(FIND_VISIBLE_TEXT_JS, "Terminate other sessions")
        if coords2:
            await page.mouse.click(coords2["x"], coords2["y"])
            _p(f"{pre}  Terminate other sessions clicked at ({coords2['x']:.0f}, {coords2['y']:.0f})")
        else:
            _p(f"{pre}  'Terminate other sessions' not found — sessions may already be clean")
        await asyncio.sleep(2)

        # 3. Close the sessions panel
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.5)

    async def _handle_all_dialogs(self, page: Page, label: str = "",
                                   handle_drive: bool = False) -> list[str]:
        """Check and handle all known Colab dialogs. Returns list of actions taken.

        Args:
            handle_drive: If True, also check and handle Drive mount dialog.
                          Set False in reconnect loops (Drive appears during cell run, not reconnect).
        """
        handled: list[str] = []
        pre = f"  [{label}] " if label else "  "

        if handle_drive:
            try:
                # Check for Drive dialog using coordinate-based detection
                coords = await page.evaluate(FIND_VISIBLE_TEXT_JS, "Connect to Google Drive")
                if coords:
                    _p(f"{pre}Drive dialog — handling")
                    await self._handle_drive_and_oauth(page)
                    handled.append("drive")
            except Exception as e:
                _p(f"{pre}drive check error: {e}")

        try:
            if await page.evaluate(TOO_MANY_SESSIONS_JS):
                await self._handle_too_many_sessions(page, label=label)
                handled.append("too_many_sessions")
        except Exception as e:
            _p(f"{pre}too_many check error: {e}")

        try:
            if await page.evaluate(GPU_ERR_JS):
                raise GpuQuotaError("GPU quota exhausted")
        except GpuQuotaError:
            raise
        except Exception as e:
            _p(f"{pre}gpu_err check error: {e}")

        try:
            coords = await page.evaluate(STILL_THERE_JS)
            if coords:
                _p(f"{pre}'Are you still there?' dialog — clicking primary action")
                await page.mouse.click(coords["x"], coords["y"])
                await asyncio.sleep(1)
                handled.append("still_there")
        except Exception as e:
            _p(f"{pre}still_there check error: {e}")

        try:
            coords = await page.evaluate(FIND_VISIBLE_TEXT_JS, "Run anyway")
            if coords:
                await page.mouse.click(coords["x"], coords["y"])
                _p(f"{pre}'Run anyway' clicked")
                handled.append("run_anyway")
        except Exception as e:
            _p(f"{pre}run_anyway check error: {e}")

        # Generic fallback: any visible dialog/modal button not already handled
        try:
            btn = await page.evaluate(GENERIC_DIALOG_BTN_JS)
            if btn and btn["text"] not in ("Run anyway",):  # avoid re-clicking already handled
                _p(f"{pre}Generic dialog: clicking '{btn['text']}' at ({btn['x']:.0f}, {btn['y']:.0f})")
                await page.mouse.click(btn["x"], btn["y"])
                await asyncio.sleep(1)
                handled.append(f"generic:{btn['text']}")
        except Exception as e:
            _p(f"{pre}generic dialog check error: {e}")

        return handled

    async def _fire_run(self, page: Page) -> None:
        """Send Ctrl+F9 to run all cells. Click canvas first to ensure keyboard focus."""
        # Escape first — closes any stray dialog that would swallow Ctrl+F9
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.3)
        await page.mouse.click(600, 400)
        await asyncio.sleep(0.3)
        await page.keyboard.press("Control+F9")
        _p("  Ctrl+F9 sent")

    async def run_and_monitor(self, page: Page, config: RunConfig) -> str:
        """
        Check Drive dialog, fire Ctrl+F9, then run two-phase monitoring loop.
        Returns the final STATUS_JS value.
        """
        assert self._ctx is not None

        # Pre-run: handle any Drive dialog visible before Ctrl+F9
        coords = await page.evaluate(FIND_VISIBLE_TEXT_JS, "Connect to Google Drive")
        if coords:
            _p("Drive dialog before Ctrl+F9 — handling first")
            await self._handle_drive_and_oauth(page)
            await asyncio.sleep(2)

        await self._fire_run(page)

        # Post-run: check for Drive dialog that appears immediately after Ctrl+F9
        await asyncio.sleep(1)
        coords = await page.evaluate(FIND_VISIBLE_TEXT_JS, "Connect to Google Drive")
        if coords:
            _p("Drive dialog after Ctrl+F9 — handling")
            await self._handle_drive_and_oauth(page)
            await asyncio.sleep(2)

        # Dense rapid-poll phase
        _p(f"Dense poll ({config.dense_interval}s interval)...")
        pivot_idx = await self._find_pivot_cell_idx(page, config.pivot_cell_pattern)
        in_sparse = False
        was_executing = False
        status = "no-status"

        deadline = time.time() + config.max_run_wait
        dense_ticks = 0
        tick = 0
        idle_streak = 0   # consecutive "connected + not executing" ticks

        while time.time() < deadline:
            interval = config.sparse_interval if in_sparse else config.dense_interval
            await asyncio.sleep(interval)
            tick += 1

            # 1. Drive dialog — always check first, regardless of OAuth tabs
            try:
                coords = await page.evaluate(FIND_VISIBLE_TEXT_JS, "Connect to Google Drive")
                if coords:
                    _p(f"  [tick {tick}] Drive dialog — handling")
                    await self._handle_drive_and_oauth(page)
                    await self._fire_run(page)
                    await asyncio.sleep(2)
                    tick = 0; dense_ticks = 0; in_sparse = False
                    was_executing = False; idle_streak = 0
                    continue
            except Exception as e:
                _p(f"  drive check error: {e}")

            # 2. OAuth popup tabs — skip blank/stale tabs (no visible content)
            try:
                assert self._ctx is not None
                oauth_tabs = [pg for pg in self._ctx.pages if "accounts.google.com" in pg.url]
                # Only handle if at least one tab has actual content (not just blank page)
                active_oauth = []
                for ot in oauth_tabs:
                    try:
                        body_len = await ot.evaluate("() => document.body?.innerText?.length || 0")
                        if body_len > 10:
                            active_oauth.append(ot)
                    except Exception:
                        pass
                if active_oauth:
                    _p(f"  [tick {tick}] OAuth tab open — handling ({len(active_oauth)} active)")
                    await self._handle_oauth()
                    # After OAuth, check Drive dialog — it may have appeared during OAuth handling
                    coords2 = await page.evaluate(FIND_VISIBLE_TEXT_JS, "Connect to Google Drive")
                    if coords2:
                        _p(f"  [tick {tick}] Drive dialog appeared during OAuth — handling")
                        await self._handle_drive_and_oauth(page)
                        await self._fire_run(page)
                        await asyncio.sleep(2)
                        tick = 0; dense_ticks = 0; in_sparse = False
                        was_executing = False; idle_streak = 0
                    continue
                elif oauth_tabs:
                    _p(f"  [tick {tick}] {len(oauth_tabs)} stale OAuth tab(s) — skipping")
            except Exception as e:
                _p(f"  oauth check error: {e}")

            # 3. All known Colab dialogs (TOO_MANY, GPU_ERR, Run anyway, generic)
            try:
                handled = await self._handle_all_dialogs(
                    page, label=f"tick {tick}", handle_drive=False
                )
                if "drive" in handled:
                    # Drive just mounted — re-fire Ctrl+F9 and reset counters
                    await self._fire_run(page)
                    await asyncio.sleep(2)
                    tick = 0
                    dense_ticks = 0
                    in_sparse = False
                    was_executing = False
                    idle_streak = 0
                    continue
                if handled:
                    continue
            except GpuQuotaError:
                raise
            except Exception as e:
                _p(f"  dialog check error: {e}")

            # 3. Status
            try:
                status = await page.evaluate(STATUS_JS)
            except Exception as e:
                _p(f"  status eval error: {e}")
                break

            if _is_executing(status):
                was_executing = True
                idle_streak = 0
            elif _is_connected(status):
                idle_streak += 1
            else:
                idle_streak = 0

            # Log every tick in dense phase; periodically in sparse
            if not in_sparse:
                _p(f"  [tick {tick}] {status!r}  exec={_is_executing(status)}  idle_streak={idle_streak}")
            else:
                # Every sparse tick: check for errors via multiple methods
                # Method 1: executionState (may be unavailable in private outputs mode)
                try:
                    cell_err = await page.evaluate(CELL_ERROR_JS)
                    if cell_err.get("error"):
                        idx = cell_err.get("cellIdx", "?")
                        src = cell_err.get("source", "unknown")
                        _p(f"[ERROR] Cell {idx} state={cell_err.get('state')!r} (via {src})")
                        raise NotebookError(
                            f"Execution error — cell {idx} state={cell_err.get('state')!r}"
                        )
                except NotebookError:
                    raise
                except Exception as e:
                    _p(f"  [sparse] cell_err check failed: {type(e).__name__}")

                # Method 2: read and check output (always available)
                try:
                    tail = await self._read_output_frames(page)
                    tail_text = "\n".join(tail)
                    if tail:
                        _p(f"  [sparse] output: {len(tail)} lines")
                    _ERR_KWDS = ("Traceback (most recent call last)", "--- FAILED", "FileNotFoundError", "RuntimeError:", "Error:")
                    for kw in _ERR_KWDS:
                        if kw in tail_text:
                            _p(f"[ERROR] Detected '{kw}' in output during sparse poll")
                            for line in tail[-20:]:
                                _p(f"  > {line}")
                            raise NotebookError(f"Error detected in output: {tail_text[-500:]!r}")
                except NotebookError:
                    raise
                except Exception as e:
                    _p(f"  [sparse] output check error: {type(e).__name__}")
                # Periodic: log tail
                if tick % max(1, int(120 / config.sparse_interval)) == 0:
                    _p(f"  [tick {tick}] {status!r}")
                    for line in tail[-15:]:
                        _p(f"    > {line}")

            # Pivot: switch dense → sparse when target cell starts running
            if not in_sparse:
                dense_ticks += 1
                if pivot_idx is not None:
                    cell_state = await page.evaluate(CELL_EXEC_STATE_JS, config.pivot_cell_pattern)
                    if cell_state and cell_state not in ("unknown", None) and "running" in str(cell_state).lower():
                        _p(f"  Pivot cell running — switching to sparse ({config.sparse_interval}s)")
                        in_sparse = True
                elif dense_ticks * config.dense_interval >= 30:
                    # No pivot_cell_pattern: switch after 30s dense
                    _p(f"  30s elapsed — switching to sparse ({config.sparse_interval}s)")
                    in_sparse = True

            # Done A: idle after execution — check for cell errors first.
            if idle_streak >= 3 and tick > 10 and was_executing:
                cell_err = await page.evaluate(CELL_ERROR_JS)
                if cell_err.get("error"):
                    idx = cell_err.get("cellIdx", "?")
                    preview = cell_err.get("preview", "")
                    _p(f"[ERROR] Cell {idx} state={cell_err.get('state')!r}: {preview!r}")
                    tail = await self._read_output_frames(page)
                    for line in tail:
                        _p(f"  > {line}")
                    raise NotebookError(
                        f"Execution error — cell {idx} state={cell_err.get('state')!r}"
                    )
                # Fallback: some exceptions (e.g. FileNotFoundError) don't set
                # executionState to 'error'. Check output for tracebacks.
                # Use _read_output_frames (not TAIL_JS) to detect errors in private outputs mode.
                tail_lines = await self._read_output_frames(page)
                if isinstance(tail_lines, list):
                    tail_text = "\n".join(tail_lines)
                    _p(f"  [idle-check] output: {len(tail_lines)} lines, {len(tail_text)} chars")
                    _ERR_KWDS = ("Traceback (most recent call last)", "RuntimeError:", "Error:")
                    for kw in _ERR_KWDS:
                        if kw in tail_text:
                            _p(f"[ERROR] Detected '{kw}' in output at idle")
                            for line in tail_lines[-30:]:
                                _p(f"  > {line}")
                            raise NotebookError(f"Execution error — {kw} in output")
                _p("Idle — notebook complete.")
                break
            # Done B: runtime disconnected mid-run.
            # Could be runtime.unassign() (normal completion) OR GPU quota exhausted
            # (Colab kills kernel with KeyboardInterrupt and disconnects).
            # Strategy: click Connect and check for GPU quota dialog.
            # If quota dialog → GpuQuotaError; if reconnects → re-fire; if stays
            # no-status → treat as intentional unassign (complete).
            if status == "no-status" and was_executing and tick > 3:
                # Read output frames before they disappear on reconnect attempt
                mid_tail = await self._read_output_frames(page)
                if mid_tail:
                    _p("  Last output before disconnect:")
                    for line in mid_tail[-10:]:
                        _p(f"    > {line}")
                # Check for cell errors while DOM still reflects pre-disconnect state.
                # Notebook errors cause runtime to disconnect just like runtime.unassign(),
                # so we must distinguish here before attempting reconnect.
                #
                # Two-pronged check:
                # 1. CELL_ERROR_JS — reliable when colab.global is still accessible
                #    (may return {error:false} if runtime already fully torn down)
                # 2. "Traceback (most recent call last)" in mid_tail — extremely specific,
                #    only appears in real Python tracebacks, not in any Colab UI banner.
                #    Safe to use unlike broad keywords ("Error:", "Exception:").
                _error_detected = False
                try:
                    cell_err = await page.evaluate(CELL_ERROR_JS)
                    if cell_err.get("error"):
                        idx = cell_err.get("cellIdx", "?")
                        preview = cell_err.get("preview", "")
                        _p(f"[ERROR] Cell {idx} state={cell_err.get('state')!r}: {preview!r}")
                        _error_detected = True
                except Exception as _cell_chk_err:
                    _p(f"  cell error check skipped: {_cell_chk_err}")

                mid_tail_text = "\n".join(mid_tail)
                if not _error_detected and "Traceback (most recent call last)" in mid_tail_text:
                    _p("[ERROR] Traceback detected in output before disconnect")
                    _error_detected = True

                if _error_detected:
                    for line in mid_tail:
                        _p(f"  > {line}")
                    raise NotebookError("Execution error detected before runtime disconnect")
                _p("Runtime disconnected mid-run — clicking Connect to probe GPU quota...")
                await page.evaluate(CONNECT_JS)
                # Poll up to 30s for status to settle (connected or quota dialog).
                # A slow reconnect may still show "no-status" at 5s but connect later.
                for _probe_i in range(6):
                    await asyncio.sleep(5)
                    try:
                        await self._handle_all_dialogs(page, label="quota-probe")
                    except GpuQuotaError:
                        raise
                    probe_status = await page.evaluate(STATUS_JS)
                    if probe_status != "no-status":
                        break  # settled: connected or connecting
                if probe_status == "no-status":
                    # Stayed disconnected after 30s — intentional unassign, complete.
                    _p("Runtime unassigned after execution — complete.")
                    status = probe_status
                    break
                elif _is_connected(probe_status):
                    # Reconnected — could be OAuth-triggered re-run or spurious reconnect.
                    # If not executing, this is a wasted GPU session: disconnect and declare complete.
                    if not ("Executing" in probe_status or "Waiting" in probe_status):
                        _p("  Runtime reconnected but idle after unassign — disconnecting to avoid wasting quota.")
                        try:
                            await self.disconnect_and_delete_runtime(page)
                        except Exception:
                            pass
                        status = "no-status"
                        break
                    else:
                        _p(f"  Reconnected and executing after mid-run disconnect: {probe_status[:60]!r} — re-firing")
                        status = probe_status
                        await self._fire_run(page)
                        await asyncio.sleep(2)
                        tick = 0
                        dense_ticks = 0
                        in_sparse = False
                        was_executing = False
                        idle_streak = 0
                        continue
                else:
                    # Still connecting — disconnect and declare complete
                    _p("  Runtime reconnecting after unassign — disconnecting to avoid wasting quota.")
                    try:
                        await self.disconnect_and_delete_runtime(page)
                    except Exception:
                        pass
                    status = "no-status"
                    break

        return status

    async def _find_pivot_cell_idx(self, page: Page, pattern: str | None) -> int | None:
        """Return index of first cell matching pattern, or None."""
        if pattern is None:
            return None
        FIND_JS = """(pat) => {
            try {
                const nm = colab.global.notebookModel;
                const re = new RegExp(pat, 'm');
                for (let i = 0; i < nm.cells.length; i++) {
                    const tm = nm.cells[i]?.textModel;
                    if (tm && re.test(tm.getValue())) return i;
                }
                return null;
            } catch(e) { return null; }
        }"""
        result = await page.evaluate(FIND_JS, pattern)
        if result is not None:
            _p(f"  Pivot cell found at index {result} (pattern={pattern!r})")
        else:
            _p(f"  Warning: pivot_cell_pattern {pattern!r} matched no cell — using 30s fallback")
        return result

    async def extract_output(
        self,
        page: Page,
        extractor,
        output_path: str | None,
    ) -> str | None:
        """
        Gather text from all frames, call extractor, optionally save to file.
        Must be called immediately after notebook goes Idle — Colab clears
        private outputs when the session ends.
        """
        if extractor is None:
            return None

        texts: list[str] = []
        for i, frame in enumerate(page.frames):
            try:
                text = await frame.inner_text("body")
                texts.append(text)
            except Exception as e:
                _p(f"  frame {i} error: {e}")

        result = extractor(texts)
        if result is None:
            _p("  extract_output: extractor returned None — page snippet:")
            combined = "\n".join(texts)
            _p(f"    {combined[:500]!r}")
            return None

        _p(f"  extract_output: {len(result)} chars")
        if output_path:
            import os
            os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(result)
            _p(f"  Saved → {output_path}")

        return result
