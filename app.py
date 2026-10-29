"""
Email Finder Pro — Flask web app
Features:
- Password-protected access using APP_PASSWORD and APP_SECRET environment variables
- Same-host website crawling with strict page/size/time limits
- Basic SSRF protections (rejects private, loopback, link-local, reserved IPs)
- Email deduplication, clean results table, CSV export
- Render/Gunicorn compatible

Use only on websites you are authorized to inspect. This tool finds publicly exposed
email addresses; it does not bypass logins, access controls, or anti-bot protections.
"""

import csv
import io
import ipaddress
import os
import re
import secrets
import socket
import time
from collections import deque
from functools import wraps
from urllib.parse import urljoin, urlparse, urldefrag, urlunparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from flask import (
    Flask, Response, abort, flash, redirect, render_template_string,
    request, session, url_for
)

from requests.exceptions import RequestException

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"), override=False)

APP_PASSWORD = os.getenv("APP_PASSWORD", "").strip()
APP_SECRET = os.getenv("APP_SECRET", "").strip()

# App can start on Render but login is disabled unless secrets are configured.
# This avoids accidentally exposing an unprotected scanner.
if len(APP_PASSWORD) < 12:
    raise RuntimeError(
        "APP_PASSWORD is missing or too short. Set a strong value (12+ characters) "
        "in Render Environment or your local .env file."
    )
if len(APP_SECRET) < 32:
    raise RuntimeError(
        "APP_SECRET is missing or too short. Generate one with: "
        "python -c \"import secrets; print(secrets.token_hex(32))\""
    )

app = Flask(__name__)
app.config.update(
    SECRET_KEY=APP_SECRET,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "true").lower() == "true",
    MAX_CONTENT_LENGTH=16 * 1024,
)

APP_NAME = "Email Finder Pro"
MAX_PAGES = 20
MAX_EMAILS = 500
RESULT_STORE = {}  # In-memory result cache; use one Gunicorn worker with this starter version.
MAX_PAGE_BYTES = 1_000_000
REQUEST_TIMEOUT = (4, 8)
USER_AGENT = "EmailFinderPro/1.0 (+public-page email discovery)"
EMAIL_RE = re.compile(
    r"(?i)(?<![a-z0-9.!#$%&'*+/=?^_`{|}~-])"
    r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}"
    r"@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+"
)

