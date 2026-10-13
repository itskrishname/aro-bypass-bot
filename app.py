"""LinkBypasser - demo web app that fully expands shortened links.

Run:  python app.py        ->  http://127.0.0.1:5000
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, jsonify, render_template, request
from flask_cors import CORS

from unshorten import bypass

app = Flask(__name__)
CORS(app)

URL_RE = re.compile(r"https?://[^\s<>\"']+|(?:www\.)?[a-z0-9-]+\.[a-z]{2,}(?:/\S*)?", re.I)
MAX_BULK = 25
VALID_MODES = ("auto", "http", "browser")


def extract_urls(text: str) -> list[str]:
    """Pull every URL out of free-form pasted text, de-duplicated, order kept."""
    found, seen = [], set()
    for match in URL_RE.findall(text or ""):
        url = match.rstrip(".,);")
        if url not in seen:
            seen.add(url)
            found.append(url)
    return found[:MAX_BULK]


def _mode() -> str:
    mode = (request.args.get("mode") or "auto").lower()
    return mode if mode in VALID_MODES else "auto"


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/expand", methods=["POST", "GET"])
def api_expand():
    """Single link. Accepts ?url=&mode= or JSON {"url": ..., "mode": "auto|http|browser"}"""
    payload = request.get_json(silent=True) or {}
    url = (payload.get("url") or request.args.get("url", "")).strip()
    if not url:
        return jsonify({"error": "No URL supplied"}), 400
    mode = (payload.get("mode") or _mode()).lower()
    if mode not in VALID_MODES:
        mode = "auto"
    return jsonify(bypass(url, mode=mode).to_dict())


@app.route("/api/bulk", methods=["POST"])
def api_bulk():
    """Paste many links, get the real destinations back."""
    payload = request.get_json(silent=True) or {}
    urls = extract_urls(payload.get("text", ""))
    if not urls:
        return jsonify({"error": "No URLs found in the input"}), 400
    mode = (payload.get("mode") or _mode()).lower()
    if mode not in VALID_MODES:
        mode = "auto"
    # the browser lane launches Chromium, so keep bulk fan-out modest
    workers = 3 if mode == "browser" else 6
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = [r.to_dict() for r in pool.map(lambda u: bypass(u, mode=mode), urls)]
    return jsonify({
        "count": len(results),
        "bypassed": sum(1 for r in results if r.get("bypassed")),
        "results": results,
    })


@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True, threaded=True)
