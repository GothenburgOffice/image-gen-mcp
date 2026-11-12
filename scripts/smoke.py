"""End-to-end check of a running image-gen-mcp server, as an MCP client.

  python scripts/smoke.py https://imagemcp.example/mcp            # list tools, key_status (free)
  python scripts/smoke.py https://imagemcp.example/mcp --generate # also one cheap image (spends ~$0.002-0.02)

Provider keys are read from OPENROUTER_API_KEY, LLMGATEWAY_API_KEY and REQUESTY_API_KEY and sent as
headers, exactly as Claude Code sends them. Optional --provider/--model pick what --generate uses.
"""
import argparse
import asyncio
import base64
import json
import os
import sys

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

HEADERS = {'X-OpenRouter-Key': 'OPENROUTER_API_KEY', 'X-LLMGateway-Key': 'LLMGATEWAY_API_KEY',
           'X-Requesty-Key': 'REQUESTY_API_KEY'}


def show(result):
    for c in result.content:
        if c.type == 'text':
            print(c.text)
        elif c.type == 'image':
            print(f'[inline image {c.mime_type}, {len(base64.b64decode(c.data))} bytes]')
    if result.is_error:
        sys.exit('tool returned an error')


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('url')
    ap.add_argument('--generate', action='store_true')
    ap.add_argument('--provider', default='openrouter')
    ap.add_argument('--model', default='openai/gpt-image-1-mini')
    ap.add_argument('--prompt', default='A flat vector icon of a red paper boat on a pale blue background, minimal')
    a = ap.parse_args()
    headers = {h: os.environ[v] for h, v in HEADERS.items() if os.environ.get(v)}
    if os.environ.get('IMAGE_MCP_TOKEN'):
        headers['Authorization'] = f'Bearer {os.environ["IMAGE_MCP_TOKEN"]}'
    async with create_mcp_http_client(headers=headers) as http, \
            streamable_http_client(a.url, http_client=http) as (read, write), \
            ClientSession(read, write) as s:
        await s.initialize()
        tools = await s.list_tools()
        print('tools:', ', '.join(t.name for t in tools.tools))
        show(await s.call_tool('key_status', {}))
        if a.generate:
            args = {'prompt': a.prompt, 'provider': a.provider, 'model': a.model, 'aspect_ratio': '1:1',
                    'resolution': '512' if a.provider == 'openrouter' else None, 'quality': 'low', 'width': 256}
            show(await s.call_tool('generate_image', {k: v for k, v in args.items() if v is not None}))


if __name__ == '__main__':
    asyncio.run(main())
