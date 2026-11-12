"""Provider calls, ported from the author's private image-generation scripts (Requesty,
OpenRouter, LLM Gateway, OpenRouter video). The request shapes, endpoints and quirks are the same as in those
scripts; what changed is only that every function takes the caller's key as an argument
(this server stores no keys) and raises ProviderError instead of exiting.

Endpoint specifics, per provider:

Requesty (REQUESTY_BASE_URL, default https://router.requesty.ai/v1)
- GET /models/all lists every model on the key; image models are those whose id holds
  "image" or whose api is "image".
- Without references: POST /images/generations (JSON). With references: POST /images/edits
  (multipart, field image[] repeated). size and quality are passed through as given; a size
  the model does not offer comes back as an HTTP error. png is asked for and converted.

OpenRouter (OPENROUTER_BASE_URL, default https://openrouter.ai/api/v1)
- One endpoint, POST /images, JSON, for text-to-image and references alike; references go in
  input_references as data URLs. Shape: aspect_ratio and resolution (512, 768, 1K, 1.5K, 2K,
  4K); size WxH only where a model lists it. GET /images/models lists supported_parameters
  per model; anything the chosen model does not list is left out (and reported).
- Prices: GET /images/models/{author}/{slug}/endpoints. GET /key: spending limit and what is
  left. HTTP 402 means the limit is reached. A failed generation is not billed.
- Python's default User-Agent is refused on some routes, so a plain one is sent.

LLM Gateway (LLMGATEWAY_BASE_URL, default https://api.llmgateway.io/v1)
- GET /models; image models have "image" in architecture.output_modalities. Price estimate:
  per_image, else request + image output tokens x token price, at the lowest tier.
- Without references: POST /images/generations; with references: POST /images/edits, JSON,
  "images": [{"image_url": <data URL>}], up to 16.
- size WxH (GPT Image, Qwen, Wan, Seedream, Z.AI); aspect_ratio (Gemini, Seedream; GPT Image
  refuses it; it wins over size); resolution tier (0.5K/1K/2K) is sent as size.
- A content-filter block returns an empty data array.

OpenRouter video
- POST /videos submits a job (202, id, polling_url); poll until completed / failed /
  cancelled / expired; GET /videos/{id}/content?index=0 is the MP4. Frames go in
  frame_images (first_frame / last_frame), references in input_references. Video models take
  JPEG or PNG only. Reference video and audio must be HTTPS URLs.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = {'User-Agent': 'image-gen-mcp'}
REQUESTY = os.environ.get('REQUESTY_BASE_URL', 'https://router.requesty.ai/v1').rstrip('/')
OPENROUTER = os.environ.get('OPENROUTER_BASE_URL', 'https://openrouter.ai/api/v1').rstrip('/')
LLMGATEWAY = os.environ.get('LLMGATEWAY_BASE_URL', 'https://api.llmgateway.io/v1').rstrip('/')

PROVIDERS = ('openrouter', 'llmgateway', 'requesty')


class ProviderError(Exception):
    def __init__(self, message: str, kind: str = 'http', status: int | None = None, endpoint: str = ''):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.endpoint = endpoint


def _request(url: str, key: str, body: bytes | None = None, content_type: str | None = None,
             timeout: int = 300) -> bytes:
    headers = {'Authorization': f'Bearer {key}', **UA}
    if content_type:
        headers['Content-Type'] = content_type
    req = urllib.request.Request(url, data=body, method='POST' if body is not None else 'GET', headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', 'replace')[:1000]
        hint = ' (402: the key\'s spending limit is reached or the account is out of credit; stop and tell the user)' \
            if e.code == 402 else ''
        raise ProviderError(f'HTTP {e.code} from provider{hint}: {detail}', 'http', e.code, url) from None
    except urllib.error.URLError as e:
        raise ProviderError(f'Could not reach provider: {e.reason}', 'connection', None, url) from None
    except TimeoutError:
        raise ProviderError('Provider timed out', 'connection', None, url) from None


def _json(url, key, payload=None, timeout=300):
    body = json.dumps(payload).encode() if payload is not None else None
    raw = _request(url, key, body, 'application/json' if body is not None else None, timeout)
    try:
        return json.loads(raw)
    except ValueError:
        raise ProviderError(f'Provider sent a non-JSON reply: {raw[:300]!r}', 'reply', None, url) from None


def data_url(data: bytes, mime: str) -> str:
    return f'data:{mime};base64,{base64.b64encode(data).decode()}'


def _multipart(fields: dict, files: list[tuple[str, str, bytes, str]]):
    boundary = 'imagegenmcp' + os.urandom(8).hex()
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    for name, filename, data, ctype in files:
        head = (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                f'filename="{filename}"\r\nContent-Type: {ctype}\r\n\r\n').encode()
        parts.append(head + data + b'\r\n')
    parts.append(f'--{boundary}--\r\n'.encode())
    return b''.join(parts), f'multipart/form-data; boundary={boundary}'


def _images_from_reply(reply: dict, endpoint: str, empty_hint: str = '') -> list[bytes]:
    images = reply.get('data') or []
    if not images:
        raise ProviderError(f'No image in the reply{empty_hint}: {json.dumps(reply)[:600]}', 'empty', None, endpoint)
    out = []
    for i, img in enumerate(images):
        if img.get('media_type') == 'image/svg+xml':
            raise ProviderError('The model returned SVG; pick a raster model', 'reply', None, endpoint)
        if not img.get('b64_json'):
            raise ProviderError(f'Image {i + 1} has no b64_json (keys {sorted(img)})', 'reply', None, endpoint)
        out.append(base64.b64decode(img['b64_json']))
    return out


def _cost(reply: dict):
    usage = reply.get('usage') or {}
    c = usage.get('cost')
    try:
        return float(c) if c is not None else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- shared cache
_cache: dict[str, tuple[float, object]] = {}
_cache_lock = threading.Lock()


def _cached(name: str, ttl: float, fn):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(name)
        if hit and now - hit[0] < ttl:
            return hit[1]
    value = fn()
    with _cache_lock:
        _cache[name] = (now, value)
    return value


# ---------------------------------------------------------------- Requesty
def requesty_models(key: str) -> list[dict]:
    data = _json(f'{REQUESTY}/models/all', key, timeout=60)
    rows = []
    for m in data.get('data', data) if isinstance(data, dict) else data:
        mid = m.get('id', '')
        if 'image' in mid.lower() or m.get('api') == 'image':
            rows.append({'id': mid, 'pricing': m.get('pricing', {})})
    return rows


def requesty_generate(key, *, model, prompt, size='1024x1024', quality='low', n=1, refs=()):
    if refs:
        endpoint = f'{REQUESTY}/images/edits'
        fields = {'model': model, 'prompt': prompt, 'size': size, 'quality': quality, 'n': str(n)}
        files = [('image[]', f'ref{i}.{mime.split("/")[1]}', data, mime) for i, (data, mime) in enumerate(refs)]
        body, ctype = _multipart(fields, files)
        reply = json.loads(_request(endpoint, key, body, ctype))
    else:
        endpoint = f'{REQUESTY}/images/generations'
        reply = _json(endpoint, key, {'model': model, 'prompt': prompt, 'size': size, 'quality': quality,
                                      'n': n, 'output_format': 'png'})
    return {'images': _images_from_reply(reply, endpoint), 'cost': _cost(reply), 'usage': reply.get('usage'),
            'notes': [], 'endpoint': endpoint}


# ---------------------------------------------------------------- OpenRouter
def openrouter_image_models(key: str) -> list[dict]:
    return _cached('or-image-models', 600, lambda: _json(f'{OPENROUTER}/images/models', key, timeout=60).get('data', []))


def _describe(params: dict) -> str:
    out = []
    for name, spec in sorted(params.items()):
        if spec.get('type') == 'enum':
            out.append(f'{name}={"|".join(str(v) for v in spec.get("values", []))}')
        elif spec.get('type') == 'range':
            out.append(f'{name}={spec.get("min")}..{spec.get("max")}')
        else:
            out.append(name)
    return '; '.join(out)


def openrouter_prices(key: str) -> list[dict]:
    """Every image model with its lowest output price and reference limit, cheapest first."""
    def build():
        models = openrouter_image_models(key)

        def one(m):
            params = m.get('supported_parameters', {})
            refs = params.get('input_references', {}).get('max', 0)
            row = {'id': m['id'], 'max_refs': refs, 'params': _describe(params)}
            try:
                eps = _json(OPENROUTER + m['endpoints'].split('/api/v1', 1)[-1], key, timeout=30).get('endpoints', [])
            except Exception:
                eps = []
            out = [p for p in (eps[0].get('pricing', []) if eps else []) if p.get('billable') == 'output_image']
            if not out:
                row.update(price=None, unit=None, sort=9e9, price_text='no endpoint or no output price')
                return row
            p = min(out, key=lambda p: float(p['cost_usd']))
            cost = float(p['cost_usd'])
            if p['unit'] == 'image':
                row.update(price=cost, unit='image', sort=cost, price_text=f'${cost:.3f} per image (lowest tier)')
            elif p['unit'] == 'megapixel':
                row.update(price=cost, unit='megapixel', sort=cost, price_text=f'${cost:.3f} per megapixel (~1 MP at 1K)')
            else:
                row.update(price=cost * 1e6, unit='million output tokens', sort=1e6 + cost,
                           price_text=f'${cost * 1e6:.1f} per million output tokens (tokens per image vary '
                                      'widely; run once and read the cost)')
            return row

        with ThreadPoolExecutor(8) as ex:
            rows = list(ex.map(one, models))
        return sorted(rows, key=lambda r: r['sort'])
    return _cached('or-prices', 6 * 3600, build)


def openrouter_key_status(key: str) -> dict:
    d = _json(f'{OPENROUTER}/key', key, timeout=30).get('data', {})
    out = {'limit': d.get('limit'), 'limit_remaining': d.get('limit_remaining'), 'limit_reset': d.get('limit_reset'),
           'usage_total': d.get('usage'), 'usage_daily': d.get('usage_daily')}
    if d.get('limit') is None:
        out['summary'] = f'no spending limit set; ${d.get("usage", 0) or 0:.2f} used'
    else:
        out['summary'] = (f'${d.get("limit_remaining", 0) or 0:.2f} of ${d["limit"]:.2f} left '
                          f'({d.get("limit_reset") or "no reset"} limit); ${d.get("usage_daily", 0) or 0:.2f} used today')
    return out


def openrouter_generate(key, *, model, prompt, quality='low', aspect_ratio=None, resolution=None, size=None,
                        n=1, background=None, seed=None, refs=()):
    entry = next((m for m in openrouter_image_models(key) if m.get('id') == model), None)
    if entry is None:
        raise ProviderError(f'{model} is not in OpenRouter /images/models; pick one from list_models', 'reply')
    supported = entry.get('supported_parameters', {})
    payload = {'model': model, 'prompt': prompt}
    notes = []
    wanted = {'quality': quality, 'aspect_ratio': aspect_ratio, 'resolution': resolution, 'size': size,
              'n': n if n != 1 else None, 'background': background, 'seed': seed}
    for name, value in wanted.items():
        if value is None:
            continue
        if name in supported:
            payload[name] = value
        elif name != 'quality' or quality != 'low':  # the default needs no note
            notes.append(f'{model} does not list {name}; not sent')
    if 'png' in supported.get('output_format', {}).get('values', []):
        payload['output_format'] = 'png'
    if refs:
        max_refs = supported.get('input_references', {}).get('max', 0)
        if len(refs) > max_refs:
            raise ProviderError(f'{model} takes at most {max_refs} reference images, not {len(refs)}', 'reply')
        payload['input_references'] = [{'type': 'image_url', 'image_url': {'url': data_url(d, m)}} for d, m in refs]
    endpoint = f'{OPENROUTER}/images'
    reply = _json(endpoint, key, payload)
    return {'images': _images_from_reply(reply, endpoint), 'cost': _cost(reply), 'usage': reply.get('usage'),
            'notes': notes, 'endpoint': endpoint}


# ---------------------------------------------------------------- LLM Gateway
def _estimate(pricing: dict):
    per_image = pricing.get('per_image') or {}
    if per_image:
        tier, price = min(per_image.items(), key=lambda kv: float(kv[1]))
        return float(price), tier
    request = float(pricing.get('request') or 0)
    tokens = pricing.get('image_output_tokens_by_resolution') or {}
    out_price = float(pricing.get('image_output') or 0)
    if tokens and out_price:
        tier, n = min(tokens.items(), key=lambda kv: kv[1])
        return request + n * out_price, tier
    return (request or None), None


def llmgateway_models(key: str) -> list[dict]:
    def build():
        data = _json(f'{LLMGATEWAY}/models', key, timeout=60)
        rows = []
        for m in data.get('data', data) if isinstance(data, dict) else data:
            arch = m.get('architecture') or {}
            if 'image' not in arch.get('output_modalities', []):
                continue
            price, tier = _estimate(m.get('pricing') or {})
            rows.append({'id': m['id'], 'est_price': price, 'tier': tier,
                         'refs': 'image' in arch.get('input_modalities', []), 'family': m.get('family', '')})
        return sorted(rows, key=lambda r: r['est_price'] if r['est_price'] is not None else 9e9)
    return _cached('llmg-models', 6 * 3600, build)


def llmgateway_generate(key, *, model, prompt, size=None, aspect_ratio=None, resolution=None, quality=None,
                        n=1, refs=()):
    if size and resolution:
        raise ProviderError('give size or resolution, not both', 'reply')
    payload = {'model': model, 'prompt': prompt, 'n': n}
    if size or resolution:
        payload['size'] = size or resolution
    if aspect_ratio:
        payload['aspect_ratio'] = aspect_ratio
    if quality:
        payload['quality'] = quality
    if refs:
        if len(refs) > 16:
            raise ProviderError('LLM Gateway takes at most 16 reference images', 'reply')
        payload['images'] = [{'image_url': data_url(d, m)} for d, m in refs]
    endpoint = f'{LLMGATEWAY}/images/edits' if refs else f'{LLMGATEWAY}/images/generations'
    reply = _json(endpoint, key, payload)
    imgs = _images_from_reply(reply, endpoint, ' (an empty data array is how a content filter answers)')
    return {'images': imgs, 'cost': _cost(reply), 'usage': reply.get('usage'), 'notes': [], 'endpoint': endpoint}


# ---------------------------------------------------------------- OpenRouter video
def video_models(key: str) -> list[dict]:
    data = _cached('or-video-models', 600, lambda: _json(f'{OPENROUTER}/videos/models', key, timeout=60).get('data', []))
    return [{'id': m['id'], 'frames': m.get('supported_frame_images'), 'durations': m.get('supported_durations'),
             'resolutions': m.get('supported_resolutions'), 'aspect_ratios': m.get('supported_aspect_ratios'),
             'audio': m.get('generate_audio'), 'pricing_skus': m.get('pricing_skus')} for m in data]


def video_submit(key: str, payload: dict) -> dict:
    job = _json(f'{OPENROUTER}/videos', key, payload)
    if not job.get('id'):
        raise ProviderError(f'No job id in the reply: {json.dumps(job)[:600]}', 'reply', None, f'{OPENROUTER}/videos')
    return job


def video_job(key: str, job_id: str) -> dict:
    if not job_id.replace('-', '').replace('_', '').isalnum():
        raise ProviderError('Invalid job id', 'reply')
    return _json(f'{OPENROUTER}/videos/{job_id}', key, timeout=60)


def video_content(key: str, job_id: str) -> bytes:
    return _request(f'{OPENROUTER}/videos/{job_id}/content?index=0', key, timeout=600)
