"""image-gen-mcp: image and video generation through Requesty, OpenRouter and LLM Gateway,
as a remote MCP server (streamable HTTP).

Bring your own key: the server stores no provider keys. Each client sends its own keys as
HTTP headers on every request (X-OpenRouter-Key, X-LLMGateway-Key, X-Requesty-Key); the
call is billed to that key. Keys are never logged, stored or returned.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Annotated, Literal

import anyio
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ImageContent, TextContent
from pydantic import Field
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

from . import imaging, providers, store

VERSION = '1.0.0'
KEY_HEADERS = {'openrouter': 'x-openrouter-key', 'llmgateway': 'x-llmgateway-key', 'requesty': 'x-requesty-key'}
DEFAULT_MODELS = {
    'openrouter': os.environ.get('DEFAULT_OPENROUTER_MODEL', 'bytedance-seed/seedream-5-0-flash'),
    'llmgateway': os.environ.get('DEFAULT_LLMGATEWAY_MODEL', 'qwen-image-2.1-turbo'),
    'requesty': os.environ.get('DEFAULT_REQUESTY_MODEL', 'vertex/google/gemini-2.5-flash-image'),
}
SERVER_TOKEN = os.environ.get('SERVER_TOKEN', '').strip()
TRUSTED_IP_HEADER = os.environ.get('TRUSTED_IP_HEADER', '').strip().lower()
PUBLIC_HOST = os.environ.get('PUBLIC_HOST', '').strip()
TTL_TEXT = f'{store.FILE_TTL_HOURS:g} hours'
limiter = store.RateLimiter(int(os.environ.get('RATE_PER_MINUTE', '20')), int(os.environ.get('RATE_PER_DAY', '500')))

INSTRUCTIONS = f"""Image and video generation through OpenRouter, LLM Gateway and Requesty, billed to the
caller's own provider keys (headers X-OpenRouter-Key, X-LLMGateway-Key, X-Requesty-Key, or X-API-Key
for the provider in the URL path; the server stores none). With no provider given, tools use the
connection's provider (/llmgateway/mcp, /requesty/mcp), else openrouter. Use generate_image for images, list_models for models and prices,
key_status for what is left on the keys, generate_video / video_status for short clips.

Rules:
- Cheapest suitable model at its lowest quality is the standard; spend more only when the user
  asks for something specific. Default: OpenRouter bytedance-seed/seedream-5-0-flash ($0.018).
- Before a batch, tell the user how many images at what price and check key_status.
- HTTP 402 means a key's spending limit is reached: stop and tell the user. Never switch to
  another key or provider to carry on unless the user asks in this task.
- Results come back as a preview inline plus an HTTPS URL valid for {TTL_TEXT}; download it
  (curl -fsSL -o out.webp URL) to keep the file. Local paths cannot be sent to this server: pass
  references as https URLs, data URLs or base64, or upload them first (POST /upload).
"""

PRICE_GUIDE = """Known models and prices (real runs, 2026-11; recheck with list_models):
- openrouter bytedance-seed/seedream-5-0-flash: $0.018/image, up to 14 refs, no watermark. DEFAULT for
  everything (portraits, scenes, icons); accepts named-character prompts with references and keeps faces.
- openrouter openai/gpt-image-1-mini (quality low): ~$0.002-0.007, cheapest; newer OpenAI-family models
  refuse more identity prompts.
- llmgateway qwen-image-2.1-turbo: $0.016, no mark (openrouter qwen/qwen-image-2.1-turbo: $0.018).
- llmgateway gemini-nano-banana-2.1 ("Nano Banana"): ~$0.04-0.06. The step up when the user asks for a
  better result: covers, posters with lettering, group scenes, clean flat-vector illustrations; got a
  four-character poster right first try where Seedream needed four tries. Through OpenRouter
  (google/gemini-nano-banana-2.1, ~$0.036) it refused named-character portraits.
- openrouter x-ai/grok-imagine-image-2.0: $0.06, max 3 refs; when likeness matters most.
- openrouter google/gemini-3.1-flash-image ~$0.068; google/gemini-2.5-flash-image ~$0.039 (older Gemini
  image models accept named fiction identities more readily).
