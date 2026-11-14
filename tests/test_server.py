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
        elif self.path == '/or/models?output_modalities=speech':
            self.reply({'data': [
                {'id': 'google/tts-lite', 'pricing': {'prompt': '0.0000005', 'completion': '0.000006'},
                 'supported_voices': ['Aoede', 'Kore']},
                {'id': 'cheap/chars', 'pricing': {'prompt': '0.0000006', 'completion': '0'},
                 'supported_voices': ['af_bella']},
                {'id': 'google/tts-pro', 'pricing': {'prompt': '0.000001', 'completion': '0.00002'},
                 'supported_voices': ['Aoede']},
                {'id': 'odd/unit', 'pricing': {'prompt': '0', 'completion': '0.0025'}, 'supported_voices': []},
                {'id': 'odd/cheap-unit', 'pricing': {'prompt': '0', 'completion': '0.00001'}, 'supported_voices': []}]})
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
        if self.path == '/or/audio/speech':
            if payload.get('model', '').startswith('google/'):
                if payload.get('response_format') != 'pcm':
                    return self.reply({'error': {'message': 'Gemini TTS only supports response_format="pcm". Got "mp3".'}}, 400)
                return self.raw(b'\xff\xf3' + b'\x00' * 98, 'audio/pcm')  # pcm that happens to look like mp3
            return self.raw(b'ID3\x04\x00fake-mp3', 'audio/mpeg')
        if self.path == '/lg/audio/speech':
            if payload.get('voice') == 'no-sse':
                if 'stream_format' in payload:
                    return self.reply({'error': 'stream_format not supported'}, 400)
                return self.raw(b'ID3\x04\x00binary', 'audio/mpeg')
            delta = base64.b64encode(b'\x01\x00' * 100).decode()  # raw pcm, as Gemini sends
            sse = (f'event: speech.audio.delta\ndata: {{"type":"speech.audio.delta","audio":"{delta}"}}\n\n'
                   'event: speech.audio.done\ndata: {"type":"speech.audio.done","usage":{"input_tokens":7,'
                   '"output_tokens":42,"cost":0.0005}}\n\ndata: [DONE]\n\n')
            return self.raw(sse.encode(), 'text/event-stream')
        self.reply({'error': 'nope'}, 404)

    def raw(self, body, ctype):
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


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


def test_speech_openrouter_asks_for_mp3_and_serves_it():
    res = run('generate_speech', {'text': 'hello there', 'voice': 'en_paul_neutral', 'response_format': 'wav'},
              {'X-OpenRouter-Key': 'or-good'})
    assert not res.is_error, res.content[0].text
    out = json.loads(res.content[0].text)
    assert SEEN['payload'] == {'model': 'mistralai/voxtral-mini-tts-2603', 'input': 'hello there',
                               'voice': 'en_paul_neutral', 'response_format': 'mp3'}
    assert out['format'] == 'mp3' and out['cost_usd'] is None and any('mp3' in n for n in out['notes'])
    with urllib.request.urlopen(out['url']) as resp:
        assert resp.headers['Content-Type'] == 'audio/mpeg' and resp.read().startswith(b'ID3')


def test_speech_llmgateway_sse_gives_cost_and_wraps_pcm():
    res = run('generate_speech', {'text': 'hej', 'provider': 'llmgateway', 'voice': 'Kore', 'inline': 'full'},
              {'X-LLMGateway-Key': 'k'})
    assert not res.is_error, res.content[0].text
    out = json.loads(res.content[0].text)
    assert SEEN['payload']['stream_format'] == 'sse'
    assert out['cost_usd'] == 0.0005 and out['format'] == 'wav' and out['usage']['output_tokens'] == 42
    assert res.content[1].type == 'audio' and res.content[1].mime_type == 'audio/wav'
    assert base64.b64decode(res.content[1].data)[:4] == b'RIFF'
    assert store.read_costs()['llmgateway-speech/gemini-2.5-flash-preview-tts']['last_cost'] == 0.0005


