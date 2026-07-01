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

CLI:
    python3 olivia_server.py --file ./olivia-session.json [--port 0] [--title "..."]

Every notable event prints a single line to stdout, flushed immediately:

    OLIVIA_EVENT {"type": "...", ...}
"""

import argparse
import html
import json
import os
import sys
import tempfile
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse

VALID_STATUS = {"pending", "answered", "resolved", "skipped"}


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
    def __init__(self, path, default_title=None):
        self.path = os.path.abspath(path)
        self.lock = threading.RLock()
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as fh:
                self.data = json.load(fh)
            self._normalize()
        else:
            self.data = {
                "title": default_title or "Untitled plan",
                "created": _now_iso(),
                "questions": [],
            }
            self._persist()

    # -- internal helpers (assume lock held) -------------------------------- #

    def _normalize(self):
        """Fill in defaults so partially-authored files still load cleanly."""
        self.data.setdefault("title", "Untitled plan")
        self.data.setdefault("created", _now_iso())
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

STATUS_BADGE = {
    "pending": "[ ]",
    "answered": "[x]",
    "resolved": "[~]",
    "skipped": "[-]",
}

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>
body{{font-family:sans-serif;max-width:720px;margin:2rem auto;padding:0 1rem;line-height:1.5}}
.q{{border:1px solid #ccc;border-radius:6px;padding:.6rem .8rem;margin:.5rem 0}}
.rec{{border:1px solid #ddd;border-radius:6px;padding:.6rem .8rem;margin:.6rem 0}}
.rationale{{color:#555;font-size:.9em;margin:.2rem 0 .4rem 1.6rem}}
textarea{{width:100%;box-sizing:border-box}}
input[type=text]{{width:100%;box-sizing:border-box}}
.badge{{font-family:monospace}}
.muted{{color:#777}}
button{{padding:.4rem .9rem;margin-top:.6rem}}
.done{{margin-top:2rem;border-top:1px solid #ccc;padding-top:1rem}}
</style></head><body>
{body}
</body></html>
"""


def render(title, body):
    return PAGE.format(title=html.escape(title), body=body)


def esc(s):
    return html.escape(s or "")


def render_index(store, complete=False):
    data = store.snapshot()
    roots = [q for q in data["questions"] if q["parentId"] is None]
    nxt = store.next_pending()
    parts = ["<h1>%s</h1>" % esc(data["title"])]
    if complete:
        parts.append('<p><b>All questions answered.</b> Review below, or click '
                     'Done when finished.</p>')
    if nxt:
        parts.append('<p><a href="/q/%s"><b>&#9654; Continue to next question</b></a></p>'
                     % esc(nxt))
    parts.append("<h2>Questions</h2>")
    if not roots:
        parts.append('<p class="muted">No questions yet.</p>')
    for q in roots:
        badge = STATUS_BADGE.get(q["status"], "[ ]")
        parts.append('<div class="q"><span class="badge">%s</span> '
                     '<a href="/q/%s">%s</a></div>'
                     % (esc(badge), esc(q["id"]), esc(q["text"])))
    parts.append('<div class="done"><form method="POST" action="/done">'
                 '<button type="submit">Done &mdash; end session &amp; shut down</button>'
                 '</form></div>')
    return render(data["title"], "\n".join(parts))


