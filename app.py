"""Render link resolver with per-request progress events."""
import os, re, time, uuid, threading, ipaddress, socket
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse
from flask import Flask, jsonify, render_template, request
import unshorten

app = Flask(__name__)
MAX_BULK = 5
MAX_JOBS = 100
JOB_TTL = 900
JOBS = {}
LOCK = threading.Lock()
BROWSER_GATE = threading.BoundedSemaphore(1)
POOL = ThreadPoolExecutor(max_workers=3)
URL_RE = re.compile(r'https?://[^\s<>"\']+', re.I)
VALID_MODES = ('auto', 'http', 'browser')

def validate_url(raw):
    if not isinstance(raw, str) or len(raw) > 2048:
        raise ValueError('Invalid URL length')
    url = unshorten.normalize_url(raw.strip())
    parsed = urlparse(url)
    if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Only public HTTP(S) URLs are allowed')
    if not unshorten.is_public_host(parsed.hostname):
        raise ValueError('Private or reserved hosts are blocked')
    return url

def create_job(urls, mode):
    now = time.monotonic()
    with LOCK:
        for key, job in list(JOBS.items()):
            if now - job['created'] > JOB_TTL:
                del JOBS[key]
        if len(JOBS) >= MAX_JOBS:
            raise ValueError('Server busy; try again later')
        jid = uuid.uuid4().hex
        JOBS[jid] = {'created': now, 'events': deque(maxlen=200), 'status': 'queued', 'results': None}
    def event(stage, message):
        with LOCK:
            j = JOBS.get(jid)
            if j is not None:
                j['events'].append({'time': round(time.monotonic()-now, 1), 'stage': stage, 'message': str(message)[:240]})
    def work():
        with LOCK: JOBS[jid]['status'] = 'running'
        event('start', f'Started {len(urls)} link(s), mode: {mode}')
        results=[]
        try:
            for index, url in enumerate(urls,1):
                event('link', f'Processing link {index}/{len(urls)}: {urlparse(url).hostname}')
                # All browser-capable work serialized to prevent Chromium OOM.
                if mode != 'http':
                    event('queue', 'Waiting for available browser slot')
                    with BROWSER_GATE:
                        unshorten._EVENT_LOCAL.callback = event
                        try: result = unshorten.bypass(url, mode=mode)
                        finally: unshorten._EVENT_LOCAL.callback = None
                else:
                    unshorten._EVENT_LOCAL.callback = event
                    try: result = unshorten.bypass(url, mode=mode)
                    finally: unshorten._EVENT_LOCAL.callback = None
                data = result.to_dict()
                results.append(data)
                event('result', 'Success' if data.get('bypassed') else 'Unresolved: ' + str(data.get('error') or data.get('protection') or 'wall'))
            with LOCK:
                JOBS[jid]['results'] = results
                JOBS[jid]['status'] = 'done'
            event('done', 'Finished')
        except Exception as exc:
            event('error', f'Processing failed: {type(exc).__name__}')
            with LOCK: JOBS[jid]['status'] = 'error'
    POOL.submit(work)
    return jid

@app.get('/')
def index(): return render_template('index.html')

@app.get('/api/health')
def health(): return jsonify({'status':'ok'})

@app.post('/api/jobs')
def jobs_start():
    payload=request.get_json(silent=True) or {}
    mode=payload.get('mode','auto')
    if mode not in VALID_MODES: return jsonify({'error':'Invalid mode'}),400
    try:
        if 'text' in payload:
            urls=list(dict.fromkeys(URL_RE.findall(str(payload['text'])[:12000])))[:MAX_BULK]
        else:
            urls=[payload.get('url','')]
        if not urls: raise ValueError('No URLs provided')
        urls=[validate_url(x) for x in urls]
        jid=create_job(urls,mode)
    except ValueError as exc: return jsonify({'error':str(exc)}),400
    return jsonify({'job_id':jid}),202

@app.get('/api/jobs/<jid>')
def jobs_status(jid):
    with LOCK:
        j=JOBS.get(jid)
        if not j: return jsonify({'error':'Job not found or expired'}),404
        return jsonify({'status':j['status'], 'events':list(j['events']), 'results':j['results']})

@app.post('/api/expand')
def expand():
    return jsonify({'error':'Use /api/jobs for progress-aware processing'}),410

if __name__=='__main__':
    app.run(host='0.0.0.0',port=int(os.getenv('PORT','10000')))