- llmgateway seedream-5-0-flash adds a small "AI generated" mark: take Seedream from OpenRouter.
- requesty vertex/google/gemini-2.5-flash-image: worked (default there; cost about $0.04, not reported in
  the reply). On Requesty use the routed ids with a provider prefix (vertex/google/...): the bare
  google/gemini-2.5-flash-image is in its model list but answered 404 "model not found" (2026-11-12)."""


def _headers(ctx: Context):
    return ctx.headers or {}


PATH_PROVIDER_HEADER = 'x-imagegen-path-provider'  # set by build_app() from the URL, never by clients


def _path_provider(headers) -> str:
    return headers.get(PATH_PROVIDER_HEADER) or 'openrouter'


def _clean(v: str | None) -> str:
    v = (v or '').strip()
    if v.lower().startswith('bearer '):
        v = v[7:].strip()
    return '' if v.startswith('${') else v


def _sent_key(headers, provider: str) -> str:
    """The provider's own header wins; X-API-Key counts for the provider the URL path names
    (/openrouter/mcp, /llmgateway/mcp, /requesty/mcp; plain /mcp means openrouter)."""
    v = _clean(headers.get(KEY_HEADERS[provider]))
    if not v and _path_provider(headers) == provider:
        v = _clean(headers.get('x-api-key'))
    return v


def _provider(ctx: Context, provider: str | None) -> str:
    return provider or _path_provider(_headers(ctx))


def _key(ctx: Context, provider: str) -> str:
    v = _sent_key(_headers(ctx), provider)
    if not v:
        raise ToolError(f'No {provider} key in this connection. Send "{KEY_HEADERS[provider]}: <key>", or '
                        f'"X-API-Key: <key>" on the /{provider}/mcp URL (see the server README). '
                        'The server stores no keys.')
    return v


def _client_id(request: Request | None) -> str:
    if request is None:
        return 'unknown'
    if TRUSTED_IP_HEADER and request.headers.get(TRUSTED_IP_HEADER):
        return request.headers[TRUSTED_IP_HEADER]
    return request.client.host if request.client else 'unknown'


def _check_token(headers) -> bool:
    if not SERVER_TOKEN:
        return True
    auth = headers.get('authorization', '')
    return auth == f'Bearer {SERVER_TOKEN}'


def _rate(ctx: Context):
    req = getattr(ctx.request_context, 'request', None)
    msg = limiter.check(_client_id(req))
    if msg:
        raise ToolError(msg)


def _text(obj) -> TextContent:
    return TextContent(type='text', text=json.dumps(obj, indent=1, ensure_ascii=False))


def _load_refs(refs: list[str] | None) -> list[tuple[bytes, str]]:
    out = []
    for r in refs or []:
        try:
            data = store.load_ref(r)
        except store.RefError as e:
            raise ToolError(str(e)) from None
        imaging.open_image(data)  # validates
        out.append((data, imaging.sniff_mime(data)))
    return out


mcp = MCPServer(
    name='image-gen',
    title='Image generation (OpenRouter, LLM Gateway, Requesty)',
    version=VERSION,
    instructions=INSTRUCTIONS,
)


@mcp.tool(
    description=f"""Generate one or more images and return them as an inline preview plus an HTTPS URL to the
full WebP (valid {TTL_TEXT}; download it with curl to keep it). Billed to the caller's key for the chosen
provider (header X-OpenRouter-Key / X-LLMGateway-Key / X-Requesty-Key, or X-API-Key for the connection's
provider). The reply gives the real cost.

Use the cheapest suitable model at its lowest quality unless the user asks for something specific. With
no model, the provider's default is used (openrouter: bytedance-seed/seedream-5-0-flash, $0.018).
{PRICE_GUIDE}

Shape, per provider:
- openrouter: aspect_ratio (1:1, 2:3, 3:2, 16:9, 9:16, ...) and resolution (512, 768, 1K, 1.5K, 2K, 4K);
  size WxH only for models that list it. Parameters the model does not list are dropped and reported.
- llmgateway: size WxH (GPT Image, Qwen, Wan, Seedream, Z.AI; GPT Image sides divisible by 16) or
  aspect_ratio (Gemini, Seedream; GPT Image refuses it); resolution tier 0.5K/1K/2K (Gemini, Wan).
  Give size or resolution, not both. quality only when given.
- requesty: size WxH (default 1024x1024) and quality (default low). Default model
  vertex/google/gemini-2.5-flash-image; Requesty replies carry no cost, so check its dashboard.

