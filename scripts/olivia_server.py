#!/usr/bin/env python3
"""Olivia Mode interview server.

Serves a branching decision tree of questions (one at a time) over a tiny local
web UI, and lets the agent that spawned it react in real time by watching stdout
and driving changes through HTTP control endpoints.

The server is the SOLE writer of the session JSON file while it runs. The agent
must never edit the file directly during a live session -- it mutates the tree
via /api/add, /api/resolve and /api/update instead. This is what keeps the two
writers from racing on the file.

Stdlib only. No third-party dependencies.

Sessions live in a central directory (default ~/.olivia-mode/sessions, overridable
via OLIVIA_MODE_HOME) so they can be found and resumed from a later Claude/Codex
session. Each session records the working directory it belongs to -- both encoded
into its filename and stored in the JSON -- so `sessions` discovery can match a
repo to its interview.

CLI:
    python3 olivia_server.py serve --file PATH [--cwd DIR] [--port 0] [--title "..."]
    python3 olivia_server.py sessions [--cwd DIR] [--all]

Every notable event prints a single line to stdout, flushed immediately:

    OLIVIA_EVENT {"type": "...", ...}
"""

import argparse
import glob
import html
import json
import os
import re
import sys
import tempfile
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse

VALID_STATUS = {"pending", "answered", "resolved", "skipped"}


# --------------------------------------------------------------------------- #
# Central session storage -- keyed by the working directory a session belongs to
# --------------------------------------------------------------------------- #

def _olivia_home():
    """Root dir for Olivia state. OLIVIA_MODE_HOME overrides ~/.olivia-mode."""
    return os.environ.get("OLIVIA_MODE_HOME") or os.path.expanduser("~/.olivia-mode")


def _sessions_dir():
    """The known directory holding every session file (created on demand)."""
    d = os.path.join(_olivia_home(), "sessions")
    os.makedirs(d, exist_ok=True)
    return d


def _slug(cwd):
    """Encode an absolute path into a filename stem (Claude Code's scheme).

    Non-alphanumeric runs collapse to a single '-', e.g.
    /Users/bhill/repos/olivia-mode -> -Users-bhill-repos-olivia-mode
    """
    cwd = os.path.abspath(cwd)
    return re.sub(r"[^A-Za-z0-9]+", "-", cwd)


def _new_path(cwd):
    """Lowest free <slug>.json, then <slug>-2.json, ... for this cwd."""
    directory = _sessions_dir()
    stem = _slug(cwd)
    candidate = os.path.join(directory, stem + ".json")
    if not os.path.exists(candidate):
        return candidate
    n = 2
    while True:
        candidate = os.path.join(directory, "%s-%d.json" % (stem, n))
        if not os.path.exists(candidate):
            return candidate
        n += 1


# --------------------------------------------------------------------------- #
# Event protocol
# --------------------------------------------------------------------------- #

