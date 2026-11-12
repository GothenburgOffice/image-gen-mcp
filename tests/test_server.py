"""Offline end-to-end tests: a stub provider API, the real server app, a real MCP client.
Nothing here reaches a real provider or spends credit."""
import asyncio
import base64
import io
import json
import os
import socket
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def png(w=64, h=96):
    buf = io.BytesIO()
    Image.new('RGB', (w, h), (200, 30, 30)).save(buf, 'PNG')
    return buf.getvalue()


SEEN = {}


class Stub(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        SEEN['auth'] = self.headers.get('Authorization')
        if self.path == '/or/images/models':
            self.reply({'data': [{'id': 'test/cheap', 'endpoints': '/api/v1/images/models/test/cheap/endpoints',
                                  'supported_parameters': {'aspect_ratio': {'type': 'enum', 'values': ['1:1', '2:3']},
                                                           'input_references': {'type': 'range', 'min': 0, 'max': 2}}}]})
        elif self.path == '/or/key':
            if self.headers.get('Authorization') != 'Bearer or-good':
                return self.reply({'error': 'bad key'}, 401)
            self.reply({'data': {'limit': 20, 'limit_remaining': 12.5, 'limit_reset': 'monthly', 'usage_daily': 0.1}})
        else:
            self.reply({'error': 'nope'}, 404)

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        SEEN['payload'] = payload
        if self.path == '/or/images':
            if self.headers.get('Authorization') == 'Bearer or-broke':
                return self.reply({'error': {'message': 'Insufficient credits'}}, 402)
            return self.reply({'data': [{'b64_json': base64.b64encode(png()).decode(), 'media_type': 'image/png'}],
                               'usage': {'cost': 0.018}})
        if self.path == '/lg/images/generations':
            return self.reply({'data': []})
        self.reply({'error': 'nope'}, 404)


stub_port, app_port = free_port(), free_port()
threading.Thread(target=ThreadingHTTPServer(('127.0.0.1', stub_port), Stub).serve_forever, daemon=True).start()
os.environ.update({
    'OPENROUTER_BASE_URL': f'http://127.0.0.1:{stub_port}/or',
    'LLMGATEWAY_BASE_URL': f'http://127.0.0.1:{stub_port}/lg',
    'DATA_DIR': tempfile.mkdtemp(),
    'PUBLIC_URL': f'http://127.0.0.1:{app_port}',
})

import uvicorn  # noqa: E402
from mcp.client.session import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402
from mcp.shared._httpx_utils import create_mcp_http_client  # noqa: E402

from image_gen_mcp import server, store  # noqa: E402

_srv = uvicorn.Server(uvicorn.Config(server.build_app(), host='127.0.0.1', port=app_port, log_level='warning'))
threading.Thread(target=_srv.run, daemon=True).start()
for _ in range(50):
    try:
        urllib.request.urlopen(f'http://127.0.0.1:{app_port}/health', timeout=1)
        break
    except Exception:
        time.sleep(0.1)

URL = f'http://127.0.0.1:{app_port}/mcp'


async def call(tool, args, headers):
    async with create_mcp_http_client(headers=headers) as http, \
            streamable_http_client(URL, http_client=http) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        return await s.call_tool(tool, args)


def run(tool, args, headers=None):
    return asyncio.run(call(tool, args, headers or {}))


def test_generate_returns_preview_url_and_cost():
    res = run('generate_image', {'prompt': 'a red square', 'model': 'test/cheap', 'aspect_ratio': '2:3',
                                 'resolution': '1K', 'fit': '32x32'}, {'X-OpenRouter-Key': 'or-good'})
    assert not res.is_error, res.content[0].text
    summary = json.loads(res.content[0].text)
    assert summary['cost_usd'] == 0.018
    assert summary['images'][0]['width'] == 32 and summary['images'][0]['height'] == 32
    assert 'full_url' in summary['images'][0]
    assert res.content[1].type == 'image' and res.content[1].mime_type == 'image/webp'
    # resolution is not listed by the model, so it is dropped and reported; quality default 'low' too
    assert 'resolution' not in SEEN['payload'] and 'quality' not in SEEN['payload']
    assert any('resolution' in n for n in summary['notes'])
    assert SEEN['payload']['aspect_ratio'] == '2:3'
    # the file is served
    with urllib.request.urlopen(summary['images'][0]['url']) as resp:
        assert resp.headers['Content-Type'] == 'image/webp'
    # the key never appears in the reply
    assert 'or-good' not in res.content[0].text
    assert store.read_costs()['openrouter/test/cheap']['last_cost'] == 0.018


def test_missing_key_is_a_clear_error():
    res = run('generate_image', {'prompt': 'x'})
    assert res.is_error and 'x-openrouter-key' in res.content[0].text


def test_402_is_reported_and_logged_without_prompt():
    res = run('generate_image', {'prompt': 'secret prompt text', 'model': 'test/cheap'}, {'X-OpenRouter-Key': 'or-broke'})
    assert res.is_error and '402' in res.content[0].text and 'stop and tell the user' in res.content[0].text
    rows = store.read_rejections('test/cheap')
    assert rows[-1]['status'] == 402
    assert 'secret prompt text' not in json.dumps(rows)


def test_content_filter_empty_reply():
    res = run('generate_image', {'prompt': 'x', 'provider': 'llmgateway', 'model': 'm'}, {'X-LLMGateway-Key': 'k'})
    assert res.is_error and 'content filter' in res.content[0].text


def test_key_status_never_echoes_keys():
    res = run('key_status', {}, {'X-OpenRouter-Key': 'or-good', 'X-Requesty-Key': 'rq-secret'})
    text = res.content[0].text
    assert '12.50 of $20.00' in text and 'rq-secret' not in text and 'or-good' not in text
    assert json.loads(text)['llmgateway'] == 'no key sent'


def test_unexpanded_placeholder_counts_as_missing():
    res = run('key_status', {}, {'X-OpenRouter-Key': '${OPENROUTER_API_KEY}'})
    assert json.loads(res.content[0].text)['openrouter'] == 'no key sent'


def test_upload_needs_a_valid_key_and_feeds_refs():
    base = f'http://127.0.0.1:{app_port}/upload'
    req = urllib.request.Request(base, data=png(), method='POST')
    try:
        urllib.request.urlopen(req)
        raise AssertionError('upload without key accepted')
    except urllib.error.HTTPError as e:
        assert e.code == 401
    req = urllib.request.Request(base, data=png(), method='POST', headers={'X-OpenRouter-Key': 'or-good'})
    url = json.loads(urllib.request.urlopen(req).read())['url']
    res = run('generate_image', {'prompt': 'same', 'model': 'test/cheap', 'refs': [url]}, {'X-OpenRouter-Key': 'or-good'})
    assert not res.is_error, res.content[0].text
    assert SEEN['payload']['input_references'][0]['image_url']['url'].startswith('data:image/png;base64,')


def test_private_reference_urls_are_refused():
    res = run('generate_image', {'prompt': 'x', 'model': 'test/cheap', 'refs': ['https://127.0.0.1/a.png']},
              {'X-OpenRouter-Key': 'or-good'})
    assert res.is_error and 'not a public address' in res.content[0].text


def test_file_names_are_validated():
    try:
        urllib.request.urlopen(f'http://127.0.0.1:{app_port}/files/..%2Fcosts.json')
        raise AssertionError('traversal served')
    except urllib.error.HTTPError as e:
        assert e.code == 404


async def call_at(path, tool, args, headers):
    url = f'http://127.0.0.1:{app_port}{path}'
    async with create_mcp_http_client(headers=headers) as http, \
            streamable_http_client(url, http_client=http) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        return await s.call_tool(tool, args)


def test_x_api_key_on_plain_mcp_is_openrouter():
    res = run('key_status', {}, {'X-API-Key': 'or-good'})
    out = json.loads(res.content[0].text)
    assert '12.50 of $20.00' in out['openrouter']['summary'] and out['llmgateway'] == 'no key sent'


def test_x_api_key_follows_the_path_provider():
    res = asyncio.run(call_at('/llmgateway/mcp', 'key_status', {}, {'X-API-Key': 'lg-key'}))
    out = json.loads(res.content[0].text)
    assert out['openrouter'] == 'no key sent' and out['llmgateway'].startswith('key sent')
    # with no provider argument, tools use the connection's provider
    res = asyncio.run(call_at('/llmgateway/mcp', 'generate_image', {'prompt': 'x', 'model': 'm'}, {'X-API-Key': 'lg-key'}))
    assert res.is_error and 'content filter' in res.content[0].text


def test_clients_cannot_forge_the_path_marker():
    res = run('key_status', {}, {'X-API-Key': 'k', 'X-Imagegen-Path-Provider': 'requesty'})
    out = json.loads(res.content[0].text)
    assert out['requesty'] == 'no key sent'


def test_upload_on_a_provider_path_with_x_api_key():
    req = urllib.request.Request(f'http://127.0.0.1:{app_port}/openrouter/upload', data=png(), method='POST',
                                 headers={'X-API-Key': 'or-good'})
    assert json.loads(urllib.request.urlopen(req).read())['url'].startswith(f'http://127.0.0.1:{app_port}/files/')