Post-processing (all providers): width scales to that width keeping the shape; fit WxH crops and
resizes to exactly that size (anchor auto/top/center/bottom picks what the crop keeps; auto = top for
portrait targets) and the uncropped original is returned too as full_url.

refs: reference images for identity/style (image-to-image). Each is an https URL, this server's
/files/ URL (an earlier result or an upload), a data: URL or bare base64. A local file must be uploaded
first: curl -fsS -X POST --data-binary @ref.webp -H "X-OpenRouter-Key: $OPENROUTER_API_KEY"
https://<this server>/upload  -> {{"url": ...}}. How many refs a model takes depends on the model.

Moderation differs per model, not per prompt: write a real person's or a named character's identity
neutrally first ("this man in the reference"); if a model still refuses, switch model rather than
rewording further (recent_rejections shows what a model refused). Never switch provider or key after
an HTTP 402 (spending limit): stop and tell the user.

inline: 'preview' (default, longest side 768 px), 'full' or 'none'.""",
    structured_output=False,
)
async def generate_image(
    prompt: Annotated[str, Field(description='What to draw.', min_length=1, max_length=32000)],
    ctx: Context,
    provider: Annotated[Literal['openrouter', 'llmgateway', 'requesty'] | None, Field(description='Whose API and key to use. Empty: the provider in the connection URL (/llmgateway/mcp, /requesty/mcp), else openrouter.')] = None,
    model: Annotated[str | None, Field(description='Model id; empty for the provider default. See list_models.')] = None,
    aspect_ratio: Annotated[str | None, Field(description='e.g. 1:1, 2:3, 16:9 (openrouter, llmgateway Gemini/Seedream).')] = None,
    size: Annotated[str | None, Field(description='WxH, e.g. 1024x1536 (requesty; llmgateway pixel-size models; openrouter only if listed).')] = None,
    resolution: Annotated[str | None, Field(description='openrouter: 512|768|1K|1.5K|2K|4K; llmgateway: 0.5K|1K|2K.')] = None,
    quality: Annotated[str | None, Field(description='low|medium|high|auto where the model takes it. Default low (openrouter, requesty).')] = None,
    refs: Annotated[list[str] | None, Field(description='Reference images: https URLs, this server\'s /files/ URLs, data URLs or base64.')] = None,
    width: Annotated[int | None, Field(description='Scale the result to this width, no crop.', ge=16, le=8192)] = None,
    fit: Annotated[str | None, Field(description='Crop and resize to exactly WxH, e.g. 800x800.')] = None,
    anchor: Annotated[Literal['auto', 'top', 'center', 'bottom'], Field(description='What fit keeps when trimming height.')] = 'auto',
    n: Annotated[int, Field(description='Number of images (each is billed).', ge=1, le=4)] = 1,
    seed: Annotated[int | None, Field(description='openrouter, where the model takes it.')] = None,
    background: Annotated[str | None, Field(description='openrouter: auto|transparent|opaque where the model takes it.')] = None,
    inline: Annotated[Literal['preview', 'full', 'none'], Field(description='How the image comes back inline.')] = 'preview',
) -> list:
    if width and fit:
        raise ToolError('Give width or fit, not both')
    if fit:
        try:
            imaging.parse_size(fit)
        except imaging.ImageError as e:
            raise ToolError(str(e)) from None
    provider = _provider(ctx, provider)
    key = _key(ctx, provider)
    model = model or DEFAULT_MODELS[provider]
    if not model:
        raise ToolError(f'{provider} has no default model: pick one with list_models')
    _rate(ctx)
    ref_data = await anyio.to_thread.run_sync(_load_refs, refs)
    shape = {'size': aspect_ratio or size or resolution, 'quality': quality, 'refs': len(ref_data)}

    def call():
        if provider == 'openrouter':
            return providers.openrouter_generate(key, model=model, prompt=prompt, quality=quality or 'low',
                                                 aspect_ratio=aspect_ratio, resolution=resolution, size=size, n=n,
                                                 background=background, seed=seed, refs=ref_data)
        if provider == 'llmgateway':
            return providers.llmgateway_generate(key, model=model, prompt=prompt, size=size, aspect_ratio=aspect_ratio,
                                                 resolution=resolution, quality=quality, n=n, refs=ref_data)
        return providers.requesty_generate(key, model=model, prompt=prompt, size=size or '1024x1024',
                                           quality=quality or 'low', n=n, refs=ref_data)

    try:
        result = await anyio.to_thread.run_sync(call)
    except providers.ProviderError as e:
        store.log_rejection(provider, e.endpoint, model, shape, e.kind, str(e), e.status, prompt)
        raise ToolError(str(e)) from None

    files, content = [], []
    for raw in result['images']:
        try:
            webp, full, (w, h) = imaging.to_webp(raw, fit_size=fit, width=width, anchor=anchor)
        except imaging.ImageError as e:
            raise ToolError(str(e)) from None
        _, url = store.save_file(webp, 'webp')
        entry = {'url': url, 'width': w, 'height': h, 'bytes': len(webp)}
        if full:
            entry['full_url'] = store.save_file(full, 'webp')[1]
        files.append(entry)
        if inline == 'full':
            content.append(ImageContent(type='image', data=_b64(webp), mime_type='image/webp'))
        elif inline == 'preview':
            content.append(ImageContent(type='image', data=_b64(imaging.preview(webp)), mime_type='image/webp'))
    cost = result['cost']
    store.log_cost(provider, model, cost, shape, len(result['images']))
    summary = {'provider': provider, 'model': model, 'images': files,
               'cost_usd': cost, 'cost_per_image_usd': round(cost / len(files), 6) if cost is not None else None,
               'expires_in': TTL_TEXT, 'download': f'curl -fsSL -o image.webp "{files[0]["url"]}"'}
    if result['notes']:
        summary['notes'] = result['notes']
    if provider == 'openrouter':
        try:
            summary['key_status'] = (await anyio.to_thread.run_sync(providers.openrouter_key_status, key))['summary']
        except Exception:
            pass
    return [_text(summary), *content]


def _b64(data: bytes) -> str:
    import base64
    return base64.b64encode(data).decode()


@mcp.tool(
    description=f"""List image models (or video models with kind='video') with prices, for one provider, plus