def emit(event_type, **fields):
    """Print a single JSON event line to stdout so the agent can react."""
    payload = {"type": event_type, "ts": _now_iso()}
    payload.update(fields)
    sys.stdout.write("OLIVIA_EVENT " + json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Session store -- owns the tree and the file. All access goes through the lock.
# --------------------------------------------------------------------------- #

class SessionStore:
    def __init__(self, path, default_title=None, cwd=None):
        self.path = os.path.abspath(path)
        self.cwd = os.path.abspath(cwd) if cwd else None
        self.lock = threading.RLock()
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as fh:
                self.data = json.load(fh)
            self._normalize()
        else:
            self.data = {
                "title": default_title or "Untitled plan",
                "created": _now_iso(),
                "cwd": self.cwd,
                "questions": [],
            }
            self._persist()

    # -- internal helpers (assume lock held) -------------------------------- #

    def _normalize(self):
        """Fill in defaults so partially-authored files still load cleanly."""
        self.data.setdefault("title", "Untitled plan")
        self.data.setdefault("created", _now_iso())
        if not self.data.get("cwd"):
            self.data["cwd"] = self.cwd
        self.data.setdefault("updated", self.data["created"])
        self.data.setdefault("questions", [])
        for q in self.data["questions"]:
            q.setdefault("parentId", None)
            q.setdefault("parentAnswer", None)
            q.setdefault("recommendations", [])
            q.setdefault("status", "pending")
            q.setdefault("answer", None)
            for i, rec in enumerate(q["recommendations"]):
                rec.setdefault("id", "r%d" % (i + 1))
                rec.setdefault("label", "")
                rec.setdefault("rationale", "")

    def _persist(self):
        """Atomically write the tree to disk (temp file + os.replace)."""
        self.data["updated"] = _now_iso()
        directory = os.path.dirname(self.path) or "."
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".olivia-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            os.replace(tmp, self.path)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def _by_id(self, qid):
        for q in self.data["questions"]:
            if q["id"] == qid:
                return q
        return None

    def _next_id(self):
        n = 1
        existing = {q["id"] for q in self.data["questions"]}
        while ("q%d" % n) in existing:
            n += 1
        return "q%d" % n

    def _children(self, qid, chosen=None):
        """Children of qid. If chosen is given, only those activated by it."""
        out = []
        for q in self.data["questions"]:
            if q["parentId"] != qid:
                continue
            if chosen is None:
                out.append(q)
            elif q["parentAnswer"] in (chosen, "*", None):
                out.append(q)
        return out

    def _ordered(self):
        """Canonical DFS order over reachable questions.

        Roots in array order; descend into a question's activated children only
        once it has been answered (so branches of unanswered questions and of
        not-chosen answers stay hidden).
        """
        order = []
        seen = set()

        def visit(q):
            if q["id"] in seen:
                return
            seen.add(q["id"])
            order.append(q)
            if q["status"] == "answered" and q.get("answer"):
                chosen = q["answer"].get("choiceId")
                for child in self._children(q["id"], chosen):
                    visit(child)

        for q in self.data["questions"]:
            if q["parentId"] is None:
                visit(q)
        return order

    # -- public API (each takes the lock) ----------------------------------- #

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.data))

    def get(self, qid):
        with self.lock:
            q = self._by_id(qid)
            return json.loads(json.dumps(q)) if q else None

    def parent_of(self, qid):
        with self.lock:
            q = self._by_id(qid)
            if not q or q["parentId"] is None:
                return None
            p = self._by_id(q["parentId"])
            return json.loads(json.dumps(p)) if p else None

    def roots(self):
        with self.lock:
            return [json.loads(json.dumps(q))
                    for q in self.data["questions"] if q["parentId"] is None]

    def ordered(self):
        """Reachable questions in canonical DFS order (deep-copied)."""
        with self.lock:
            return [json.loads(json.dumps(q)) for q in self._ordered()]

    def next_pending(self, after=None):
        """Return the next pending question id in DFS order.

        If `after` is given, prefer the first pending question that comes after
        it; otherwise the first pending anywhere. Returns None when complete.
        """
        with self.lock:
            order = self._ordered()
            ids = [q["id"] for q in order]
            start = 0
            if after in ids:
                start = ids.index(after) + 1
            for q in order[start:]:
                if q["status"] == "pending":
                    return q["id"]
            for q in order:
                if q["status"] == "pending":
                    return q["id"]
            return None

    def answer(self, qid, choice_id, custom_text="", note=""):
        with self.lock:
            q = self._by_id(qid)
            if q is None:
                return None, "unknown question"
            rec_ids = {r["id"] for r in q["recommendations"]}
            if choice_id != "custom" and choice_id not in rec_ids:
                return None, "unknown choice"
            q["answer"] = {
                "choiceId": choice_id,
                "customText": custom_text or "",
                "note": note or "",
            }
            q["status"] = "answered"
            self._persist()
            nxt = self.next_pending(after=qid)
        emit("answer", qid=qid, choiceId=choice_id,
             custom=custom_text or "", note=note or "", next=nxt)
        return nxt, None

    def add(self, items):
        """Add one or more questions. Returns the list of created ids."""
        created = []
        with self.lock:
            for item in items:
                qid = item.get("id") or self._next_id()
                if self._by_id(qid) is not None:
                    qid = self._next_id()
                recs = []
                for i, rec in enumerate(item.get("recommendations", [])):
                    recs.append({
                        "id": rec.get("id") or ("r%d" % (i + 1)),
                        "label": rec.get("label", ""),
                        "rationale": rec.get("rationale", ""),
                    })
                self.data["questions"].append({
                    "id": qid,
                    "text": item.get("text", ""),
                    "parentId": item.get("parentId"),
                    "parentAnswer": item.get("parentAnswer"),
                    "recommendations": recs,
                    "status": "pending",
                    "answer": None,
                })
                created.append(qid)
            self._persist()
        emit("added", ids=created)
        return created

    def resolve(self, qid, reason=""):
        with self.lock:
            q = self._by_id(qid)
            if q is None:
                return False
            q["status"] = "resolved"
            if reason:
                q["resolvedReason"] = reason
            self._persist()
        emit("resolve", qid=qid, reason=reason)
        return True

    def update(self, qid, text=None, recommendations=None):
        with self.lock:
            q = self._by_id(qid)
            if q is None:
                return False
            if text is not None:
                q["text"] = text
            if recommendations is not None:
                recs = []
                for i, rec in enumerate(recommendations):
                    recs.append({
                        "id": rec.get("id") or ("r%d" % (i + 1)),
                        "label": rec.get("label", ""),
                        "rationale": rec.get("rationale", ""),
                    })
                q["recommendations"] = recs
            self._persist()
        emit("updated", qid=qid)
        return True