def test_upload_on_a_provider_path_with_x_api_key():
    req = urllib.request.Request(f'http://127.0.0.1:{app_port}/openrouter/upload', data=png(), method='POST',
                                 headers={'X-API-Key': 'or-good'})
    assert json.loads(urllib.request.urlopen(req).read())['url'].startswith(f'http://127.0.0.1:{app_port}/files/')


def test_speech_llmgateway_falls_back_to_binary_when_sse_is_refused():
    res = run('generate_speech', {'text': 'hej', 'provider': 'llmgateway', 'voice': 'no-sse'}, {'X-LLMGateway-Key': 'k'})
    assert not res.is_error, res.content[0].text
    out = json.loads(res.content[0].text)
    assert 'stream_format' not in SEEN['payload'] and out['format'] == 'mp3' and out['cost_usd'] is None
    assert any('sse' in n for n in out['notes'])


def test_speech_requesty_length_limit():
    res = run('generate_speech', {'text': 'a' * 4097, 'provider': 'requesty'}, {'X-Requesty-Key': 'k'})
    assert res.is_error and '4096' in res.content[0].text


def test_speech_models_ranked_with_voice_filter():
    res = run('list_models', {'kind': 'speech', 'voice': 'aoede'}, {'X-OpenRouter-Key': 'or-good'})
    assert not res.is_error, res.content[0].text
    rows = json.loads(res.content[0].text)['models']
    assert [r['id'] for r in rows] == ['google/tts-lite', 'google/tts-pro']
    assert rows[0]['est_usd_per_1000_chars'] < rows[1]['est_usd_per_1000_chars']
    assert 'Aoede' in rows[0]['voices'] and '/ $6.00 per million' in rows[0]['price']
    res = run('list_models', {'kind': 'speech'}, {'X-OpenRouter-Key': 'or-good'})
    rows = json.loads(res.content[0].text)['models']
    assert rows[0]['id'] == 'cheap/chars' and {r['id'] for r in rows[-2:]} == {'odd/unit', 'odd/cheap-unit'}
    assert rows[-1]['est_usd_per_1000_chars'] is None and rows[-2]['est_usd_per_1000_chars'] is None


def test_speech_openrouter_gemini_falls_back_to_pcm_as_wav():
    res = run('generate_speech', {'text': 'hej', 'model': 'google/gemini-tts', 'voice': 'Aoede'},
              {'X-OpenRouter-Key': 'or-good'})
    assert not res.is_error, res.content[0].text
    out = json.loads(res.content[0].text)
    assert SEEN['payload']['response_format'] == 'pcm' and out['format'] == 'wav'
    with urllib.request.urlopen(out['url']) as resp:
        assert resp.headers['Content-Type'] == 'audio/wav' and resp.read()[:4] == b'RIFF'


def test_llmgateway_speech_list_keeps_voiceless_models_and_drops_realtime():
    from image_gen_mcp import providers
    catalog = {'data': [
        {'id': 'tts-novoice', 'architecture': {'output_modalities': ['audio']}, 'providers': [{}],
         'pricing': {'input_character': '15e-6'}},
        {'id': 'realtime', 'architecture': {'output_modalities': ['text', 'audio']},
         'providers': [{'supported_voices': ['alloy']}], 'pricing': {'prompt': '1e-6'}},
        {'id': 'chat', 'architecture': {'output_modalities': ['text']}, 'providers': [{}], 'pricing': {}}]}
    real = providers._json
    providers._json = lambda *a, **k: catalog
    providers._cache.pop('llmg-speech-models', None)
    try:
        rows = providers.speech_models('llmgateway', 'k')
    finally:
        providers._json = real
        providers._cache.pop('llmg-speech-models', None)
    assert [r['id'] for r in rows] == ['tts-novoice'] and rows[0]['voices'] == []