the costs actually seen in real runs on this server. Use it to pick the cheapest suitable model.
openrouter: every image model with its lowest output price and max reference images (first call can
take ~20 s, then cached 6 h). llmgateway: estimated price per image at the lowest tier and whether it
takes references. requesty: image models with their pricing (prefer the vendor-prefixed ids, e.g.
vertex/google/...; some bare ids in the list return 404). Needs that provider's key header.
filter narrows by id substring.
{PRICE_GUIDE}""",
    structured_output=False,
)
async def list_models(
    ctx: Context,
    provider: Annotated[Literal['openrouter', 'llmgateway', 'requesty'] | None, Field(description='Which provider. Empty: the provider in the connection URL, else openrouter.')] = None,
    filter: Annotated[str | None, Field(description='Only ids containing this text.')] = None,
    kind: Annotated[Literal['image', 'video'], Field(description='video lists OpenRouter video models (per-second prices).')] = 'image',
) -> list:
    provider = 'openrouter' if kind == 'video' else _provider(ctx, provider)
    key = _key(ctx, provider)

    def call():
        if kind == 'video':
            return providers.video_models(key)
        if provider == 'openrouter':
            return [{k: v for k, v in r.items() if k != 'sort'} for r in providers.openrouter_prices(key)]
        if provider == 'llmgateway':
            return providers.llmgateway_models(key)
        return providers.requesty_models(key)

    try:
        rows = await anyio.to_thread.run_sync(call)
    except providers.ProviderError as e:
        raise ToolError(str(e)) from None
    if filter:
        rows = [r for r in rows if filter.lower() in r['id'].lower()]
    prefix = f'{provider}-video/' if kind == 'video' else f'{provider}/'
    seen = {k[len(prefix):]: v for k, v in store.read_costs().items() if k.startswith(prefix)}
    return [_text({'provider': provider, 'kind': kind, 'default_model': None if kind == 'video' else DEFAULT_MODELS[provider],
                   'models': rows, 'observed_costs': seen,
                   'note': 'Reference images can add a per-image input charge; the cost in each reply is what was billed.'})]


@mcp.tool(
    description="""Spending status of the keys this connection sends. OpenRouter: limit, what is left, reset