HTML = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }} — Email Finder Pro</title>
<style>
:root{color-scheme:dark;--bg:#0b1220;--panel:#121d30;--line:#27364d;--text:#edf3fb;--muted:#9fb0c7;--accent:#5eead4;--blue:#60a5fa;--danger:#fca5a5}
*{box-sizing:border-box}body{margin:0;background:linear-gradient(135deg,#0b1220,#101c30);color:var(--text);font:15px/1.5 system-ui,-apple-system,Segoe UI,Arial,sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:28px 18px 50px}.top{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:22px}
.brand{font-size:24px;font-weight:800;letter-spacing:-.5px}.brand span{color:var(--accent)}.muted{color:var(--muted)}.panel{background:rgba(18,29,48,.96);border:1px solid var(--line);border-radius:18px;padding:22px;margin:16px 0;box-shadow:0 12px 32px #0002}
h1{font-size:25px;margin:0 0 8px}h2{font-size:18px;margin:0 0 14px}.formrow{display:flex;gap:10px;flex-wrap:wrap}
input[type=url],input[type=password],input[type=text]{flex:1;min-width:220px;background:#0b1424;border:1px solid #34445c;border-radius:10px;color:var(--text);padding:13px 14px;font:inherit}
button,.btn{display:inline-block;background:var(--accent);color:#06211f;font-weight:800;border:0;border-radius:10px;padding:12px 16px;font:inherit;text-decoration:none;cursor:pointer}
button:hover,.btn:hover{filter:brightness(1.05)}.btn.secondary{background:#263a55;color:var(--text);border:1px solid #3c506b}
.note{font-size:13px;color:var(--muted);margin-top:10px}.flash{padding:12px 14px;border-radius:10px;background:#42212b;border:1px solid #713344;color:#ffe1e6;margin:10px 0}.success{background:#12352f;border-color:#246a5d;color:#d7fff7}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:12px}.stat{padding:15px;background:#0b1424;border:1px solid var(--line);border-radius:12px}.num{font-size:27px;font-weight:800;color:var(--accent)}.label{font-size:12px;color:var(--muted)}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:11px;border-bottom:1px solid var(--line);overflow-wrap:anywhere}th{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.06em}
.tablewrap{overflow:auto}.pill{display:inline-block;border:1px solid #2c5b55;color:#a7f3d0;background:#0d2927;padding:3px 8px;border-radius:999px;font-size:12px}
a{color:#93c5fd}.actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.inline{display:inline}.footer{font-size:12px;color:var(--muted);margin-top:22px}
@media(max-width:600px){.wrap{padding:18px 12px}.panel{padding:16px}.brand{font-size:21px}}
</style>
</head>
<body><main class="wrap">
<header class="top"><div class="brand">✉️ Email <span>Finder Pro</span></div>
{% if logged_in %}<form method="post" action="{{ url_for('logout') }}" class="inline"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><button class="btn secondary" type="submit">Log out</button></form>{% endif %}
</header>
{% with messages = get_flashed_messages() %}{% if messages %}{% for message in messages %}<div class="flash">{{ message }}</div>{% endfor %}{% endif %}{% endwith %}
{% if not logged_in %}
<section class="panel"><h1>Secure sign in</h1><p class="muted">Sign in to scan public website pages for visible email addresses.</p>
<form method="post" action="{{ url_for('login') }}"><input type="hidden" name="csrf_token" value="{{ csrf_token }}">
<div class="formrow"><input type="password" name="password" placeholder="Application password" required autocomplete="current-password"><button type="submit">Sign in</button></div></form>
<p class="note">The password is set by the site owner in the APP_PASSWORD environment variable.</p></section>
{% else %}
<section class="panel"><h1>Find emails on a website</h1><p class="muted">Enter a public website URL. The scanner stays on the same hostname and checks a limited number of pages.</p>
<form method="post" action="{{ url_for('scan') }}"><input type="hidden" name="csrf_token" value="{{ csrf_token }}">
<div class="formrow"><input type="url" name="target_url" placeholder="https://example.com" required maxlength="2048" value="{{ target_url or '' }}"><button type="submit">Scan website</button></div>
<p class="note">Limits: up to {{ max_pages }} pages, {{ max_emails }} unique emails, 1 MB per page. Only scan sites you are authorized to inspect.</p></form></section>
{% if results is not none %}
<section class="panel"><div class="top"><div><h2>Scan results</h2><div class="muted">Target: {{ target_url }}</div></div>
{% if results %}<a class="btn" href="{{ url_for('export_csv') }}">Export CSV</a>{% endif %}</div>
<div class="stats"><div class="stat"><div class="num">{{ results|length }}</div><div class="label">Unique emails found</div></div>
<div class="stat"><div class="num">{{ pages_scanned }}</div><div class="label">Pages scanned</div></div>
<div class="stat"><div class="num">{{ pages_failed }}</div><div class="label">Pages skipped / failed</div></div></div>
{% if results %}<div class="tablewrap" style="margin-top:18px"><table><thead><tr><th>#</th><th>Email address</th><th>Found on page</th></tr></thead><tbody>
{% for item in results %}<tr><td>{{ loop.index }}</td><td><a href="mailto:{{ item.email }}">{{ item.email }}</a></td><td><a href="{{ item.source }}" target="_blank" rel="noopener noreferrer">{{ item.source }}</a></td></tr>{% endfor %}
</tbody></table></div>{% else %}<p>No email addresses were found on the pages that could be scanned.</p>{% endif %}
</section>{% endif %}
{% endif %}
<div class="footer">Email Finder Pro · Public-page discovery only · Respect site terms, privacy, and applicable laws.</div>
</main></body></html>
"""


def get_csrf_token():
    token = session.get("_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf"] = token
    return token


@app.context_processor
def inject_template_globals():
    return {
        "csrf_token": get_csrf_token(),
        "logged_in": bool(session.get("authenticated")),
        "max_pages": MAX_PAGES,
        "max_emails": MAX_EMAILS,
    }


@app.before_request
def csrf_protect():
    if request.method == "POST":
        expected = session.get("_csrf", "")
        supplied = request.form.get("csrf_token", "")
        if not expected or not supplied or not secrets.compare_digest(expected, supplied):
            abort(400, description="Invalid form token. Refresh the page and try again.")


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("index"))
        return view(*args, **kwargs)
    return wrapped


def normalize_url(raw_url):
    raw_url = (raw_url or "").strip()
    if not raw_url or len(raw_url) > 2048:
        raise ValueError("Please enter a valid website URL.")
    if "://" not in raw_url:
        raw_url = "https://" + raw_url
    parsed = urlparse(raw_url)
    if parsed.scheme.lower() not in ("http", "https"):
        raise ValueError("Only HTTP and HTTPS URLs are allowed.")
    if not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("URL must include a hostname and must not contain username/password.")
    if parsed.port not in (None, 80, 443):
        raise ValueError("Only standard HTTP/HTTPS ports (80 and 443) are allowed.")
    host = parsed.hostname.rstrip(".").lower()
    if not host or host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise ValueError("Local and private hostnames are not allowed.")
    # Reject literal IPs unless they are globally routable.
    try:
        ip = ipaddress.ip_address(host)
        if not ip.is_global:
            raise ValueError("Private or reserved IP addresses are not allowed.")
    except ValueError as exc:
        if str(exc) == "Private or reserved IP addresses are not allowed.":
            raise
        # Non-IP hostnames are checked through DNS below.
    validate_public_dns(host)
    path = parsed.path or "/"
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", parsed.query, ""))


def validate_public_dns(host):
    try:
        records = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise ValueError("Hostname could not be resolved. Check the URL.")
    if not records:
        raise ValueError("Hostname has no usable IP address.")
    for record in records:
        ip = ipaddress.ip_address(record[4][0].split("%")[0])
        if not ip.is_global:
            raise ValueError("This hostname resolves to a private or reserved IP and is blocked.")


def safe_fetch(url):
    """Fetch a page only after URL/DNS validation; refuse redirects to other hosts."""
    validate_public_dns(urlparse(url).hostname)
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    with requests.get(
        url, headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=False,
        stream=True, verify=True
    ) as response:
        if response.is_redirect or response.is_permanent_redirect:
            location = response.headers.get("Location", "")
            if not location:
                raise ValueError("Page returned a redirect without a destination.")
            destination = urljoin(url, location)
            old = urlparse(url)
            new = urlparse(destination)
            if new.hostname != old.hostname or new.scheme not in ("http", "https"):
                raise ValueError("Cross-host redirects are blocked for safety.")
            validate_public_dns(new.hostname)
            # One same-host redirect hop only; caller will process it as a new URL.
            return None, destination, response.status_code
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").lower()
        if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
            return "", None, response.status_code
        chunks = []
        total = 0
        for chunk in response.iter_content(chunk_size=16384):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_PAGE_BYTES:
                raise ValueError("Page exceeds the 1 MB size limit.")
            chunks.append(chunk)
        encoding = response.encoding or "utf-8"
        return b"".join(chunks).decode(encoding, errors="replace"), None, response.status_code


def scan_site(start_url):
    start = normalize_url(start_url)
    start_host = urlparse(start).hostname
    queue = deque([start])
    queued = {urldefrag(start)[0]}
    visited = set()
    email_sources = {}
    pages_scanned = 0
    pages_failed = 0
    started = time.monotonic()
    max_duration = 90

    while queue and len(visited) < MAX_PAGES and len(email_sources) < MAX_EMAILS:
        if time.monotonic() - started > max_duration:
            break
        current = urldefrag(queue.popleft())[0]
        if current in visited:
            continue
        parsed_current = urlparse(current)
        if parsed_current.hostname != start_host or parsed_current.scheme not in ("http", "https"):
            continue
        visited.add(current)
        try:
            html, redirect_to, status = safe_fetch(current)
            if redirect_to:
                redirect_url = normalize_url(redirect_to)
                if urlparse(redirect_url).hostname == start_host and redirect_url not in queued:
                    queue.appendleft(redirect_url)
                    queued.add(redirect_url)
                continue
            pages_scanned += 1
            if not html:
                continue
            soup = BeautifulSoup(html, "html.parser")
            # Extract visible text, mailto links, and raw HTML.
            text_parts = [html, soup.get_text(" ", strip=True)]
            for anchor in soup.select('a[href^="mailto:"]'):
                href = anchor.get("href", "").split(":", 1)[-1].split("?", 1)[0]
                text_parts.append(href)

            for text_part in text_parts:
                for match in EMAIL_RE.findall(text_part):
                    email = match.strip(".,;:()[]{}<>\"'").lower()
                    if len(email) <= 254 and ".." not in email:
                        email_sources.setdefault(email, current)
                        if len(email_sources) >= MAX_EMAILS:
                            break
                if len(email_sources) >= MAX_EMAILS:
                    break

            for anchor in soup.find_all("a", href=True):
                if len(visited) + len(queue) >= MAX_PAGES:
                    break
                href = anchor.get("href", "").strip()
                if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
                    continue
                absolute = urldefrag(urljoin(current, href))[0]
                p = urlparse(absolute)
                if p.scheme not in ("http", "https") or p.hostname != start_host:
                    continue
                # Skip common non-page assets and logout/action links.
                if re.search(r"\.(pdf|jpg|jpeg|png|gif|svg|zip|rar|7z|mp4|mp3|css|js|woff2?)(?:$|\?)", p.path, re.I):
                    continue
                if any(word in p.path.lower() for word in ("/logout", "/signout", "/delete", "/checkout")):
                    continue
                if absolute not in queued and absolute not in visited:
                    queue.append(absolute)
                    queued.add(absolute)
        except (RequestException, ValueError, UnicodeError, socket.error):
            pages_failed += 1
            continue

    results = [{"email": email, "source": source} for email, source in sorted(email_sources.items())]
    return results, pages_scanned, pages_failed, start


@app.route("/", methods=["GET"])
def index():
    results = RESULT_STORE.get(session.get("result_id")) if session.get("result_id") else None
    return render_template_string(
        HTML, title=APP_NAME, results=results,
        target_url=session.get("target_url", ""),
        pages_scanned=session.get("pages_scanned", 0),
        pages_failed=session.get("pages_failed", 0),
    )


@app.route("/login", methods=["POST"])
def login():
    password = request.form.get("password", "")
    if secrets.compare_digest(password, APP_PASSWORD):
        session.clear()
        session["authenticated"] = True
        get_csrf_token()
        flash("Login successful.", "success")
        return redirect(url_for("index"))
    time.sleep(0.6)
    flash("Incorrect password. Please try again.")
    return redirect(url_for("index"))


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    session.clear()
    flash("You have been logged out.")
    return redirect(url_for("index"))


@app.route("/scan", methods=["POST"])
@login_required
def scan():
    target_url = request.form.get("target_url", "").strip()
    try:
        results, pages_scanned, pages_failed, normalized = scan_site(target_url)
        result_id = secrets.token_urlsafe(24)
        # Keep the in-memory cache bounded. A single Gunicorn worker is configured for this starter app.
        if len(RESULT_STORE) >= 100:
            RESULT_STORE.pop(next(iter(RESULT_STORE)))
        RESULT_STORE[result_id] = results
        session["result_id"] = result_id
        session["target_url"] = normalized
        session["pages_scanned"] = pages_scanned
        session["pages_failed"] = pages_failed
        if results:
            flash(f"Scan complete: {len(results)} unique email(s) found.", "success")
        else:
            flash("Scan complete. No email addresses were found on the pages scanned.", "success")
    except ValueError as exc:
        flash(str(exc))
    except Exception:
        app.logger.exception("Unexpected scan error")
        flash("An unexpected error occurred. Check the server logs; do not share secrets.")
    return redirect(url_for("index"))


@app.route("/export.csv", methods=["GET"])
@login_required
def export_csv():
    results = RESULT_STORE.get(session.get("result_id")) if session.get("result_id") else None
    if not isinstance(results, list):
        flash("No scan results available. Please scan a website first.")
        return redirect(url_for("index"))
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["Email", "Source Page"])
    for item in results:
        if isinstance(item, dict):
            writer.writerow([item.get("email", ""), item.get("source", "")])
    response = Response("\ufeff" + output.getvalue(), mimetype="text/csv; charset=utf-8")
    response.headers["Content-Disposition"] = 'attachment; filename="email_finder_results.csv"'
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.errorhandler(400)
def bad_request(error):
    return f"Bad request: {error.description}", 400


@app.errorhandler(413)
def too_large(_error):
    return "Request too large.", 413


@app.after_request
def add_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'unsafe-inline'; img-src 'self' data:; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'self'"
    )
    return response


if __name__ == "__main__":
    # For local development only. In Render, use the Gunicorn start command.
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "8000")), debug=False)
