# image-gen-mcp

A remote MCP server (streamable HTTP) for image, short-video and speech (text-to-speech) generation through
[OpenRouter](https://openrouter.ai), [LLM Gateway](https://llmgateway.io) and
[Requesty](https://requesty.ai). It works in every Claude surface (the Desktop app's Chat and Code tabs, claude.ai and Claude
Code cloud sessions) through custom connectors, and with any other MCP client.

Endpoints, one per provider:

| URL | Provider |
|---|---|
| `https://imagemcp.gothenburgoffice.se/openrouter/mcp` | OpenRouter: images, video, speech and spending status |
| `https://imagemcp.gothenburgoffice.se/llmgateway/mcp` | LLM Gateway: images and speech |
| `https://imagemcp.gothenburgoffice.se/requesty/mcp` | Requesty: images and speech |

## Bring your own key

The server stores **no provider keys**. Each connection sends its provider's key in the
standard header `X-API-Key: <key>`, and each call is billed to that key. Setup is under
[Connect a client](#connect-a-client).

The server forwards a key to its own provider and does nothing else with it: keys are not
logged, not written to disk and never returned. The server is open to anyone, but it can only
spend the caller's own credit. It limits paid calls per client IP (20 a minute and 500 a day by
default). Set a spending limit on your keys at the provider: the server reports HTTP 402 as
"limit reached".

<details>
<summary>Alternative: one connection with several keys</summary>

A client that may use custom header names, such as the `claude` CLI, can send several keys on
the plain `/mcp` URL: `X-OpenRouter-Key`, `X-LLMGateway-Key` and `X-Requesty-Key`. The tools
then take a `provider` argument. Claude's connector dialog needs Anthropic's approval for custom
header names, so connectors use `X-API-Key`.

</details>

## Tools

| Tool | What it does |
|---|---|
| `generate_image` | prompt → WebP, with an inline preview and an HTTPS URL to the full file. Options: provider, model, aspect ratio, size, resolution, quality, reference images, `width` (scale) or `fit WxH` (crop, with the uncropped original returned too), n |
| `list_models` | Image models (or `kind=video`, `kind=speech`) for a provider with prices, plus the costs this server has seen in real runs |
| `key_status` | Which keys the connection sends; for OpenRouter, the limit, what is left and today's use |
| `recent_rejections` | Recent refused or failed requests (no prompts kept), useful before retrying a refused prompt |
| `generate_video` / `video_status` | OpenRouter video jobs: submit, wait, return an MP4 URL; resume a job without paying again |
| `generate_speech` | Text to speech on any of the three providers (`/audio/speech`): text → an MP3/WAV URL. Options: provider, model, voice, format, speed, instructions |

The tool descriptions carry the usage rules and known prices: the cheapest suitable model by
default, a price check before a batch, and stopping at a spending limit. A client needs nothing
else to use them.

The default model is `bytedance-seed/seedream-5-0-flash` on OpenRouter ($0.018 an image, up
to 14 references). For a better result, use `gemini-nano-banana-2.1` on LLM Gateway (about
$0.04–0.06).

**Results.** Each image comes back inline as a preview, with the longest side at 768 px. You
can pass `inline: "full"` or `"none"` instead. The full WebP is at
`https://<host>/files/<random>.webp` and is kept for 7 days. Download it to keep it:
`curl -fsSL -o out.webp <url>`.

**Speech.** `generate_speech` defaults to `mistralai/voxtral-mini-tts-2603` on OpenRouter (MP3
only), `gemini-2.5-flash-preview-tts` on LLM Gateway (WAV; ElevenLabs models too) and
`openai/gpt-4o-mini-tts` on Requesty (OpenAI voices only). Only LLM Gateway reports the cost of a
speech call; for the others, `list_models kind=speech` shows the prices. Override the defaults with
`DEFAULT_<PROVIDER>_SPEECH_MODEL`.

**Reference images.** The server cannot read your local files. Give each reference as an https
URL, a `/files/` URL from an earlier result, a data URL or base64. To use a local file, upload it
first:

```bash
curl -fsS -X POST --data-binary @portrait.webp -H "X-API-Key: $OPENROUTER_API_KEY" https://imagemcp.gothenburgoffice.se/openrouter/upload
```

The upload returns `{"url": ...}`; pass that URL in `refs`. An upload needs a key that its
provider accepts, so the server can't be used as free file hosting.

## Connect a client

### Recommended: custom connectors (every Claude surface)

Connectors live in your Claude account, so one setup reaches the Desktop app's **Chat** and
**Code** tabs, claude.ai and Claude Code cloud sessions. Tested with all three providers from a
cloud session (2026-11-12).

Custom header names such as `X-OpenRouter-Key` need Anthropic's approval in the connector
dialog, so connectors use the standard `X-API-Key` header and the URL says which provider the key
is for. Add one connector per provider in **Settings → Connectors → Add custom connector**:

1. URL: `https://imagemcp.gothenburgoffice.se/openrouter/mcp`, `/llmgateway/mcp` or `/requesty/mcp`.
2. Authentication: **No sign-in**.
3. Request headers: `X-API-Key` with that provider's real API key as the value. Claude stores it
   encrypted and sends it on every request; this server doesn't store it.

### Claude Code config, for the terminal (`claude`) only

Keep the keys in environment variables (`OPENROUTER_API_KEY`, `LLMGATEWAY_API_KEY`,
`REQUESTY_API_KEY`) and add the server once:

```bash
claude mcp add --transport http --scope user image-gen https://imagemcp.gothenburgoffice.se/mcp --header 'X-OpenRouter-Key: ${OPENROUTER_API_KEY}' --header 'X-LLMGateway-Key: ${LLMGATEWAY_API_KEY}' --header 'X-Requesty-Key: ${REQUESTY_API_KEY}'
```

The single quotes store the `${...}` placeholders, and the `claude` CLI fills them in from your
environment. **The Desktop app's Code tab did not fill them in** (2026-11-12): the server got the
placeholder text, and `key_status` said "no key sent". So in the Desktop app, use the connectors
above. If you use both, remove this entry (`claude mcp remove image-gen --scope user`), so
sessions don't show a second, keyless copy of the tools.

A committed `.mcp.json` with the same headers (`"X-OpenRouter-Key": "${OPENROUTER_API_KEY:-}"`)
works the same way, with the same Desktop caveat. Prefer connectors.

### Cloud sessions (Claude Code on the web)

The connectors above are available there. The environment's network access must reach
`imagemcp.gothenburgoffice.se`: either a custom allowlist or full access. Because the server
returns image URLs, a cloud session can save results into its repository with `curl`.

| Where | How |
|---|---|
| Claude Desktop app, Chat and Code tabs | Connectors |
| claude.ai chat | Connectors |
| Claude Code cloud sessions | Connectors, plus network access to the host |
| `claude` in a terminal | Connectors, or the `claude mcp add` entry above |

## Run your own instance

```bash
cp .env.example .env    # set PUBLIC_URL and PUBLIC_HOST
docker compose -f compose.images.yaml up -d
```

Put it behind an HTTPS reverse proxy. With Caddy:

```
imagemcp.example.com {
	reverse_proxy 127.0.0.1:3430 {
		header_up X-Real-IP {remote_host}
	}
}
```

If you set `TRUSTED_IP_HEADER=X-Real-IP`, make sure only the proxy can reach the app port.
Setting `SERVER_TOKEN` additionally requires `Authorization: Bearer <token>` on `/mcp` and
`/upload`.

Every push to `main` that passes CI publishes `ghcr.io/gothenburgoffice/image-gen-mcp:latest`
and `:sha-<commit>`.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest -q tests          # offline: a stub provider, no credit spent
DATA_DIR=.localdata PORT=8765 .venv/bin/python -m image_gen_mcp
.venv/bin/python scripts/smoke.py http://127.0.0.1:8765/mcp              # free: tools + key_status
.venv/bin/python scripts/smoke.py http://127.0.0.1:8765/mcp --generate   # one cheap image (~$0.002–0.02)
```

## Origin and license

The provider code is ported from image-generation scripts in the author's private repository,
where it was written and tested against the live provider APIs. The endpoints, request shapes
and provider quirks are unchanged; they are documented at the top of
`image_gen_mcp/providers.py`.

MIT License, © 2026 Gothenburg Office: see [LICENSE](LICENSE).
