# Scrapes Gop — Render (Full Chromium)

## Deploy
1. Extract this ZIP and upload its contents to the ROOT of a GitHub repository.
2. Open https://dashboard.render.com/ > New > Web Service, connect the repository.
3. Choose Docker runtime (Dockerfile at repository root).
4. Select an instance with enough RAM for Chromium (Starter or higher recommended).
5. Deploy. Render sets PORT; Gunicorn binds to it automatically.
6. Visit your service URL and `/api/health` to verify it is up.

## Supported modes
- `http`: lightweight HTTP extraction
- `browser`: real Playwright Chromium automation
- `auto`: HTTP first, browser fallback

## Important
- Original extraction code is retained; Chromium runs in a Playwright base image.
- Browser-based bypass depends on the target website and can fail on CAPTCHAs, anti-bot measures, or changed pages.
- Bulk browser operations can exhaust RAM; avoid public heavy traffic without authentication, rate limiting, and job queue.
- The upstream URL validation has not been fully security audited for SSRF, DNS rebinding or all browser subrequests. Do not publicly expose to untrusted users before hardening.
- Docker image is large and the first build can take several minutes.
- Deployment is not live-tested here; local Python compilation passed.
