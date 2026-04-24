"""
All Colab shadow-DOM JavaScript snippets.

Key design notes:
- STATUS_JS uses text-node walk (nodeType===3), NOT element.innerText.
  innerText traversal picks up source code containing "RAM", "L4", "Gemini" etc.
  Only "X.XX GB/Y.YY GB" unambiguously means a connected runtime.
- CLICK_TEXT_JS walks shadow DOM — standard querySelector won't find Colab buttons.
- CELL_EXEC_STATE_JS returns the executionState of the first cell matching a pattern.
  Use this to detect pivot_cell_pattern transition from dense→sparse monitoring.
"""

STATUS_JS = r"""() => {
    function walk(node, d=0) {
        if (d > 40) return null;
        if (node.nodeType === 3) {
            const t = node.textContent.trim();
            if (/\d+\.\d+ GB\/\d+/.test(t)) return t;
        }
        if (node.nodeType === 1 && node.shadowRoot) {
            const r = walk(node.shadowRoot, d+1); if (r) return r;
        }
        for (const c of (node.childNodes||[])) {
            const r = walk(c, d+1); if (r) return r;
        }
        return null;
    }
    return walk(document) || 'no-status';
}"""

CONNECT_JS = r"""() => {
    // Handles both initial connect ('Connect') and post-GPU-switch reconnect ('Reconnect')
    const CONNECT_TEXTS = new Set(['Connect', 'Reconnect',
                                   'Connect to a hosted runtime',
                                   'Connect to a new runtime']);
    function deepClick(node, d=0) {
        if (d > 40) return false;
        if (node.nodeType === 1) {
            if (node.id === 'connect') { node.click(); return true; }
            if ((node.tagName||'').toLowerCase() === 'button') {
                const t = (node.innerText||'').trim();
                if (CONNECT_TEXTS.has(t)) { node.click(); return true; }
            }
        }
        if (node.shadowRoot && deepClick(node.shadowRoot, d+1)) return true;
        for (const c of (node.children||[])) { if (deepClick(c, d+1)) return true; }
        return false;
    }
    return deepClick(document.documentElement);
}"""

TOO_MANY_SESSIONS_JS = r"""() => {
    function df(node, d=0) {
        if (d > 30) return false;
        if (node.nodeType === 3) {
            const t = node.textContent;
            if (t.includes('Terminate other sessions') ||
                t.includes('maximum number of sessions') ||
                t.includes('too many open sessions') ||
                t.includes('session limit') ||
                t.includes('too many active sessions') ||
                t.includes('Too many sessions')) return true;
        }
        if (node.shadowRoot && df(node.shadowRoot, d+1)) return true;
        for (const c of (node.childNodes||[])) { if (df(c, d+1)) return true; }
        return false;
    }
    return df(document);
}"""

GPU_ERR_JS = r"""() => {
    function df(node, d=0) {
        if (d > 30) return false;
        if (node.nodeType === 3 && (
            node.textContent.includes('Cannot connect to GPU') ||
            node.textContent.includes('GPU quota')
        )) return true;
        if (node.shadowRoot && df(node.shadowRoot, d+1)) return true;
        for (const c of (node.childNodes||[])) { if (df(c, d+1)) return true; }
        return false;
    }
    return df(document);
}"""

CLICK_TEXT_JS = r"""(text) => {
    function findButton(el) {
        // Walk up to find a button or element with role=button
        let cur = el;
        while (cur && cur !== document.body) {
            if (cur.tagName === 'BUTTON' || cur.getAttribute('role') === 'button') {
                return cur;
            }
            cur = cur.parentElement;
        }
        return null;
    }
    function df(node, d=0) {
        if (d > 40) return false;
        if (node.nodeType === 3 && node.textContent.trim() === text) {
            const p = node.parentElement;
            if (p) {
                const btn = findButton(p);
                if (btn) { btn.click(); return true; }
                if (p) { p.click(); return true; }
            }
        }
        if (node.shadowRoot && df(node.shadowRoot, d+1)) return true;
        for (const c of (node.childNodes||[])) { if (df(c, d+1)) return true; }
        return false;
    }
    return df(document);
}"""

DRIVE_JS = r"""() => {
    // Check if "Connect to Google Drive" button is visible
    function findText(text, node, d=0) {
        if (d > 60) return false;
        if (node.nodeType === 3 && node.textContent.trim() === text) {
            const p = node.parentElement;
            if (p) {
                const r = p.getBoundingClientRect();
                if (r.width > 0 && r.height > 0 && r.top > 60) return true;
            }
        }
        if (node.shadowRoot && findText(text, node.shadowRoot, d+1)) return true;
        for (const c of (node.childNodes||[])) {
            if (findText(text, c, d+1)) return true;
        }
        return false;
    }
    return findText("Connect to Google Drive", document);
}"""