period and today's use (HTTP 402 on generation means the limit is reached). LLM Gateway and Requesty
have no limit endpoint here: only whether a key was sent; check their dashboards. Check this before a
batch, and tell the user the count x price first. Never switch to another key to carry on after a limit
is reached unless the user asks. Keys are never returned.""",
    structured_output=False,
)
async def key_status(ctx: Context) -> list:
    out = {}
    for provider in KEY_HEADERS:
        v = _sent_key(_headers(ctx), provider)
        if not v:
            out[provider] = 'no key sent'
            continue
        if provider == 'openrouter':
            try:
                out[provider] = await anyio.to_thread.run_sync(providers.openrouter_key_status, _key(ctx, provider))
            except providers.ProviderError as e:
                out[provider] = f'key sent, but the status check failed: {e}'
        else:
            out[provider] = 'key sent; no spending-limit endpoint, see the provider dashboard'
    return [_text(out)]


@mcp.tool(
    description="""Recent failed generation requests on this server (all callers; no prompts are kept, only a
hash): provider, endpoint, model, shape, kind (http, connection, empty = content filter on LLM Gateway,
reply), status and the provider's message. Read it before retrying a refused prompt: the same model behind
two providers can have different moderation.""",
    structured_output=False,
)
async def recent_rejections(
    model: Annotated[str | None, Field(description='Only entries whose model contains this text.')] = None,
    limit: Annotated[int, Field(ge=1, le=200)] = 20,
) -> list:
    return [_text(store.read_rejections(model, limit))]


# ---------------------------------------------------------------- video
async def _wait_video(key: str, job_id: str, wait_seconds: int, model: str | None, shape: dict, prompt: str) -> dict:
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            job = await anyio.to_thread.run_sync(providers.video_job, key, job_id)
        except providers.ProviderError as e:
            raise ToolError(str(e)) from None
        status = job.get('status')
        if status == 'completed':
            data = await anyio.to_thread.run_sync(providers.video_content, key, job_id)
            _, url = store.save_file(data, 'mp4')
            cost = (job.get('usage') or {}).get('cost')
            store.log_cost('openrouter-video', model or job.get('model') or 'unknown', cost, shape)
            return {'status': 'completed', 'job_id': job_id, 'url': url, 'bytes': len(data), 'cost_usd': cost,
                    'expires_in': TTL_TEXT, 'download': f'curl -fsSL -o clip.mp4 "{url}"'}
        if status in ('failed', 'cancelled', 'expired'):
            err = json.dumps(job.get('error') or job)[:400]
            store.log_rejection('openrouter-video', f'{providers.OPENROUTER}/videos', model or job.get('model'),
                                shape, 'reply', err, None, prompt)
            raise ToolError(f'Job {job_id} ended as {status}: {err}')
        if time.monotonic() >= deadline:
            return {'status': status, 'job_id': job_id,
                    'next': 'Not finished yet. Call video_status with this job_id later; it does not pay again.'}
        await asyncio.sleep(8)


@mcp.tool(
    description="""Generate a short MP4 clip through OpenRouter video (caller's X-OpenRouter-Key). Every submit
spends credit, priced per second by resolution and audio (list_models kind='video'). Default to the cheapest
suitable model at its lowest resolution, shortest duration, no audio. The job is asynchronous: the tool waits
up to wait_seconds; if unfinished it returns job_id, then call video_status (never resubmit: that pays again).

Inputs: prompt; first_frame / last_frame (image becomes the opening/closing frame; models list which they
take); refs (reference images that guide but are not frames; HeyGen Video 1 takes them, Seedance 2.0 mini
refused photoreal faces, Grok Imagine Video 1.5 lite and Veo 3.1 Lite don't support them); ref_videos (HTTPS
URLs only). Images are converted to PNG (models take JPEG/PNG only); give them as https URLs, this server's
/files/ URLs, data URLs or base64. duration, resolution and aspect_ratio must be among the model's lists.
Tested (2026-11): tbc/mischief-1 $0.015 for 3 s 480p (16:9, first frame only); x-ai/grok-imagine-video-1.5-lite
$0.09 for 4 s 480p (both kept a photoreal face from the frame); google/veo-3.1-lite $0.12 for 4 s 720p (refused
a photoreal still); heygen/heygen-video-1 $0.175 for 5 s 480p from portraits (needs audio=true).""",
    structured_output=False,
)
async def generate_video(
    model: Annotated[str, Field(description='Video model id from list_models kind=video.')],
    ctx: Context,
    prompt: Annotated[str | None, Field(description='Required unless first_frame is given.')] = None,
    duration: Annotated[int | None, Field(description='Seconds, from the model\'s list.')] = None,
    resolution: Annotated[str | None, Field(description='e.g. 480p, 720p.')] = None,
    aspect_ratio: Annotated[str | None, Field(description='e.g. 16:9, 9:16.')] = None,
    audio: Annotated[bool, Field(description='Generated audio (costs more).')] = False,
    first_frame: Annotated[str | None, Field(description='Opening frame image.')] = None,
    last_frame: Annotated[str | None, Field(description='Closing frame image.')] = None,
    refs: Annotated[list[str] | None, Field(description='Reference images (not frames).')] = None,
    ref_videos: Annotated[list[str] | None, Field(description='Reference video HTTPS URLs.')] = None,
    seed: Annotated[int | None, Field()] = None,
    wait_seconds: Annotated[int, Field(description='How long to wait before returning the job id.', ge=0, le=600)] = 240,
) -> list:
    if not prompt and not first_frame:
        raise ToolError('prompt is required unless first_frame is given')
    key = _key(ctx, 'openrouter')
    _rate(ctx)

    def frame(src):
        data, mime = imaging.to_png_or_jpeg(_load_refs([src])[0][0])
        return providers.data_url(data, mime)

    payload = {'model': model, 'generate_audio': audio}
    for name, value in (('prompt', prompt), ('duration', duration), ('resolution', resolution),
                        ('aspect_ratio', aspect_ratio), ('seed', seed)):
        if value is not None:
            payload[name] = value
    frames = []
    for kind, src in (('first_frame', first_frame), ('last_frame', last_frame)):
        if src:
            frames.append({'type': 'image_url', 'image_url': {'url': await anyio.to_thread.run_sync(frame, src)},
                           'frame_type': kind})
    if frames:
        payload['frame_images'] = frames
    inputs = [{'type': 'image_url', 'image_url': {'url': await anyio.to_thread.run_sync(frame, r)}} for r in refs or []]
    for v in ref_videos or []:
        if not v.startswith('https://'):
            raise ToolError('ref_videos must be HTTPS URLs')
        inputs.append({'type': 'video_url', 'video_url': {'url': v}})
    if inputs:
        payload['input_references'] = inputs
    shape = {'size': f'{resolution or ""} {aspect_ratio or ""} {duration or ""}s'.strip(), 'quality': None,
             'refs': len(frames) + len(inputs)}
    try:
        job = await anyio.to_thread.run_sync(providers.video_submit, key, payload)
    except providers.ProviderError as e:
        store.log_rejection('openrouter-video', e.endpoint, model, shape, e.kind, str(e), e.status, prompt or '')
        raise ToolError(str(e)) from None
    result = await _wait_video(key, job['id'], wait_seconds, model, shape, prompt or '')
    return [_text(result)]


@mcp.tool(
    description="""Check or finish an OpenRouter video job started by generate_video (caller's X-OpenRouter-Key).
Waits up to wait_seconds; when the job is completed the MP4 is downloaded and returned as a URL. Does not
pay again.""",
    structured_output=False,
)
async def video_status(
    job_id: Annotated[str, Field(description='The job_id from generate_video.')],
    ctx: Context,
    wait_seconds: Annotated[int, Field(ge=0, le=600)] = 120,
) -> list:
    key = _key(ctx, 'openrouter')
    return [_text(await _wait_video(key, job_id, wait_seconds, None, {'size': None, 'quality': None, 'refs': 0}, ''))]


# ---------------------------------------------------------------- HTTP routes
@mcp.custom_route('/health', methods=['GET'])
async def health(request: Request) -> Response:
    return JSONResponse({'status': 'ok', 'version': VERSION})


@mcp.custom_route('/files/{name}', methods=['GET'])
async def files(request: Request) -> Response:
    p = store.file_path(request.path_params['name'])
    if not p:
        return JSONResponse({'error': 'not found or expired'}, status_code=404)
    ext = p.rsplit('.', 1)[-1]
    media = {'webp': 'image/webp', 'mp4': 'video/mp4', 'png': 'image/png', 'jpg': 'image/jpeg'}[ext]
    return FileResponse(p, media_type=media, headers={'Cache-Control': 'private, max-age=86400',
                                                      'X-Content-Type-Options': 'nosniff'})


@mcp.custom_route('/upload', methods=['POST'])
async def upload(request: Request) -> Response:
    """Upload a reference image (raw bytes in the body). Needs at least one provider key header,
    which is checked against that provider, so the endpoint is not free public file hosting."""
    if not _check_token(request.headers):
        return JSONResponse({'error': 'unauthorized'}, status_code=401)
    msg = limiter.check(_client_id(request))
    if msg:
        return JSONResponse({'error': msg}, status_code=429)
    sent = {p: _sent_key(request.headers, p) for p in KEY_HEADERS}
    sent = {p: v for p, v in sent.items() if v}
    if not sent:
        return JSONResponse({'error': 'send a provider key header (X-OpenRouter-Key, X-LLMGateway-Key or X-Requesty-Key)'},
                            status_code=401)
    if not await anyio.to_thread.run_sync(_any_key_valid, sent):
        return JSONResponse({'error': 'the key was refused by its provider'}, status_code=401)
    body = await request.body()
    if len(body) > store.MAX_REF_BYTES:
        return JSONResponse({'error': 'too large'}, status_code=413)
    try:
        imaging.open_image(body)
    except imaging.ImageError as e:
        return JSONResponse({'error': str(e)}, status_code=400)
    ext = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp'}.get(imaging.sniff_mime(body))
    if not ext:
        body, ext = imaging.encode_webp(imaging.open_image(body)), 'webp'
    _, url = store.save_file(body, ext)
    return JSONResponse({'url': url, 'expires_in': TTL_TEXT})


_valid_keys: dict[str, float] = {}


def _any_key_valid(sent: dict[str, str]) -> bool:
    import hashlib
    now = time.time()
    for provider, key in sent.items():
        h = hashlib.sha256(f'{provider}:{key}'.encode()).hexdigest()
        if _valid_keys.get(h, 0) > now:
            return True
        try:
            if provider == 'openrouter':
                providers.openrouter_key_status(key)
            elif provider == 'requesty':
                providers.requesty_models(key)
            else:
                providers._json(f'{providers.LLMGATEWAY}/models', key, timeout=30)
        except providers.ProviderError:
            continue
        _valid_keys[h] = now + 3600
        return True
    return False


# ---------------------------------------------------------------- app
def build_app():
    hosts = ['127.0.0.1:*', 'localhost:*']
    origins = ['http://127.0.0.1:*', 'http://localhost:*']
    if PUBLIC_HOST:
        hosts.append(PUBLIC_HOST)
        origins.append(f'https://{PUBLIC_HOST}')
    app = mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        host='0.0.0.0',
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                                     allowed_hosts=hosts, allowed_origins=origins),
    )

    inner = app

    async def guarded(scope, receive, send):
        # /<provider>/mcp and /<provider>/upload are /mcp and /upload with X-API-Key meaning that
        # provider's key. The marker header is always stripped from what the client sent.
        if scope['type'] == 'http':
            headers = [(k, v) for k, v in scope['headers'] if k.decode().lower() != PATH_PROVIDER_HEADER]
            parts = scope['path'].split('/', 2)
            if len(parts) == 3 and parts[1] in KEY_HEADERS and parts[2].split('/')[0] in ('mcp', 'upload'):
                headers.append((PATH_PROVIDER_HEADER.encode(), parts[1].encode()))
                scope = {**scope, 'path': '/' + parts[2], 'raw_path': ('/' + parts[2]).encode()}
            scope = {**scope, 'headers': headers}
        # Optional shared token for the MCP endpoint (SERVER_TOKEN); /health and /files stay open.
        if scope['type'] == 'http' and SERVER_TOKEN and scope['path'].startswith(('/mcp', '/upload')):
            headers = {k.decode().lower(): v.decode() for k, v in scope['headers']}
            if not _check_token(headers):
                await JSONResponse({'error': 'unauthorized'}, status_code=401)(scope, receive, send)
                return
        await inner(scope, receive, send)

    return guarded


async def _janitor():
    while True:
        await anyio.to_thread.run_sync(store.cleanup)
        await asyncio.sleep(3600)


def main():
    import uvicorn
    store.ensure_dirs()
    app = build_app()

    async def run():
        config = uvicorn.Config(app, host=os.environ.get('HOST', '0.0.0.0'), port=int(os.environ.get('PORT', '8000')),
                                proxy_headers=False, server_header=False, log_level='info')
        server = uvicorn.Server(config)
        async with anyio.create_task_group() as tg:
            tg.start_soon(_janitor)
            await server.serve()
            tg.cancel_scope.cancel()

    anyio.run(run)


if __name__ == '__main__':
    main()
