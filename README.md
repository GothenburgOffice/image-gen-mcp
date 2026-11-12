# image-gen-mcp

A remote MCP server (streamable HTTP) for image and short-video generation through
[OpenRouter](https://openrouter.ai), [LLM Gateway](https://llmgateway.io) and
[Requesty](https://requesty.ai). Any MCP client, such as Claude Code (local, desktop or cloud),
can generate images through it without knowing the provider APIs.

Public instance: `https://imagemcp.gothenburgoffice.se/mcp`

## Bring your own key

The server stores **no provider keys**. Every client sends its own keys as HTTP headers, and
each call is billed to that key:

| Header | Provider |
|---|---|
| `X-OpenRouter-Key` | OpenRouter: images, video and spending status |
| `X-LLMGateway-Key` | LLM Gateway: images |
| `X-Requesty-Key` | Requesty: images |

Send only the keys you want to use. The server forwards a key to its own provider and does
nothing else with it: keys are not logged, not written to disk and never returned. The server is
open to anyone, but it can only spend the caller's own credit. It limits paid calls per client
IP (20 a minute and 500 a day by default). Set a spending limit on your keys at the provider:
the server reports HTTP 402 as "limit reached".

## Tools

| Tool | What it does |
|---|---|
| `generate_image` | prompt → WebP, with an inline preview and an HTTPS URL to the full file. Options: provider, model, aspect ratio, size, resolution, quality, reference images, `width` (scale) or `fit WxH` (crop, with the uncropped original returned too), n |
| `list_models` | Image models (or `kind=video`) for a provider with prices, plus the costs this server has seen in real runs |
| `key_status` | Which keys the connection sends; for OpenRouter, the limit, what is left and today's use |
| `recent_rejections` | Recent refused or failed requests (no prompts kept), useful before retrying a refused prompt |
| `generate_video` / `video_status` | OpenRouter video jobs: submit, wait, return an MP4 URL; resume a job without paying again |

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

**Reference images.** The server cannot read your local files. Give each reference as an https
URL, a `/files/` URL from an earlier result, a data URL or base64. To use a local file, upload it
first:

```bash
curl -fsS -X POST --data-binary @portrait.webp -H "X-OpenRouter-Key: $OPENROUTER_API_KEY" https://imagemcp.gothenburgoffice.se/upload
```

The upload returns `{"url": ...}`; pass that URL in `refs`. An upload needs a key that its
provider accepts, so the server can't be used as free file hosting.

## Connect a client

### Claude Code: local terminal or desktop

Keep the keys in environment variables (`OPENROUTER_API_KEY`, `LLMGATEWAY_API_KEY`,
`REQUESTY_API_KEY`). Then add the server once, for all your projects:

```bash
claude mcp add --transport http --scope user image-gen https://imagemcp.gothenburgoffice.se/mcp --header 'X-OpenRouter-Key: ${OPENROUTER_API_KEY}' --header 'X-LLMGateway-Key: ${LLMGATEWAY_API_KEY}' --header 'X-Requesty-Key: ${REQUESTY_API_KEY}'
```

Keep the single quotes, so the `${...}` placeholders are stored as written and Claude Code
fills them in from your environment when it connects. With double quotes (or in PowerShell
`$env:...`) the keys themselves are written into `~/.claude.json`. That also works, but you
then have to re-add the server when a key changes.

Check the connection with `claude mcp list` and ask Claude to "check image-gen key status".

### Per project (`.mcp.json`): also works in cloud sessions

Commit this file to a repository, and every session in it, local or cloud, gets the server:

```json
{
  "mcpServers": {
    "image-gen": {
      "type": "http",
      "url": "https://imagemcp.gothenburgoffice.se/mcp",
      "headers": {
        "X-OpenRouter-Key": "${OPENROUTER_API_KEY:-}",
        "X-LLMGateway-Key": "${LLMGATEWAY_API_KEY:-}",
        "X-Requesty-Key": "${REQUESTY_API_KEY:-}"
      }
    }
  }
}
```

`:-` gives an empty value when a variable is not set. The server treats an empty value as "no
key".

### Claude Code on the web (cloud sessions)

1. Commit the `.mcp.json` above to the repository the session runs in.
2. In the cloud environment's settings, add `OPENROUTER_API_KEY` (and the others you use) as
   environment variables.
3. Set the environment's network access so it can reach `imagemcp.gothenburgoffice.se`: either
   add the host to a custom allowlist or use full access. The default trusted list does not
   include it.

Because the server returns image URLs, a cloud session can save results into its repository with
`curl`.

### Clients that can't send custom headers

The claude.ai and Claude Desktop "custom connector" dialogs accept only a URL (with OAuth), not
headers. To use the server there, you would have to run your own instance with server-side keys,
which this server deliberately does not offer.

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

The provider code is ported from the image-generation scripts in DreamGen Studio
(`tools/image-gen/`). The endpoints, request shapes and provider quirks are the same; they are
documented at the top of `image_gen_mcp/providers.py`.