TAIL_JS = r"""() => {
    try {
        const blocks = [];
        function walk(root, d=0) {
            if (d > 20) return;
            const outputs = root.querySelectorAll(
                'colab-cell-output, .output_area, [id*="output"], [class*="cell_output"], ' +
                'div.stream, div.output_text, [class*="output_text"], [class*="output-id"]'
            );
            for (const el of outputs) {
                const t = (el.innerText || '').trim();
                if (t.length > 5) blocks.push(t);
            }
            for (const el of root.querySelectorAll('*')) {
                if (el.shadowRoot) walk(el.shadowRoot, d+1);
            }
        }
        walk(document);
        const allLines = blocks.join('\n').split('\n').filter(l => l.trim());
        const kw = ['Error', 'Traceback', 'RuntimeError', 'Exception', 'Failed', 'FAILED'];
        const rel = allLines.filter(l => kw.some(k => l.includes(k)));
        return (rel.length ? rel : allLines).slice(-15);
    } catch(e) { return [`error: ${e}`]; }
}"""

OUTPUT_FRAME_URLS_JS = r"""() => {
    // Collect src URLs of all output iframes (inside shadow DOM).
    // Playwright page.frames doesn't traverse shadow roots, but we can find
    // the URLs here and match them to frames via page.frame(url=...).
    const urls = [];
    function walk(root, d=0) {
        if (d > 30) return;
        for (const el of root.querySelectorAll('iframe[src*="outputframe"]')) {
            if (el.src) urls.push(el.src);
        }
        for (const el of root.querySelectorAll('*')) {
            if (el.shadowRoot) walk(el.shadowRoot, d+1);
        }
    }
    walk(document);
    return urls;
}"""

PATCH_CELL_JS = r"""([pattern, replace, flags]) => {
    try {
        const nm = colab.global.notebookModel;
        const re = new RegExp(pattern, flags);
        for (let i = 0; i < nm.cells.length; i++) {
            const tm = nm.cells[i]?.textModel;
            if (!tm) continue;
            const val = tm.getValue();
            if (!re.test(val)) continue;
            tm.setValue(val.replace(re, replace));
            return {ok: true, cellIdx: i};
        }
        return {error: 'no match', pattern};
    } catch(e) { return {error: e.toString()}; }
}"""

READ_CELLS_JS = r"""() => {
    try {
        const nm = colab.global.notebookModel;
        return nm.cells.map((c, i) => ({
            i,
            preview: (c.textModel?.getValue() || '').slice(0, 80),
            len: (c.textModel?.getValue() || '').length
        }));
    } catch(e) { return {error: e.toString()}; }
}"""

CELL_ERROR_JS = r"""() => {
    try {
        const nm = colab.global.notebookModel;
        if (!nm || !nm.cells) return {error: false, warning: 'no notebookModel'};

        for (let i = 0; i < nm.cells.length; i++) {
            const cell = nm.cells[i];
            if (!cell) continue;

            const state = (cell.executionState || '').toLowerCase();
            if (state === 'error' || state === 'failed') {
                const preview = (cell.textModel?.getValue() || '').slice(0, 80);
                return {error: true, cellIdx: i, state, preview, source: 'executionState'};
            }

            // Fallback: in private outputs mode, executionState may not update.
            // Check cell output for error markers.
            if (cell.outputs && Array.isArray(cell.outputs)) {
                for (const output of cell.outputs) {
                    const text = output.data?.['text/plain'] ||
                                 output.data?.['application/vnd.google.colaboratory.output-result'] ||
                                 output.data?.['text/html'] || '';
                    if (typeof text === 'string' &&
                        (text.includes('Traceback') || text.includes('Error') || text.includes('error'))) {
                        const preview = (cell.textModel?.getValue() || '').slice(0, 80);
                        return {error: true, cellIdx: i, state: 'error-from-output', preview, source: 'output-data'};
                    }
                }
            }
        }
        return {error: false};
    } catch(e) { return {error: false, warning: 'exception: ' + e.toString()}; }
}"""

CLICK_RUNTIME_MENUBAR_JS = r"""() => {
    // Find the "Runtime" menu item in the top navigation bar (top < 60px) and return
    // its bounding rect. Caller must then do page.mouse.click(left+20, top+10) to
    // properly trigger the dropdown (JS element.click() won't render menu items).
    const hits = [];
    function df(node, d=0) {
        if (d > 40) return;
        if (node.nodeType === 3 && node.textContent.trim() === 'Runtime') {
            const p = node.parentElement;
            if (p) {
                const r = p.getBoundingClientRect();
                hits.push({top: r.top, left: r.left, w: r.width, h: r.height});
            }
        }
        if (node.shadowRoot) df(node.shadowRoot, d+1);
        for (const c of (node.childNodes||[])) df(c, d+1);
    }
    df(document);
    const bar = hits.filter(h => h.top >= 0 && h.top < 60 && h.w > 0);
    const t = bar[0] || hits.find(h => h.w > 0);
    return t ? {ok: true, top: t.top, left: t.left} : {ok: false, count: hits.length};
}"""