def render_question(store, qid):
    q = store.get(qid)
    if q is None:
        return None
    title = store.snapshot()["title"]
    parts = ['<p><a href="/">&#8592; All questions</a></p>']

    parent = store.parent_of(qid)
    if parent:
        pa = ""
        if parent.get("answer"):
            choice = parent["answer"]["choiceId"]
            label = choice
            for r in parent["recommendations"]:
                if r["id"] == choice:
                    label = r["label"]
            if choice == "custom":
                label = parent["answer"].get("customText") or "custom answer"
            pa = ' &rarr; you chose: <i>%s</i>' % esc(label)
        parts.append('<p class="muted">Follow-up to: %s%s</p>'
                     % (esc(parent["text"]), pa))

    parts.append("<h1>%s</h1>" % esc(q["text"]))
    if q["status"] != "pending":
        parts.append('<p class="muted">Status: %s (you can re-answer below)</p>'
                     % esc(q["status"]))

    current = q.get("answer") or {}
    parts.append('<form method="POST" action="/answer">')
    parts.append('<input type="hidden" name="qid" value="%s">' % esc(qid))

    for r in q["recommendations"]:
        checked = "checked" if current.get("choiceId") == r["id"] else ""
        note_val = current.get("note", "") if current.get("choiceId") == r["id"] else ""
        parts.append('<div class="rec">')
        parts.append('<label><input type="radio" name="choiceId" value="%s" %s> '
                     '<b>%s</b></label>' % (esc(r["id"]), checked, esc(r["label"])))
        if r.get("rationale"):
            parts.append('<div class="rationale">%s</div>' % esc(r["rationale"]))
        parts.append('<label class="muted">Note: '
                     '<textarea name="note_%s" rows="2">%s</textarea></label>'
                     % (esc(r["id"]), esc(note_val)))
        parts.append('</div>')

    # Custom answer option
    custom_checked = "checked" if current.get("choiceId") == "custom" else ""
    custom_text = current.get("customText", "") if current.get("choiceId") == "custom" else ""
    custom_note = current.get("note", "") if current.get("choiceId") == "custom" else ""
    parts.append('<div class="rec">')
    parts.append('<label><input type="radio" name="choiceId" value="custom" %s> '
                 '<b>My own answer</b></label>' % custom_checked)
    parts.append('<input type="text" name="customText" placeholder="Your answer" value="%s">'
                 % esc(custom_text))
    parts.append('<label class="muted">Note: '
                 '<textarea name="note_custom" rows="2">%s</textarea></label>'
                 % esc(custom_note))
    parts.append('</div>')

    parts.append('<button type="submit">Submit &amp; continue</button>')
    parts.append('</form>')
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
                self._send_html(render("Not found", "<p>No such question.</p>"), 404)
            else:
                self._send_html(body)
            return

        if path == "/api/state":
            self._send_json(store.snapshot())
            return

        if path == "/shutdown":
            self._send_html(render("Done", "<p>Session ended. You can close this tab.</p>"))
            self._shutdown()
            return

        self._send_html(render("Not found", "<p>Not found.</p>"), 404)

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
                self._send_html(render("Error",
                    '<p>Please select an answer. <a href="/q/%s">Back</a></p>' % esc(qid)), 400)
                return
            custom_text = form.get("customText", [""])[0] if choice == "custom" else ""
            note = form.get("note_%s" % choice, [""])[0]
            nxt, err = store.answer(qid, choice, custom_text, note)
            if err:
                self._send_html(render("Error", "<p>%s</p>" % esc(err)), 400)
                return
            self._redirect("/q/%s" % nxt if nxt else "/?complete=1")
            return

        if path == "/done":
            emit("done")
            self._send_html(render("Done", "<p>Session ended. You can close this tab.</p>"))
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

        self._send_html(render("Not found", "<p>Not found.</p>"), 404)

    def _shutdown(self):
        # server.shutdown() must run off the request thread or it deadlocks.
        threading.Thread(target=self.server_ref.shutdown, daemon=True).start()


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(description="Olivia Mode interview server")
    ap.add_argument("--file", required=True, help="path to olivia-session.json")
    ap.add_argument("--port", type=int, default=0, help="port (0 = pick a free one)")
    ap.add_argument("--host", default="127.0.0.1", help="bind host (default localhost)")
    ap.add_argument("--title", default=None, help="title for a new session")
    args = ap.parse_args()

    store = SessionStore(args.file, default_title=args.title)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    Handler.store = store
    Handler.server_ref = httpd

    host, port = httpd.server_address[0], httpd.server_address[1]
    url = "http://%s:%d/" % (host, port)
    emit("ready", url=url, file=store.path, title=store.data["title"],
         questions=len(store.data["questions"]))

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        emit("done", reason="interrupt")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
