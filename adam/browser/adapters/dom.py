"""适配器固定 DOM 程序；不接受调用者提供的 JavaScript。"""

# 观察期间不写属性或点控件；Map 保存原始节点，避免同名替代控件复用旧引用。
SNAPSHOT_SCRIPT = r"""({key, mode, expected, maxChars, maxElements}) => {
    const randomId = () => Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, '0')).join('');
    const clip = (value, limit) => {
        const text = value.toWellFormed().slice(0, limit);
        const last = text.charCodeAt(text.length - 1);
        return last >= 0xd800 && last <= 0xdbff ? text.slice(0, -1) : text;
    };
    let state = window[key];
    if (!state) {
        state = {documentId: randomId(), revision: 0, refs: new Map()};
        const observer = new MutationObserver(() => state.revision++);
        observer.observe(document, {subtree: true, childList: true, attributes: true, characterData: true});
        for (const event of ['input', 'change']) document.addEventListener(event, () => state.revision++, true);
        window[key] = state;
    }
    const visible = el => {
        const style = getComputedStyle(el);
        return el.isConnected && !el.closest('[hidden],[inert],[aria-hidden="true"]') &&
            style.visibility !== 'hidden' && style.visibility !== 'collapse' &&
            style.display !== 'none' && el.getClientRects().length > 0;
    };
    const enabled = el => !el.matches(':disabled') && !el.closest('[aria-disabled="true"]');
    const signature = el => JSON.stringify([
        location.href, el.outerHTML, el.value, el.checked, el.selectedIndex,
        visible(el), enabled(el)
    ]);
    if (mode === 'validate' || mode === 'handle') {
        const item = state.refs.get(expected.elementId);
        const valid = state.documentId === expected.documentId && state.revision === expected.revision &&
            !!item && visible(item.element) && enabled(item.element) &&
            item.signature === signature(item.element) && state.pageText === (document.body?.innerText || '');
        return mode === 'handle' ? (valid ? item.element : null) : valid;
    }
    const elements = [];
    state.refs = new Map();
    let elementsTruncated = false;
    const candidates = document.querySelectorAll(
        'a[href],button,input:not([type="hidden"]),select,textarea,[role="button"],[role="link"],[contenteditable="true"]'
    );
    for (const el of candidates) {
        if (!visible(el) || !enabled(el)) continue;
        if (elements.length === maxElements) { elementsTruncated = true; break; }
        const id = randomId();
        state.refs.set(id, {element: el, signature: signature(el)});
        const labels = Array.from(el.labels || []).map(label => label.innerText).join(' ');
        const name = (el.getAttribute('aria-label') || labels || el.innerText || el.getAttribute('placeholder') || '').trim();
        elements.push([id, el.tagName.toLowerCase(), clip(el.getAttribute('role') || '', 100), clip(name, 300), clip(el.getAttribute('type') || '', 100)]);
    }
    const text = document.body?.innerText || '';
    state.pageText = text;
    return {documentId: state.documentId, revision: state.revision, url: location.href,
        title: clip(document.title, 300), text: clip(text, maxChars), elements,
        textTruncated: text.length > maxChars, elementsTruncated};
}"""