FIND_VISIBLE_TEXT_JS = r"""(text) => {
    // Return {x, y} for a visible text node's parent (bounding rect > 0, top > 60).
    // Used to click dropdown menu items and dialog buttons by coordinate.
    function df(node, d=0) {
        if (d > 60) return null;
        if (node.nodeType === 3 && node.textContent.trim() === text) {
            const p = node.parentElement;
            if (p) {
                const r = p.getBoundingClientRect();
                if (r.width > 0 && r.height > 0 && r.top > 60)
                    return {x: r.left + r.width / 2, y: r.top + r.height / 2};
            }
        }
        if (node.shadowRoot) { const r = df(node.shadowRoot, d+1); if (r) return r; }
        for (const c of (node.childNodes||[])) { const r = df(c, d+1); if (r) return r; }
        return null;
    }
    return df(document);
}"""

GENERIC_DIALOG_BTN_JS = r"""() => {
    // Find a visible button with safe-to-click text inside any dialog/modal.
    // Excludes nav-bar items (top < 60px). Returns {text, x, y} or null.
    const SAFE = ['OK', 'Got it', 'Dismiss', 'Continue', 'Reconnect',
                  'Run anyway', 'Terminate other sessions', 'close'];
    function df(node, d=0) {
        if (d > 40) return null;
        if (node.nodeType === 3) {
            const t = node.textContent.trim();
            if (SAFE.includes(t)) {
                let el = node.parentElement;
                while (el && el.tagName !== 'BODY') {
                    const tag = (el.tagName||'').toLowerCase();
                    const role = el.getAttribute ? (el.getAttribute('role')||'') : '';
                    if (tag === 'button' || role === 'button') {
                        const r = el.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0 && r.top > 60)
                            return {text: t, x: r.left + r.width/2, y: r.top + r.height/2};
                        break;
                    }
                    el = el.parentElement;
                }
            }
        }
        if (node.shadowRoot) { const r = df(node.shadowRoot, d+1); if (r) return r; }
        for (const c of (node.childNodes||[])) { const r = df(c, d+1); if (r) return r; }
        return null;
    }
    return df(document);
}"""

CELL_EXEC_STATE_JS = r"""(pattern) => {
    try {
        const nm = colab.global.notebookModel;
        const re = new RegExp(pattern, 'm');
        for (let i = 0; i < nm.cells.length; i++) {
            const cell = nm.cells[i];
            if (!cell?.textModel) continue;
            if (re.test(cell.textModel.getValue())) {
                return cell.executionState || 'unknown';
            }
        }
        return null;
    } catch(e) { return {error: e.toString()}; }
}"""

# Detects Colab's "Are you still there?" activity-check dialog and returns
# the coords of its primary-action button (the one that resumes the session).
# The button lives in the mwc-dialog host's LIGHT DOM (slot="primaryAction"),
# while the title lives in the host's shadow root — so we must walk both.
# Falls back to clicking any visible button inside a dialog that contains the title text.
STILL_THERE_JS = r"""() => {
    function getCoords(el) {
        const r = el.getBoundingClientRect();
        if (r.width > 0 && r.height > 0 && r.top > 0)
            return {x: r.left + r.width / 2, y: r.top + r.height / 2};
        return null;
    }
    function walk(node, d) {
        if (d > 40) return null;
        if (node.shadowRoot) {
            // Check if this shadow root contains the "Are you still there?" title
            const title = node.shadowRoot.querySelector('#title');
            if (title && title.textContent.includes('Are you still there')) {
                // Path 1: slot="primaryAction" in host's light DOM
                const slotBtn = node.querySelector('[slot="primaryAction"]');
                if (slotBtn) {
                    const c = getCoords(slotBtn);
                    if (c) return c;
                }
                // Path 2: any button/[role=button] inside the shadow root
                for (const el of node.shadowRoot.querySelectorAll('button,[role="button"]')) {
                    const c = getCoords(el);
                    if (c) return c;
                }
            }
            const sub = walk(node.shadowRoot, d + 1);
            if (sub) return sub;
        }
        for (const c of (node.childNodes || [])) {
            const sub = walk(c, d + 1);
            if (sub) return sub;
        }
        return null;
    }
    return walk(document, 0);
}"""
