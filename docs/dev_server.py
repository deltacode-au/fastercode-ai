"""Local live-edit server for docs/docs.html.

Run with `python docs/dev_server.py` from the repo root. Serves the docs
fragment wrapped in a minimal HTML shell with a floating toolbar that lets
you click into prose and edit it in place, add new sections/subsections,
bold text or change its size, then save straight back to docs/docs.html on
disk - the sidebar nav and section numbering are rebuilt from the live DOM
on every save, so they never drift out of sync. Code blocks and
method-signature headers stay non-editable, since editing their visible
text alone would desync the copy button's data-code attribute and the
Pygments-highlighted spans underneath it - adding a real syntax-highlighted
code sample still goes through the usual script-based workflow instead.

The release banner (.update-banner) is editable too, despite sitting
outside <main>: its bullets and its version/date/days-since line are all
fair game, while the NEW badge and the "what's new" toggle stay locked so
the disclosure keeps working. Turning Edit on forces the banner open so
the bullets are reachable; Save strips that `open` attribute again, so the
published page still ships it collapsed.

Caution: this uses plain browser contenteditable, not a real rich-text
editor - selecting/deleting a range that spans across a heading or section
boundary can merge or drop content unexpectedly (a known contenteditable
rough edge). Prefer small, scoped edits within one heading/paragraph at a
time, and check `git diff docs/docs.html` after saving.

Every launch serves at the same fixed URL regardless of which file it's
editing, so responses are sent non-cacheable - otherwise a browser tab left
open from a previous session (e.g. one that had index.html loaded) can keep
showing that stale content on a later launch targeting docs.html. If a tab
from an earlier session is still open, close it or hard-refresh rather than
trusting whatever's already on screen.

Pass a filename to edit something else in docs/, e.g.
`python docs/dev_server.py index.html` for the landing page. That file is a
full HTML document rather than a bare fragment - do_GET/do_POST detect the
shape and serve/save it whole instead of wrapping or overwriting the head.
It also has no nav.toc, so the eyebrow-renumbering half of syncNav() no-ops
there (guarded explicitly, since throwing would break editing prose too);
the +Section button still works but adds docs-shaped markup, which will
look out of place on the landing page.

Binds 127.0.0.1 only - never exposed on the network.
"""

import http.server
import json
import re
import shutil
import sys
import threading
import webbrowser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_TARGET_NAME = sys.argv[1] if len(sys.argv) > 1 else "docs.html"
SITE_PATH = REPO_ROOT / "docs" / _TARGET_NAME
BACKUP_PATH = SITE_PATH.with_name(SITE_PATH.name + ".bak")
PORT = 8770

# A fragment (docs.html) is a bare <section>/<div> tree meant to be dropped
# into do_GET's own <html><head><body> shell. A full document (index.html)
# already has its own <!doctype>, <head> and <style> - wrapping it in a
# second shell would nest two <html> elements, and saving body.innerHTML
# over the whole file would silently discard everything outside <body>.
# Checked on a short prefix, not the whole text: a full document's shape is
# decided in its first few bytes, and grepping the entire file for "<html"
# has a real false-positive - docs.html has that exact string inside a code
# comment further down.
def _is_full_document(text):
    head = text.lstrip()[:200].lower()
    return head.startswith("<!doctype") or head.startswith("<html")

TAG_PAIRS = ["section", "div", "pre", "table"]

