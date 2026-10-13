# Scrapes Gop Render — live logs edition

Deploy as Render Docker Web Service, health check `/api/health`, Starter or higher recommended.

Live progress: `POST /api/jobs` JSON `{ "url": "https://example.com", "mode": "auto" }` returns `job_id`; poll `GET /api/jobs/<job_id>` for status/events/results. Bulk: send `text` instead of `url` (max 5).

Changes: live user-visible per-job logs; 75-second browser budget (previously 150s); serial browser access to avoid memory spikes; honest `http+browser` lane when fallback fails; improved failure propagation; removed permissive CORS. Jobs stored in memory and expire after 15 minutes; use a single Gunicorn worker.

Limitations: not guaranteed for protected links; CAPTCHA requires user action; browser destination network checks need additional hardening for adversarial public input; no authentication/rate limiting yet. Logs show major milestones, not every browser click. A worker restart loses jobs.