# --------------------------------------------------------------------------- #
# HTML rendering -- deliberately minimal, no CSS framework.
# --------------------------------------------------------------------------- #

# status -> (human label for the transcript, css state class)
STATUS_TAG = {
    "pending": ("awaiting", "is-pending"),
    "answered": ("on record", "is-done"),
    "resolved": ("resolved", "is-done"),
    "skipped": ("skipped", "is-skipped"),
}

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Hanken+Grotesk:wght@400;500;600;700&family=Newsreader:ital,opsz,wght@0,6..72,400;0,6..72,500;1,6..72,400;1,6..72,500&family=Space+Mono:wght@400;700&display=swap" rel="stylesheet">
<style>
:root{{
  --paper:#E7E8E1; --paper-2:#DFE1D8; --ink:#16211D; --muted:#5A6560;
  --line:#C7CBBF; --olivia:#2E6B5B; --olivia-soft:#E1EAE3;
  --mark:#A9762E; --mark-soft:#F0E5D2;
  --serif:"Newsreader",Georgia,serif;
  --sans:"Hanken Grotesk",system-ui,-apple-system,sans-serif;
  --mono:"Space Mono",ui-monospace,Menlo,monospace;
}}
*{{box-sizing:border-box}}
html{{-webkit-text-size-adjust:100%}}
body{{
  margin:0; background:var(--paper); color:var(--ink);
  font-family:var(--sans); font-size:17px; line-height:1.55;
  -webkit-font-smoothing:antialiased;
}}
::selection{{background:var(--olivia); color:var(--paper)}}
.wrap{{max-width:44rem; margin:0 auto; padding:3.2rem 1.4rem 4rem}}
a{{color:inherit}}
:focus-visible{{outline:2px solid var(--mark); outline-offset:3px; border-radius:2px}}

.eyebrow{{
  font-family:var(--mono); font-size:.72rem; letter-spacing:.18em;
  text-transform:uppercase; color:var(--olivia); margin:0;
}}
.mono{{font-family:var(--mono); font-size:.74rem; letter-spacing:.04em; color:var(--muted)}}

/* --- masthead --- */
.mast{{margin-bottom:2.4rem}}
.mast__title{{
  font-family:var(--serif); font-weight:500; letter-spacing:-.01em;
  font-size:clamp(2rem,5.5vw,3.1rem); line-height:1.08; margin:.5rem 0 0;
}}
.progress{{display:flex; align-items:center; gap:.9rem; margin-top:1.5rem}}
.progress__track{{flex:1; height:2px; background:var(--line); position:relative}}
.progress__fill{{position:absolute; inset:0 auto 0 0; background:var(--olivia)}}
.progress__count{{font-family:var(--mono); font-size:.74rem; color:var(--muted); white-space:nowrap}}

.lead{{font-family:var(--serif); font-size:1.15rem; color:var(--muted); margin:1.4rem 0 0; font-style:italic}}