TOOLBAR_HTML = """
<div id="__devtoolbar" data-devtool="1" style="position:fixed; bottom:16px; right:16px; z-index:9999; background:#1c1c1c; color:#eee; border:1px solid #444; border-radius:8px; padding:10px 14px; font-family:sans-serif; font-size:13px; display:flex; gap:8px; align-items:center; box-shadow:0 4px 16px rgba(0,0,0,0.35); flex-wrap:wrap; max-width:90vw;">
  <button id="__toggle" style="cursor:pointer;">Edit: off</button>
  <span style="width:1px; height:20px; background:#444;"></span>
  <button id="__addSection" style="cursor:pointer;">+ Section</button>
  <button id="__addSubsection" style="cursor:pointer;">+ Subsection</button>
  <span style="width:1px; height:20px; background:#444;"></span>
  <button id="__moveUp" style="cursor:pointer;">Section &uarr;</button>
  <button id="__moveDown" style="cursor:pointer;">Section &darr;</button>
  <button id="__promote" style="cursor:pointer;">Promote</button>
  <button id="__demote" style="cursor:pointer;">Demote</button>
  <span style="width:1px; height:20px; background:#444;"></span>
  <button id="__bold" style="cursor:pointer; font-weight:bold;">B</button>
  <select id="__fontSize" style="cursor:pointer;">
    <option value="">Size...</option>
    <option value="0.85rem">Small</option>
    <option value="1rem">Normal</option>
    <option value="1.15rem">Medium</option>
    <option value="1.4rem">Large</option>
  </select>
  <span style="width:1px; height:20px; background:#444;"></span>
  <button id="__save" style="cursor:pointer;">Save</button>
  <span id="__status" style="min-width:90px; display:inline-block;"></span>
</div>
<script data-devtool="1">
(function () {
  var editing = false;
  var main = document.querySelector('main');
  // The release banner lives outside <main> (it's a sibling of .layout), so
  // it needs to be its own editable root or its copy is unreachable. Absent
  // on pages that don't carry a banner - everything below stays optional.
  var banner = document.querySelector('.update-banner');
  var bannerDetails = banner ? banner.querySelector('details') : null;
  var editRoots = banner ? [main, banner] : [main];
  var toggleBtn = document.getElementById('__toggle');
  var addSectionBtn = document.getElementById('__addSection');
  var addSubsectionBtn = document.getElementById('__addSubsection');
  var moveUpBtn = document.getElementById('__moveUp');
  var moveDownBtn = document.getElementById('__moveDown');
  var promoteBtn = document.getElementById('__promote');
  var demoteBtn = document.getElementById('__demote');
  var boldBtn = document.getElementById('__bold');
  var fontSizeSelect = document.getElementById('__fontSize');
  var saveBtn = document.getElementById('__save');
  var status = document.getElementById('__status');
  var activeSection = null;
  var activeSubsection = null;

  function protectNonProse() {
    main.querySelectorAll('.code-window, .method-head code').forEach(function (el) {
      el.setAttribute('contenteditable', 'false');
    });
    if (banner) {
      // The badge and the disclosure label are controls, not copy - editing
      // them buys nothing and breaks the open/close affordance.
      banner.querySelectorAll('.update-badge, .update-toggle').forEach(function (el) {
        el.setAttribute('contenteditable', 'false');
      });
    }
  }

  function setEditing(on) {
    editing = on;
    editRoots.forEach(function (root) { root.contentEditable = editing; });
    if (editing) {
      protectNonProse();
      // The bullets are the point of editing the banner, so surface them.
      // Save strips this attribute again - see the save handler.
      if (bannerDetails) bannerDetails.open = true;
    }
    toggleBtn.textContent = 'Edit: ' + (editing ? 'on' : 'off');
  }

  toggleBtn.addEventListener('click', function () { setEditing(!editing); });

  if (banner) {
    // While editing, clicking the summary would collapse the banner instead
    // of placing a caret - which makes the version/date line impossible to
    // retype. Suppress the toggle and let the click land as a cursor.
    banner.addEventListener('click', function (e) {
      if (editing && e.target.closest('summary')) e.preventDefault();
    });
  }

  main.addEventListener('click', function (e) {
    var sec = e.target.closest('section[id]');
    if (sec) activeSection = sec;
    var h3 = e.target.closest('h3');
    if (h3) activeSubsection = h3;
  });

  function escapeHtml(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  // Renumbers every top-level section's <span class="eyebrow"> and rebuilds
  // nav.toc from the live DOM, so the sidebar can never drift out of sync
  // with however many sections currently exist / their order / titles.
  function syncNav() {
    // Docs-only. The landing page has no <main> and no nav.toc, and its one
    // .eyebrow is the hero wordmark - renumbering here would overwrite it
    // with "01".
    var toc = document.querySelector('nav.toc');
    if (!main || !toc) return;
    var sections = main.querySelectorAll(':scope > section[id]');
    var links = Array.prototype.map.call(sections, function (sec, i) {
      var num = String(i + 1).padStart(2, '0');
      var h2 = sec.querySelector('h2');
      var eyebrow = h2.querySelector('.eyebrow');
      eyebrow.textContent = num;
      var label = Array.prototype.slice.call(h2.childNodes)
        .filter(function (n) { return n !== eyebrow; })
        .map(function (n) { return n.textContent; })
        .join('').trim();
      return '<a href="#' + sec.id + '"><span class="toc-num">' + num + '</span>' + escapeHtml(label) + '</a>';
    });
    toc.innerHTML = links.join('\\n');
  }

  function slugify(title) {
    var base = title.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, '') || 'section';
    var slug = base;
    var n = 2;
    while (document.getElementById(slug)) { slug = base + '-' + n; n += 1; }
    return slug;
  }

  function moveSection(dir) {
    var target = activeSection;
    if (!target) { window.alert('Click inside a section first.'); return; }
    var sibling = dir === 'up' ? target.previousElementSibling : target.nextElementSibling;
    if (!sibling || sibling.tagName !== 'SECTION') return;
    if (dir === 'up') main.insertBefore(target, sibling);
    else main.insertBefore(sibling, target);
    syncNav();
    target.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  function collectUntilNextH3(startEl) {
    var nodes = [];
    var node = startEl.nextElementSibling;
    while (node && node.tagName !== 'H3') { nodes.push(node); node = node.nextElementSibling; }
    return nodes;
  }

  function promoteSubsection() {
    var h3 = activeSubsection;
    if (!h3 || !main.contains(h3)) { window.alert('Click inside a subsection first.'); return; }
    var parentSection = h3.closest('section[id]');
    if (!parentSection) return;
    var contentNodes = collectUntilNextH3(h3);
    var titleText = h3.textContent;

    var eyebrow = document.createElement('span');
    eyebrow.className = 'eyebrow';
    eyebrow.textContent = '00';
    var h2 = document.createElement('h2');
    h2.appendChild(eyebrow);
    h2.appendChild(document.createTextNode(titleText));

    var section = document.createElement('section');
    section.id = slugify(titleText);
    section.appendChild(h2);
    contentNodes.forEach(function (n) { section.appendChild(n); });

    h3.remove();
    parentSection.after(section);
    activeSection = section;
    activeSubsection = null;
    syncNav();
    h2.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  function demoteSection() {
    var sec = activeSection;
    if (!sec) { window.alert('Click inside a section first.'); return; }
    var prev = sec.previousElementSibling;
    if (!prev || prev.tagName !== 'SECTION') {
      window.alert('No preceding section to demote into.');
      return;
    }
    var h2 = sec.querySelector(':scope > h2');
    var eyebrow = h2.querySelector('.eyebrow');
    var titleText = Array.prototype.slice.call(h2.childNodes)
      .filter(function (n) { return n !== eyebrow; })
      .map(function (n) { return n.textContent; })
      .join('').trim();

    var h3 = document.createElement('h3');
    h3.style.margin = '32px 0 8px';
    h3.style.fontSize = '1.05rem';
    h3.textContent = titleText;
    prev.appendChild(h3);

    Array.prototype.slice.call(sec.children)
      .filter(function (el) { return el !== h2; })
      .forEach(function (el) { prev.appendChild(el); });

    sec.remove();
    activeSection = prev;
    activeSubsection = null;
    syncNav();
    h3.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  moveUpBtn.addEventListener('click', function () { moveSection('up'); });
  moveDownBtn.addEventListener('click', function () { moveSection('down'); });
  promoteBtn.addEventListener('click', promoteSubsection);
  demoteBtn.addEventListener('click', demoteSection);

  addSectionBtn.addEventListener('click', function () {
    var title = window.prompt('New section title:');
    if (!title) return;
    if (!editing) setEditing(true);
    var slug = slugify(title);
    var section = document.createElement('section');
    section.id = slug;

    var eyebrow = document.createElement('span');
    eyebrow.className = 'eyebrow';
    eyebrow.textContent = '00';
    var h2 = document.createElement('h2');
    h2.appendChild(eyebrow);
    h2.appendChild(document.createTextNode(title));

    var p = document.createElement('p');
    p.textContent = 'New section \\u2014 start writing here.';
    var div = document.createElement('div');
    div.className = 'prose';
    div.appendChild(p);

    section.appendChild(h2);
    section.appendChild(div);
    main.appendChild(section);
    activeSection = section;
    syncNav();
    h2.scrollIntoView({ behavior: 'smooth', block: 'center' });
  });

  addSubsectionBtn.addEventListener('click', function () {
    var target = activeSection || main.querySelector(':scope > section[id]:last-of-type');
    if (!target) return;
    if (!editing) setEditing(true);
    var h3 = document.createElement('h3');
    h3.style.margin = '32px 0 8px';
    h3.style.fontSize = '1.05rem';
    h3.textContent = 'New subsection';
    var div = document.createElement('div');
    div.className = 'prose';
    div.innerHTML = '<p>New subsection content.</p>';
    target.appendChild(h3);
    target.appendChild(div);
    h3.scrollIntoView({ behavior: 'smooth', block: 'center' });
  });

  boldBtn.addEventListener('click', function () {
    document.execCommand('bold');
  });

  fontSizeSelect.addEventListener('change', function () {
    var value = fontSizeSelect.value;
    fontSizeSelect.value = '';
    if (!value) return;
    var sel = window.getSelection();
    if (!sel.rangeCount || sel.isCollapsed) return;
    // execCommand('fontSize') reuses the browser's own selection-splitting
    // logic (handles selections spanning multiple elements, which a
    // hand-rolled Range.surroundContents() call cannot) - it inserts legacy
    // <font size="7"> wrappers, which get converted to modern <span
    // style="font-size:..."> immediately below so nothing deprecated is
    // left in the saved HTML.
    document.execCommand('fontSize', false, '7');
    main.querySelectorAll('font[size="7"]').forEach(function (font) {
      var span = document.createElement('span');
      span.style.fontSize = value;
      while (font.firstChild) span.appendChild(font.firstChild);
      font.replaceWith(span);
    });
  });

  saveBtn.addEventListener('click', function () {
    syncNav();
    var clone = document.body.cloneNode(true);
    clone.querySelectorAll('[data-devtool]').forEach(function (el) { el.remove(); });
    // contenteditable is edit-session state, not content - main's own
    // "true" and protectNonProse()'s "false" markers must never survive
    // into the saved file (this previously leaked straight into
    // docs/docs.html, since Edit mode was left on when Save was clicked).
    clone.querySelectorAll('[contenteditable]').forEach(function (el) {
      el.removeAttribute('contenteditable');
    });
    // Same class of leak: setEditing() forces the banner disclosure open so
    // its bullets can be edited, and `open` is a real attribute that would
    // otherwise persist - shipping the banner permanently expanded to every
    // visitor.
    clone.querySelectorAll('.update-banner details[open]').forEach(function (el) {
      el.removeAttribute('open');
    });
    var html = clone.innerHTML;
    status.textContent = 'saving...';
    fetch('/save', {
      method: 'POST',
      headers: { 'Content-Type': 'text/html; charset=utf-8' },
      body: html
    }).then(function (r) {
      return r.json().then(function (data) { return { ok: r.ok, data: data }; });
    }).then(function (res) {
      if (res.ok) {
        status.textContent = 'saved - reloading';
        setTimeout(function () { location.reload(); }, 400);
      } else {
        status.textContent = 'error: ' + (res.data.error || 'unknown');
        setTimeout(function () { status.textContent = ''; }, 4000);
      }
    }).catch(function (err) {
      status.textContent = 'error: ' + err;
    });
  });
})();
</script>
"""


