"""Files, logs and limits kept on the server. Nothing here ever holds a provider key.

- Generated images, videos and uploaded references live under DATA_DIR/files with an
  unguessable name and are deleted after FILE_TTL_HOURS.
- costs.json: the price level seen in real runs, one entry per provider/model (last cost per
  image or clip, date, request shape, min, max, runs). Shared by every caller; it holds no
  prompts and nothing that identifies a caller.
- rejections.jsonl: the last 200 failed requests (provider, endpoint, model, shape, status,
  message, prompt hash). The prompt itself is not kept: this is a shared server.
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import ipaddress
import json
import os
import re
import secrets
import socket
import threading
import time
import urllib.parse
import urllib.request
from collections import defaultdict, deque

DATA_DIR = os.environ.get('DATA_DIR', '/data')
FILES = os.path.join(DATA_DIR, 'files')
COST_LOG = os.path.join(DATA_DIR, 'costs.json')
REJECTION_LOG = os.path.join(DATA_DIR, 'rejections.jsonl')
REJECTION_KEEP = 200
FILE_TTL_HOURS = float(os.environ.get('FILE_TTL_HOURS', '168'))
PUBLIC_URL = os.environ.get('PUBLIC_URL', 'http://127.0.0.1:8000').rstrip('/')
MAX_REF_BYTES = int(os.environ.get('MAX_REF_BYTES', str(15 * 1024 * 1024)))

NAME_RE = re.compile(r'^[A-Za-z0-9_-]{16,64}\.(webp|mp4|png|jpg)$')
_lock = threading.Lock()


def _today():
    return datetime.date.today().isoformat()


def ensure_dirs():
    os.makedirs(FILES, exist_ok=True)


# ---------------------------------------------------------------- files
def save_file(data: bytes, ext: str) -> tuple[str, str]:
    """Store bytes under a random name. Returns (name, public URL)."""
    ensure_dirs()
    name = f'{secrets.token_urlsafe(18)}.{ext}'
    with open(os.path.join(FILES, name), 'wb') as f:
        f.write(data)
    return name, f'{PUBLIC_URL}/files/{name}'


def file_path(name: str) -> str | None:
    if not NAME_RE.match(name):
        return None
    p = os.path.join(FILES, name)
    return p if os.path.isfile(p) else None


def cleanup():
    if not os.path.isdir(FILES):
        return 0
    cutoff = time.time() - FILE_TTL_HOURS * 3600
    removed = 0
    for n in os.listdir(FILES):
        p = os.path.join(FILES, n)
        try:
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
                removed += 1
        except OSError:
            pass
    return removed


# ---------------------------------------------------------------- reference images
class RefError(ValueError):
    pass


class _Refuse(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RefError(f'Reference URL redirects ({code}); give the final https URL instead')


# Redirects are refused so a public URL cannot bounce the fetch to a private address.
_NO_REDIRECT = urllib.request.build_opener(_Refuse)


def _public_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


def load_ref(ref: str) -> bytes:
    """A reference given as this server's file URL, an https URL, a data URL or bare base64."""
    ref = ref.strip()
    own = f'{PUBLIC_URL}/files/'
    if ref.startswith(own) or ref.startswith('/files/'):
        name = ref.rsplit('/', 1)[-1]
        p = file_path(name)
        if not p:
            raise RefError(f'{ref} is not on this server (expired or never uploaded)')
        with open(p, 'rb') as f:
            return f.read()
    if ref.startswith('data:'):
        try:
            return base64.b64decode(ref.split(',', 1)[1], validate=False)
        except Exception:
            raise RefError('Unreadable data URL') from None
    if ref.startswith('https://'):
        host = urllib.parse.urlsplit(ref).hostname or ''
        if not _public_host(host):
            raise RefError(f'Reference host {host!r} is not a public address')
        req = urllib.request.Request(ref, headers={'User-Agent': 'image-gen-mcp'})
        try:
            with _NO_REDIRECT.open(req, timeout=60) as resp:
                data = resp.read(MAX_REF_BYTES + 1)
        except Exception as e:
            raise RefError(f'Could not fetch reference {ref}: {e}') from None
        if len(data) > MAX_REF_BYTES:
            raise RefError(f'Reference {ref} is larger than {MAX_REF_BYTES // 1048576} MB')
        return data
    if ref.startswith('http://'):
        raise RefError('Only https URLs are fetched')
    try:
        data = base64.b64decode(ref, validate=True)
    except Exception:
        raise RefError('A reference must be an https URL, this server\'s /files/ URL (from upload or an '
                       'earlier result), a data: URL, or base64. Local paths cannot be read by a remote server: '
                       'upload the file first (see the upload instructions in generate_image).') from None
    if len(data) > MAX_REF_BYTES:
        raise RefError('Reference is too large')
    return data