/* --- buttons --- */
.btn{{
  display:inline-flex; align-items:center; gap:.5rem; cursor:pointer;
  font-family:var(--sans); font-weight:600; font-size:.95rem;
  border:1px solid var(--ink); border-radius:0; padding:.7rem 1.2rem;
  background:var(--paper); color:var(--ink); text-decoration:none;
  transition:transform .12s ease, background .15s ease, color .15s ease;
}}
.btn:hover{{transform:translateY(-1px)}}
.btn--go{{background:var(--mark); border-color:var(--mark); color:#fff}}
.btn--go:hover{{background:#966623; border-color:#966623}}
.btn--ghost{{border-color:var(--line); color:var(--muted); font-weight:500; font-size:.85rem; padding:.55rem 1rem}}
.btn--ghost:hover{{border-color:var(--ink); color:var(--ink)}}
.cta-row{{margin:2rem 0 2.6rem}}

/* --- the record (interview thread) --- */
.record{{
  list-style:none; margin:0; padding:0; position:relative;
}}
.record::before{{
  content:""; position:absolute; left:6px; top:.4rem; bottom:.4rem;
  width:2px; background:var(--line);
}}
.node{{position:relative; padding:.55rem 0 .55rem 2rem; border-bottom:1px solid transparent}}
.node__dot{{
  position:absolute; left:0; top:.9rem; width:14px; height:14px; border-radius:50%;
  background:var(--paper); border:2px solid var(--line); z-index:1;
}}
.node.is-done .node__dot{{background:var(--olivia); border-color:var(--olivia)}}
.node.is-pending .node__dot{{border-color:var(--muted)}}
.node.is-skipped .node__dot{{border-style:dotted}}
.node--live .node__dot{{background:var(--mark); border-color:var(--mark); box-shadow:0 0 0 4px var(--mark-soft)}}
.node__link{{
  font-family:var(--serif); font-size:1.15rem; line-height:1.3; text-decoration:none;
  display:inline; color:var(--ink); text-decoration:.5px underline transparent;
  transition:text-decoration-color .15s ease;
}}
.node__link:hover{{text-decoration-color:var(--olivia)}}
.node.is-done .node__link{{color:var(--muted)}}
.node__id{{font-family:var(--mono); font-size:.7rem; color:var(--muted); margin-right:.55rem}}
.node__branch{{color:var(--olivia); margin-right:.3rem; font-family:var(--sans)}}
.node__tag{{
  font-family:var(--mono); font-size:.66rem; letter-spacing:.1em; text-transform:uppercase;
  color:var(--muted); margin-left:.6rem; white-space:nowrap;
}}
.node--live .node__tag{{color:var(--mark)}}
.node__said{{
  font-family:var(--serif); font-style:italic; color:var(--muted);
  font-size:1rem; margin:.15rem 0 0; padding-left:.1rem;
}}
.node__said b{{font-weight:500; font-style:normal; color:var(--ink)}}

.section-label{{
  font-family:var(--mono); font-size:.72rem; letter-spacing:.16em; text-transform:uppercase;
  color:var(--muted); border-bottom:1px solid var(--line); padding-bottom:.5rem; margin:0 0 .4rem;
}}

/* --- foot --- */
.foot{{margin-top:3rem; padding-top:1.4rem; border-top:1px solid var(--line)}}

/* --- question page --- */
.topbar{{display:flex; justify-content:space-between; align-items:baseline; gap:1rem; margin-bottom:2.4rem}}
.back{{font-family:var(--mono); font-size:.74rem; text-decoration:none; color:var(--muted)}}
.back:hover{{color:var(--ink)}}
.earlier{{
  border-left:2px solid var(--olivia); background:var(--olivia-soft);
  padding:.7rem .95rem; margin:0 0 2rem;
}}
.earlier__q{{font-family:var(--serif); font-size:1rem; margin:.25rem 0 0; color:var(--ink)}}
.earlier__a{{font-family:var(--serif); font-style:italic; color:var(--muted); margin:.35rem 0 0}}
.earlier__a b{{font-style:normal; font-weight:500; color:var(--ink)}}
.ask{{
  font-family:var(--serif); font-weight:500; letter-spacing:-.01em;
  font-size:clamp(1.2rem,2.2vw,1.5rem); line-height:1.32; margin:.5rem 0 0;
}}
.revise{{font-family:var(--mono); font-size:.74rem; color:var(--muted); margin:1rem 0 0}}

.answers{{margin:2.4rem 0 0}}
.opt{{
  display:block; position:relative; cursor:pointer;
  border:1px solid var(--line); border-left:3px solid var(--line);
  padding:.9rem 1rem .9rem 2.7rem; margin:.65rem 0; background:var(--paper);
  transition:border-color .15s ease, background .15s ease;
}}
.opt:hover{{border-color:var(--muted)}}
.opt > input[type="radio"]{{position:absolute; opacity:0; width:0; height:0}}
.opt__dot{{
  position:absolute; left:1rem; top:1.05rem; width:15px; height:15px; border-radius:50%;
  border:2px solid var(--muted); background:var(--paper);
}}
.opt__label{{font-weight:600; font-size:1.02rem}}
.opt__why{{color:var(--muted); font-size:.92rem; margin:.3rem 0 0}}
.opt__note{{display:none; margin:.8rem 0 0}}
.opt:has(input:checked){{border-color:var(--mark); border-left-color:var(--mark); background:var(--mark-soft)}}
.opt:has(input:checked) .opt__dot{{border-color:var(--mark); background:var(--mark); box-shadow:inset 0 0 0 3px var(--mark-soft)}}
.opt:has(input:checked) .opt__note{{display:block}}
.opt-or{{
  font-family:var(--mono); font-size:.7rem; letter-spacing:.16em; text-transform:uppercase;
  color:var(--muted); text-align:center; margin:1.4rem 0 .4rem;
}}

label.field{{display:block; font-family:var(--mono); font-size:.7rem; letter-spacing:.08em;
  text-transform:uppercase; color:var(--muted); margin-bottom:.35rem}}
textarea, input[type=text]{{
  width:100%; font-family:var(--sans); font-size:.95rem; color:var(--ink);
  background:var(--paper); border:1px solid var(--line); border-radius:0;
  padding:.55rem .7rem; resize:vertical;
}}
textarea:focus, input[type=text]:focus{{border-color:var(--olivia); outline:none}}

.submit-row{{margin-top:2rem}}
.kbd-hint{{font-family:var(--mono); font-size:.72rem; color:var(--muted); margin:1rem 0 0}}
.kbd-hint kbd{{
  font-family:var(--mono); font-size:.9em; color:var(--ink); background:var(--paper-2);
  border:1px solid var(--line); border-radius:3px; padding:.05em .35em;
}}

@media (prefers-reduced-motion: no-preference){{
  .ask, .opt, .earlier{{animation:rise .5s cubic-bezier(.2,.7,.2,1) both}}
  @keyframes rise{{from{{opacity:0; transform:translateY(9px)}} to{{opacity:1; transform:none}}}}
  .node--live .node__dot{{animation:pulse 2.4s ease-in-out infinite}}
  @keyframes pulse{{
    0%,100%{{box-shadow:0 0 0 4px var(--mark-soft)}}
    50%{{box-shadow:0 0 0 7px rgba(169,118,46,.14)}}
  }}
}}
@media (max-width:520px){{
  .wrap{{padding-top:2rem}}
  .node__tag{{display:block; margin:.2rem 0 0}}
}}
</style></head><body>
{body}
</body></html>
"""


# Keyboard shortcuts for the question page (progressive enhancement -- the form
# works without it). Selection state is driven by the radios' :checked, which the
# CSS :has() rules react to, so we only need to flip .checked and manage focus.
KEYBOARD_JS = """<script>
(function(){
  var form = document.querySelector('form.answers');
  if(!form) return;
  var radios = [].slice.call(form.querySelectorAll('input[name="choiceId"]'));
  var custom = form.querySelector('input[name="customText"]');
  function typing(el){
    return el && (el.tagName === 'TEXTAREA' ||
                  (el.tagName === 'INPUT' && el.type === 'text'));
  }
  function pick(i, focusCustom){
    if(i < 0 || i >= radios.length) return;
    var r = radios[i];
    r.checked = true;
    r.dispatchEvent(new Event('change', {bubbles:true}));
    if(focusCustom && r.value === 'custom' && custom){ custom.focus(); custom.select(); }
    else if(r.closest('.opt')){ r.closest('.opt').scrollIntoView({block:'nearest'}); }
  }
  function current(){
    for(var i=0;i<radios.length;i++){ if(radios[i].checked) return i; }
    return -1;
  }
  function submit(){
    if(form.requestSubmit){ form.requestSubmit(); } else { form.submit(); }
  }
  document.addEventListener('keydown', function(e){
    if(e.key === 'Enter'){
      if(e.altKey) return;
      // Inside a textarea, Enter inserts a newline; require a modifier to record.
      var inTextarea = document.activeElement &&
                       document.activeElement.tagName === 'TEXTAREA';
      if(inTextarea){
        if(e.metaKey || e.ctrlKey){ e.preventDefault(); submit(); }
        return;
      }
      // Anywhere else (a highlighted answer, the custom text field, the page):
      // plain Enter records.
      e.preventDefault();
      submit();
      return;
    }
    if(e.metaKey || e.ctrlKey || e.altKey) return;
    var inText = typing(document.activeElement);
    if(!inText && e.key >= '1' && e.key <= '9'){
      var idx = parseInt(e.key, 10) - 1;
      if(idx < radios.length){ e.preventDefault(); pick(idx, true); }
      return;
    }
    if(!inText && (e.key === 'ArrowDown' || e.key === 'ArrowUp' ||
                   e.key === 'j' || e.key === 'k')){
      e.preventDefault();
      var down = (e.key === 'ArrowDown' || e.key === 'j');
      var cur = current();
      if(cur === -1){ cur = down ? -1 : radios.length; }
      var next = down ? cur + 1 : cur - 1;
      pick(Math.max(0, Math.min(radios.length - 1, next)), false);
      return;
    }
  });
})();
</script>"""


def render(title, body):
    return PAGE.format(title=html.escape(title), body=body)


def render_notice(heading, message, link_href: "Optional[str]" = "/",
                  link_label="Back to the record"):
    cta = ""
    if link_href:
        cta = ('<div class="cta-row"><a class="btn" href="%s">%s</a></div>'
               % (esc(link_href), esc(link_label)))
    body = (
        '<main class="wrap">'
        '<p class="eyebrow">Olivia Mode</p>'
        '<h1 class="mast__title">%s</h1>'
        '<p class="lead">%s</p>'
        '%s'
        '</main>'
    ) % (esc(heading), esc(message), cta)
    return render(heading, body)


def esc(s):
    return html.escape(s or "")


def _choice_label(q):
    """Human label for the answer chosen on q, or None if unanswered."""
    ans = q.get("answer") or {}
    choice = ans.get("choiceId")
    if not choice:
        return None
    if choice == "custom":
        return ans.get("customText") or "your own answer"
    for r in q.get("recommendations", []):
        if r["id"] == choice:
            return r["label"]
    return choice


def render_index(store, complete=False):
    data = store.snapshot()
    ordered = store.ordered()
    nxt = store.next_pending()

    total = len(ordered)
    done = sum(1 for q in ordered if q["status"] in ("answered", "resolved"))
    pct = int(round(done * 100 / total)) if total else 0
    started = done > 0

    parts = ['<main class="wrap">']
    parts.append('<header class="mast">')
    parts.append('<p class="eyebrow">Olivia Mode &middot; Interview</p>')
    parts.append('<h1 class="mast__title">%s</h1>' % esc(data["title"]))
    if total:
        parts.append(
            '<div class="progress">'
            '<span class="progress__track"><span class="progress__fill" style="width:%d%%"></span></span>'
            '<span class="progress__count">%d / %d on record</span></div>'
            % (pct, done, total))
    parts.append('</header>')

    if complete or not nxt:
        parts.append('<p class="lead">That&rsquo;s the whole record. Look it over, '
                     'or end the interview when you&rsquo;re satisfied.</p>')
    else:
        verb = "Resume the interview" if started else "Begin the interview"
        parts.append('<div class="cta-row">'
                     '<a class="btn btn--go" href="/q/%s">%s &rarr;</a></div>'
                     % (esc(nxt), verb))

    parts.append('<p class="section-label">The record</p>')
    if not ordered:
        parts.append('<p class="lead">No questions on the record yet.</p>')
    else:
        parts.append('<ol class="record">')
        for q in ordered:
            _, state_cls = STATUS_TAG.get(q["status"], ("", "is-pending"))
            live = q["id"] == nxt
            cls = "node %s%s" % (state_cls, " node--live" if live else "")
            tag = "up next" if live else STATUS_TAG.get(q["status"], ("", ""))[0]
            is_child = q.get("parentId") is not None
            branch = '<span class="node__branch">&#8627;</span>' if is_child else ""
            parts.append('<li class="%s">' % cls)
            parts.append('<span class="node__dot"></span>')
            parts.append('<a class="node__link" href="/q/%s">'
                         '<span class="node__id">%s</span>%s%s</a>'
                         '<span class="node__tag">%s</span>'
                         % (esc(q["id"]), esc(q["id"]), branch,
                            esc(q["text"]), esc(tag)))
            label = _choice_label(q)
            if label and q["status"] in ("answered", "resolved"):
                parts.append('<p class="node__said">you said: <b>%s</b></p>' % esc(label))
            parts.append('</li>')
        parts.append('</ol>')

    parts.append('<div class="foot"><form method="POST" action="/done">'
                 '<button class="btn btn--ghost" type="submit">End the interview</button>'
                 '</form></div>')
    parts.append('</main>')
    return render(data["title"], "\n".join(parts))


def render_question(store, qid):
    q = store.get(qid)
    if q is None:
        return None
    title = store.snapshot()["title"]
    state_tag = STATUS_TAG.get(q["status"], ("awaiting", ""))[0]

    parts = ['<main class="wrap">']
    parts.append('<div class="topbar">'
                 '<a class="back" href="/">&#8592; The record</a>'
                 '<span class="mono">%s &middot; %s</span></div>'
                 % (esc(qid), esc(state_tag)))

    parent = store.parent_of(qid)
    if parent:
        parts.append('<div class="earlier">')
        parts.append('<p class="eyebrow">Earlier</p>')
        parts.append('<p class="earlier__q">%s</p>' % esc(parent["text"]))
        label = _choice_label(parent)
        if label:
            parts.append('<p class="earlier__a">you said: <b>%s</b></p>' % esc(label))
        parts.append('</div>')

    parts.append('<p class="eyebrow">Olivia asks</p>')
    parts.append('<h1 class="ask">%s</h1>' % esc(q["text"]))
    if q["status"] != "pending":
        parts.append('<p class="revise">You&rsquo;ve answered this &mdash; revise it below if you like.</p>')

    current = q.get("answer") or {}
    parts.append('<form class="answers" method="POST" action="/answer">')
    parts.append('<input type="hidden" name="qid" value="%s">' % esc(qid))
    parts.append('<p class="section-label">Suggested answers</p>')

    for r in q["recommendations"]:
        selected = current.get("choiceId") == r["id"]
        checked = "checked" if selected else ""
        note_val = current.get("note", "") if selected else ""
        parts.append('<label class="opt">')
        parts.append('<input type="radio" name="choiceId" value="%s" %s>'
                     % (esc(r["id"]), checked))
        parts.append('<span class="opt__dot"></span>')
        parts.append('<span class="opt__label">%s</span>' % esc(r["label"]))
        if r.get("rationale"):
            parts.append('<p class="opt__why">%s</p>' % esc(r["rationale"]))
        parts.append('<span class="opt__note">'
                     '<label class="field">Note (optional)</label>'
                     '<textarea name="note_%s" rows="2" '
                     'placeholder="Anything to add?">%s</textarea></span>'
                     % (esc(r["id"]), esc(note_val)))
        parts.append('</label>')

    # Custom answer option
    selected = current.get("choiceId") == "custom"
    custom_checked = "checked" if selected else ""
    custom_text = current.get("customText", "") if selected else ""
    custom_note = current.get("note", "") if selected else ""
    parts.append('<p class="opt-or">or</p>')
    parts.append('<label class="opt">')
    parts.append('<input type="radio" name="choiceId" value="custom" %s>' % custom_checked)
    parts.append('<span class="opt__dot"></span>')
    parts.append('<span class="opt__label">In my own words</span>')
    parts.append('<span class="opt__note">'
                 '<label class="field">Your answer</label>'
                 '<input type="text" name="customText" placeholder="Type your answer" value="%s">'
                 '<label class="field" style="margin-top:.7rem">Note (optional)</label>'
                 '<textarea name="note_custom" rows="2" placeholder="Anything to add?">%s</textarea>'
                 '</span>' % (esc(custom_text), esc(custom_note)))
    parts.append('</label>')

    n_opts = len(q["recommendations"]) + 1
    parts.append('<div class="submit-row">'
                 '<button class="btn btn--go" type="submit">Record answer &rarr;</button>'
                 '</div>')
    parts.append('<p class="kbd-hint">'
                 '<kbd>&uarr;</kbd><kbd>&darr;</kbd> <kbd>j</kbd><kbd>k</kbd> '
                 'or <kbd>1</kbd>&ndash;<kbd>%d</kbd> to choose &middot; '
                 '<kbd>&crarr;</kbd> to record '
                 '(<kbd>&#8984;</kbd>/<kbd>Ctrl</kbd>+<kbd>&crarr;</kbd> inside a note)</p>' % n_opts)
    parts.append('</form>')
    parts.append(KEYBOARD_JS)
    parts.append('</main>')
    return render(title, "\n".join(parts))


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    # store and server are injected as class attributes at startup.
    store: "Optional[SessionStore]" = None
    server_ref: "Optional[ThreadingHTTPServer]" = None

    def log_message(self, format, *args):
        pass  # keep stdout clean for the event protocol

    # -- helpers ------------------------------------------------------------ #

    def _send_html(self, body, status=200):
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, obj, status=200):
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(length) if length else b""

    def _read_json(self):
        raw = self._read_body()
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    # -- GET ---------------------------------------------------------------- #

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        store = self.store

        if path == "/":
            qs = parse_qs(parsed.query)
            complete = qs.get("complete", ["0"])[0] == "1"
            self._send_html(render_index(store, complete=complete))
            return

        if path == "/next":
            nxt = store.next_pending()
            self._redirect("/q/%s" % nxt if nxt else "/?complete=1")
            return

        if path.startswith("/q/"):
            qid = path[len("/q/"):]
            body = render_question(store, qid)
            if body is None:
                self._send_html(render_notice(
                    "No such question",
                    "That question isn't on the record.", "/"), 404)
            else:
                self._send_html(body)
            return

        if path == "/api/state":
            self._send_json(store.snapshot())
            return

        if path == "/shutdown":
            self._send_html(render_notice(
                "Interview ended",
                "The record is saved. You can close this tab.", link_href=None))
            self._shutdown()
            return

        self._send_html(render_notice("Not found", "There's nothing here.", "/"), 404)

    # -- POST --------------------------------------------------------------- #

    def do_POST(self):
        path = urlparse(self.path).path
        store = self.store

        if path == "/answer":
            raw = self._read_body().decode("utf-8")
            form = parse_qs(raw, keep_blank_values=True)
            qid = form.get("qid", [""])[0]
            choice = form.get("choiceId", [""])[0]
            if not choice:
                self._send_html(render_notice(
                    "Pick an answer",
                    "Choose one of Olivia's suggestions or write your own before continuing.",
                    "/q/%s" % qid, "Back to the question"), 400)
                return
            custom_text = form.get("customText", [""])[0] if choice == "custom" else ""
            note = form.get("note_%s" % choice, [""])[0]
            nxt, err = store.answer(qid, choice, custom_text, note)
            if err:
                self._send_html(render_notice(
                    "That didn't work", err, "/q/%s" % qid, "Back to the question"), 400)
                return
            self._redirect("/q/%s" % nxt if nxt else "/?complete=1")
            return

        if path == "/done":
            emit("done")
            self._send_html(render_notice(
                "Interview ended",
                "The record is saved. You can close this tab.", link_href=None))
            self._shutdown()
            return

        if path == "/api/add":
            data = self._read_json()
            items = data.get("questions") if isinstance(data, dict) else data
            if isinstance(data, dict) and "questions" not in data:
                items = [data]  # allow a single question object
            created = store.add(items or [])
            self._send_json({"ok": True, "ids": created})
            return

        if path == "/api/resolve":
            data = self._read_json()
            ok = store.resolve(data.get("id", ""), data.get("reason", ""))
            self._send_json({"ok": ok}, 200 if ok else 404)
            return

        if path == "/api/update":
            data = self._read_json()
            ok = store.update(data.get("id", ""),
                              text=data.get("text"),
                              recommendations=data.get("recommendations"))
            self._send_json({"ok": ok}, 200 if ok else 404)
            return

        self._send_html(render_notice("Not found", "There's nothing here.", "/"), 404)

    def _shutdown(self):
        # server.shutdown() must run off the request thread or it deadlocks.
        threading.Thread(target=self.server_ref.shutdown, daemon=True).start()


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def _summarize(path):
    """Load a session file read-only and return a discovery summary, or None."""
    try:
        store = SessionStore(path)
    except (OSError, ValueError):
        return None
    ordered = store.ordered()
    total = len(ordered)
    answered = sum(1 for q in ordered if q["status"] in ("answered", "resolved"))
    pending = sum(1 for q in ordered if q["status"] == "pending")
    return {
        "path": store.path,
        "title": store.data.get("title", "Untitled plan"),
        "cwd": store.data.get("cwd"),
        "created": store.data.get("created"),
        "updated": store.data.get("updated"),
        "total": total,
        "answered": answered,
        "pending": pending,
        "complete": pending == 0 and total > 0,
    }


def cmd_sessions(args):
    """Print (as JSON) the sessions known for a working directory."""
    cwd = os.path.abspath(args.cwd)
    directory = _sessions_dir()

    if args.all:
        paths = sorted(glob.glob(os.path.join(directory, "*.json")))
    else:
        # Fast path: glob the cwd slug; confirm by the stored cwd below.
        stem = _slug(cwd)
        paths = sorted(set(glob.glob(os.path.join(directory, stem + ".json")))
                       | set(glob.glob(os.path.join(directory, stem + "-*.json"))))

    matches = []
    for p in paths:
        summary = _summarize(p)
        if summary is None:
            continue
        if args.all or summary.get("cwd") == cwd:
            matches.append(summary)
    matches.sort(key=lambda m: m.get("updated") or "", reverse=True)

    print(json.dumps({
        "dir": directory,
        "cwd": cwd,
        "matches": matches,
        "newPath": _new_path(cwd),
    }, indent=2))


def cmd_serve(args):
    store = SessionStore(args.file, default_title=args.title, cwd=args.cwd)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    Handler.store = store
    Handler.server_ref = httpd

    host, port = httpd.server_address[0], httpd.server_address[1]
    url = "http://%s:%d/" % (host, port)
    emit("ready", url=url, file=store.path, cwd=store.data.get("cwd"),
         title=store.data["title"], questions=len(store.data["questions"]))

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        emit("done", reason="interrupt")
    finally:
        httpd.server_close()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # Backward-compat: a bare invocation (no subcommand) means `serve`.
    if not argv or argv[0].startswith("-"):
        argv.insert(0, "serve")

    ap = argparse.ArgumentParser(description="Olivia Mode interview server")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_serve = sub.add_parser("serve", help="run the interview web server")
    p_serve.add_argument("--file", required=True, help="path to the session JSON")
    p_serve.add_argument("--cwd", default=os.getcwd(),
                         help="working dir this session belongs to (default: CWD)")
    p_serve.add_argument("--port", type=int, default=0, help="port (0 = pick a free one)")
    p_serve.add_argument("--host", default="127.0.0.1", help="bind host (default localhost)")
    p_serve.add_argument("--title", default=None, help="title for a new session")
    p_serve.set_defaults(func=cmd_serve)

    p_ls = sub.add_parser("sessions", help="list sessions known for a working dir")
    p_ls.add_argument("--cwd", default=os.getcwd(),
                      help="working dir to match (default: CWD)")
    p_ls.add_argument("--all", action="store_true", help="list every session")
    p_ls.set_defaults(func=cmd_sessions)

    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
