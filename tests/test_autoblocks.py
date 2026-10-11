import pytest

from docsync.autoblocks import patch_page
from docsync.generate import autoblock, START, END


def anchor(bid, edge, extra=""):
    return f'<ac:structured-macro ac:name="anchor"{extra}><ac:parameter ac:name="">docsync:auto:{bid}:{edge}</ac:parameter></ac:structured-macro>'


MANUAL = "<h2>Назначение</h2><p>Ручной текст аналитика.</p>"
PAGE = MANUAL + anchor("t1", "start", ' ac:macro-id="abc"') + "<p>old</p>" + anchor("t1", "end", ' ac:macro-id="def"') + "<p>Хвост</p>"


def block(bid, inner):
    return START.format(id=bid) + f"<p>{inner}</p>" + END.format(id=bid)


def test_replace_keeps_manual_text():
    r = patch_page(PAGE, [{"blockId": "t1", "xhtml": block("t1", "new")}])
    assert r.results[0]["status"] == "PUBLISHED" and r.changed
    assert r.body.startswith(MANUAL) and r.body.endswith("<p>Хвост</p>") and "<p>new</p>" in r.body


def test_idempotent():
    r1 = patch_page(PAGE, [{"blockId": "t1", "xhtml": block("t1", "new")}])
    r2 = patch_page(r1.body, [{"blockId": "t1", "xhtml": block("t1", "new")}])
    assert r2.results[0]["status"] == "UNCHANGED" and not r2.changed


def test_manual_edit_inside_block_is_conflict():
    r1 = patch_page(PAGE, [{"blockId": "t1", "xhtml": block("t1", "new")}])
    edited = r1.body.replace("<p>new</p>", "<p>new, поправлено руками</p>")
    r3 = patch_page(edited, [{"blockId": "t1", "xhtml": block("t1", "newer")}], {"t1": r1.results[0]["hashAfter"]})
    assert r3.results[0]["status"] == "CONFLICT" and "поправлено руками" in r3.body


def test_missing_anchors():
    r = patch_page(MANUAL, [{"blockId": "x", "xhtml": block("x", "y")}])
    assert r.results[0]["status"] == "MISSING_ANCHORS"


def test_malformed_block_rejected():
    with pytest.raises(ValueError):
        patch_page(PAGE, [{"blockId": "t1", "xhtml": START.format(id="t1") + "<p>broken" + END.format(id="t1")}])


def test_autoblock_wrapper_is_well_formed_with_cdata():
    xhtml = autoblock("s", '<ac:structured-macro ac:name="plantuml"><ac:plain-text-body><![CDATA[@startuml\na -> b\n@enduml]]></ac:plain-text-body></ac:structured-macro>', "meta")
    page = "<p>x</p>" + xhtml + "<p>y</p>"
    r = patch_page(page, [{"blockId": "s", "xhtml": xhtml}])
    assert r.results[0]["status"] == "UNCHANGED"


def test_normalize_ignores_confluence_rewrites():
    from docsync.autoblocks import normalize
    sent = '<ac:structured-macro ac:name="panel"><ac:parameter ac:name="title">A — B · C</ac:parameter></ac:structured-macro>'
    stored = '<ac:structured-macro ac:name="panel" ac:schema-version="1" ac:macro-id="x1"> <ac:parameter ac:name="title">A &mdash; B &middot; C</ac:parameter> </ac:structured-macro>'
    assert normalize(sent) == normalize(stored)


def test_normalize_ignores_attachment_version():
    from docsync.autoblocks import normalize
    assert normalize('<ri:attachment ri:filename="a.svg"/>') == normalize('<ri:attachment ri:filename="a.svg" ri:version-at-save="5" />')