def check_balance(html_text):
    problems = []
    for tag in TAG_PAIRS:
        opens = len(re.findall(r"<" + tag + r"(?:\s|>)", html_text))
        closes = html_text.count(f"</{tag}>")
        if opens != closes:
            problems.append(f"<{tag}>: {opens} open vs {closes} close")
    return problems


# Fired once at the very top of <head>, before any of the document's own
# scripts - specifically before the landing page's IIFE that drives the
# hero's typing loop, which checks this flag and returns early. Without it,
# the animation would keep retyping the prompt out from under an edit.
FLAG_SCRIPT = "<script>window.__devToolEditing = true;</script>\n"


def _reconstruct_full_document(original_text, new_body_html):
    # The toolbar posts body.cloneNode(true).innerHTML - the children of
    # <body>, not <body> itself and nothing from <head>. So the file has to
    # be rebuilt around that: keep the ORIGINAL doctype/html/head/opening
    # <body> tag verbatim (the live DOM's <head> was never touched and was
    # never posted), splice in the edited body, and close it out.
    body_start = original_text.find("<body")
    if body_start == -1:
        raise ValueError("no <body> tag found in the original file")
    body_tag_end = original_text.find(">", body_start)
    if body_tag_end == -1:
        raise ValueError("malformed <body> tag in the original file")
    prefix = original_text[: body_tag_end + 1]
    return prefix + "\n" + new_body_html + "\n</body>\n</html>\n"


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path != "/":
            self.send_response(404)
            self.end_headers()
            return
        content = SITE_PATH.read_text(encoding="utf-8")
        if _is_full_document(content):
            # Serve the real document, not a fragment dropped into a shell -
            # a second <html><head><body> around an already-complete one
            # would nest two documents. The flag has to land inside <head>
            # so it executes before the page's own body scripts do.
            served = content.replace("<head>", "<head>\n" + FLAG_SCRIPT, 1)
            close = served.rfind("</body>")
            if close == -1:
                served += TOOLBAR_HTML
            else:
                served = served[:close] + TOOLBAR_HTML + "\n" + served[close:]
        else:
            served = (
                "<!doctype html>\n<html>\n<head>\n"
                '<meta charset="utf-8">\n'
                f"<title>docs/{_TARGET_NAME} (live edit)</title>\n"
                "</head>\n<body>\n" + content + "\n" + TOOLBAR_HTML + "\n</body>\n</html>\n"
            )
        self._respond(200, served.encode("utf-8"), "text/html; charset=utf-8")

    def do_POST(self):
        if self.path != "/save":
            self.send_response(404)
            self.end_headers()
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            print(f"[dev_server] POST /save: {length} bytes incoming")
            raw = self.rfile.read(length)
            new_body = raw.decode("utf-8")

            problems = check_balance(new_body)
            if problems:
                print(f"[dev_server] rejected: {problems}")
                self._json(400, {"error": "unbalanced tags: " + "; ".join(problems)})
                return

            # Read BEFORE overwriting: full-document mode needs the current
            # on-disk <head> to rebuild around, and the posted body is only
            # ever the fragment inside <body> either way.
            current = SITE_PATH.read_text(encoding="utf-8") if SITE_PATH.exists() else ""
            if SITE_PATH.exists():
                shutil.copyfile(SITE_PATH, BACKUP_PATH)

            if current and _is_full_document(current):
                new_content = _reconstruct_full_document(current, new_body)
            else:
                new_content = new_body

            SITE_PATH.write_text(new_content, encoding="utf-8")
            print(f"[dev_server] saved {len(new_content)} chars to {SITE_PATH}")
            self._json(200, {"ok": True})
        except Exception as exc:
            print(f"[dev_server] ERROR handling /save: {type(exc).__name__}: {exc}")
            try:
                self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
            except Exception:
                pass

    def _json(self, status, payload):
        self._respond(status, json.dumps(payload).encode("utf-8"), "application/json")

    def _respond(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # Every invocation serves at the same fixed URL (127.0.0.1:PORT/)
        # regardless of which file it's editing - without this, a browser
        # that cached an earlier session's response (e.g. index.html) can
        # keep showing it on a later launch targeting docs.html instead.
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        self.wfile.write(body)


def main():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}/"
    print(f"Serving {SITE_PATH} for live editing at {url}")
    print("Press Ctrl+C to stop.")
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