# ---------------------------------------------------------------- logs
def log_cost(provider: str, model: str, cost, shape: dict, items: int = 1):
    if cost is None:
        return None
    per = round(float(cost) / max(items, 1), 6)
    with _lock:
        data = read_costs()
        key = f'{provider}/{model}'
        e = data.get(key, {})
        e.update({'last_cost': per, 'last_date': _today(), 'shape': shape,
                  'min': min(e.get('min', per), per), 'max': max(e.get('max', per), per),
                  'runs': e.get('runs', 0) + 1})
        data[key] = e
        ensure_dirs()
        tmp = COST_LOG + '.tmp'
        with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
            json.dump(dict(sorted(data.items())), f, indent=1)
            f.write('\n')
        os.replace(tmp, COST_LOG)
    return e


def read_costs() -> dict:
    if os.path.exists(COST_LOG):
        try:
            with open(COST_LOG, encoding='utf-8') as f:
                return json.load(f)
        except ValueError:
            return {}
    return {}


def log_rejection(provider, endpoint, model, shape: dict, kind, message, status=None, prompt=''):
    entry = {
        'time': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'provider': provider, 'endpoint': endpoint, 'model': model, **shape,
        'kind': kind, 'status': status, 'message': (message or '')[:400],
        'prompt_sha256': hashlib.sha256(prompt.encode('utf-8')).hexdigest()[:12] if prompt else None,
    }
    with _lock:
        ensure_dirs()
        lines = []
        if os.path.exists(REJECTION_LOG):
            with open(REJECTION_LOG, encoding='utf-8') as f:
                lines = [ln for ln in f.read().splitlines() if ln.strip()]
        lines.append(json.dumps(entry, ensure_ascii=False))
        with open(REJECTION_LOG, 'w', encoding='utf-8', newline='\n') as f:
            f.write('\n'.join(lines[-REJECTION_KEEP:]) + '\n')
    return entry


def read_rejections(model: str | None = None, limit: int = 20) -> list[dict]:
    if not os.path.exists(REJECTION_LOG):
        return []
    with open(REJECTION_LOG, encoding='utf-8') as f:
        rows = [json.loads(ln) for ln in f if ln.strip()]
    if model:
        rows = [r for r in rows if model.lower() in (r.get('model') or '').lower()]
    return rows[-limit:]


# ---------------------------------------------------------------- rate limit
class RateLimiter:
    """Sliding windows per client: paid calls per minute and per day."""

    def __init__(self, per_minute: int, per_day: int):
        self.per_minute, self.per_day = per_minute, per_day
        self.hits: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def check(self, client: str) -> str | None:
        now = time.time()
        with self.lock:
            q = self.hits[client]
            while q and now - q[0] > 86400:
                q.popleft()
            if len(q) >= self.per_day:
                return f'Daily limit of {self.per_day} paid calls reached for this client'
            if sum(1 for t in q if now - t < 60) >= self.per_minute:
                return f'Rate limit: at most {self.per_minute} paid calls a minute'
            q.append(now)
        return None
