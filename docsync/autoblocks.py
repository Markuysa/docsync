"""Replace autoblocks (content between docsync:auto:<id>:start/end anchors) in Confluence storage format.

Content outside the anchors is never changed, a hand-edited block is reported as CONFLICT instead of being
overwritten, and the result is checked for balanced tags."""
from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass, field


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _anchor_re(block_id: str, edge: str) -> re.Pattern:
    # Confluence adds ac:macro-id and other attributes itself - allow any
    return re.compile(
        rf'<ac:structured-macro[^>]*ac:name="anchor"[^>]*>\s*<ac:parameter ac:name="">docsync:auto:{re.escape(block_id)}:{edge}'
        rf"</ac:parameter>\s*</ac:structured-macro>"
    )


def find_block(body: str, block_id: str) -> tuple[int, int] | None:
    s = _anchor_re(block_id, "start").search(body)
    if not s:
        return None
    e = _anchor_re(block_id, "end").search(body, s.start())
    if not e:
        raise ValueError(f"block {block_id}: start anchor without end anchor")
    return s.start(), e.end()


def normalize(x: str) -> str:
    """Strip what Confluence Cloud rewrites on its own before hashing: generated macro attributes
    (ac:macro-id, ac:schema-version, ac:local-id, data-layout), HTML entities (- -> &mdash;) and whitespace."""
    x = re.sub(r'\s(?:ac:macro-id|ac:schema-version|ac:local-id|ri:version-at-save|data-layout)="[^"]*"', "", x)
    x = html.unescape(x)
    x = re.sub(r">\s+<", "><", x)          # Confluence inserts whitespace between tags
    x = re.sub(r"\s+/>", "/>", x)           # <x /> vs <x/>
    return re.sub(r"\s+", " ", x).strip()


def outside_hash(body: str, ids: list[str]) -> str:
    out = body
    for bid in ids:
        pos = find_block(out, bid)
        if pos:
            out = out[: pos[0]] + f"[[docsync:{bid}]]" + out[pos[1]:]
    return sha256(normalize(out))


def assert_well_formed(xml: str) -> None:
    stack: list[str] = []
    text = re.sub(r"<!\[CDATA\[.*?\]\]>", "", xml, flags=re.S)
    for m in re.finditer(r"<(/?)([A-Za-z][\w:-]*)([^>]*?)(/?)>", text):
        closing, name, _, self_closing = m.groups()
        if self_closing or m.group(0).startswith("<!"):
            continue
        if name in ("br", "hr", "img", "col") and not closing:
            continue
        if closing:
            top = stack.pop() if stack else None
            if top != name:
                raise ValueError(f"malformed storage: </{name}> closes <{top}>")
        else:
            stack.append(name)
    if stack:
        raise ValueError(f"malformed storage: unclosed <{stack[-1]}>")


@dataclass
class PatchResult:
    body: str
    results: list[dict] = field(default_factory=list)
    changed: bool = False
    outside_hash: str = ""

    @property
    def conflicts(self) -> list[dict]:
        return [r for r in self.results if r["status"] in ("CONFLICT", "MISSING_ANCHORS")]


def patch_page(body: str, blocks: list[dict], last_hashes: dict[str, str] | None = None) -> PatchResult:
    """blocks: [{blockId, xhtml}], last_hashes: blockId -> hash of the last publication."""
    last_hashes = last_hashes or {}
    ids = [b["blockId"] for b in blocks]
    before = outside_hash(body, ids)
    nxt = body
    results: list[dict] = []
    for b in blocks:
        bid, xhtml = b["blockId"], b["xhtml"]
        pos = find_block(nxt, bid)
        if not pos:
            results.append({"blockId": bid, "status": "MISSING_ANCHORS"})
            continue
        current_hash = sha256(normalize(nxt[pos[0]: pos[1]]))
        expected = last_hashes.get(bid)
        if expected and expected != current_hash:
            results.append({"blockId": bid, "status": "CONFLICT", "currentHash": current_hash, "expected": expected})
            continue
        new_hash = sha256(normalize(xhtml))
        if new_hash == current_hash:
            results.append({"blockId": bid, "status": "UNCHANGED", "hashBefore": current_hash, "hashAfter": new_hash})
            continue
        nxt = nxt[: pos[0]] + xhtml + nxt[pos[1]:]
        results.append({"blockId": bid, "status": "PUBLISHED", "hashBefore": current_hash, "hashAfter": new_hash})
    after = outside_hash(nxt, ids)
    if after != before:
        raise RuntimeError("Invariant violated: content outside autoblocks changed")
    assert_well_formed(nxt)
    return PatchResult(body=nxt, results=results, changed=nxt != body, outside_hash=after)
