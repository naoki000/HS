# -*- coding: utf-8 -*-
"""最新フレームを覗くための HTTP サーバ。

標準ライブラリだけで書く。Pythonista のメインスレッドは塞がない。

    GET /            確認用 HTML
    GET /frame.jpg   最新 JPEG（無ければ 503）
    GET /frame       /frame.jpg と同じ。既存の start.py / arena_judge.py が
                     配信元に対して叩くパスがこれなので、そのまま繋げられる
    GET /status      JSON
    GET /timeline    バックグラウンド検証の記録（JSON）
    GET /start       キャプチャ開始
    GET /stop        キャプチャ停止

認証は無い。信頼できるネットワークでだけ使うこと。
"""

import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import urlparse

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web')


class _Threaded(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def lan_ips():
    """同じネットワークの端末から届く IPv4 を拾う。"""
    found = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(('8.8.8.8', 80))
            found.append(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if ip not in found:
                found.append(ip)
    except OSError:
        pass
    return [ip for ip in found if not ip.startswith('127.')]


def make_handler(capture, probe, log):

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.0'
        server_version = 'PythonistaCapturePoC'

        def log_message(self, fmt, *args):
            pass

        def address_string(self):
            # 既定実装は逆引きを引くことがあり、1リクエストごとに待たされる
            return self.client_address[0]

        # ---------------------------------------------------------- 返す

        def _send(self, body, ctype, status=200):
            if isinstance(body, str):
                body = body.encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _json(self, obj, status=200):
            self._send(json.dumps(obj, ensure_ascii=False),
                       'application/json; charset=utf-8', status)

        # ----------------------------------------------------------- GET

        def do_GET(self):
            path = urlparse(self.path).path
            try:
                if path == '/':
                    return self._index()
                if path in ('/frame.jpg', '/frame'):
                    return self._frame()
                if path == '/status':
                    return self._json(self._status())
                if path == '/timeline':
                    return self._json(probe.snapshot() if probe else {})
                if path == '/start':
                    # ObjC をこのスレッドで触ると segfault する。
                    # 依頼だけ置いて、スクリプトスレッドの pump() にやらせる。
                    capture.request_start()
                    return self._json({'ok': True, 'action': 'start',
                                       'queued': True,
                                       'status': self._status()})
                if path == '/stop':
                    capture.request_stop()
                    return self._json({'ok': True, 'action': 'stop',
                                       'queued': True,
                                       'status': self._status()})
                return self._json({'ok': False, 'error': 'not found'}, 404)
            except Exception as e:      # noqa: BLE001 - サーバは落とさない
                log('[http] %s: %s' % (type(e).__name__, e))
                return self._json({'ok': False,
                                   'error': '%s: %s' % (type(e).__name__, e)}, 500)

        def _status(self):
            st = capture.status()
            st['now'] = time.time()
            if st['last_frame_time']:
                st['frame_age'] = round(st['now'] - st['last_frame_time'], 2)
            st['availability'] = capture.availability()
            if probe:
                st['probe_tick'] = probe.tick
                st['app_state'] = probe.snapshot()['app_state']
            return st

        def _frame(self):
            jpeg = capture.latest_jpeg()
            if not jpeg:
                return self._json({'ok': False, 'error': 'まだフレームがありません'}, 503)
            self._send(jpeg, 'image/jpeg')

        def _index(self):
            try:
                with open(os.path.join(WEB_DIR, 'index.html'), 'rb') as f:
                    return self._send(f.read(), 'text/html; charset=utf-8')
            except OSError:
                return self._send('<h1>index.html がありません</h1>',
                                  'text/html; charset=utf-8', 500)

    return Handler


class CaptureServer(object):
    def __init__(self, capture, probe=None, port=8765, log=print):
        self.capture = capture
        self.probe = probe
        self.port = port
        self.log = log
        self.httpd = None
        self.thread = None

    def start(self):
        handler = make_handler(self.capture, self.probe, self.log)
        self.httpd = _Threaded(('0.0.0.0', self.port), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()
        return self.urls()

    def urls(self):
        out = ['http://127.0.0.1:%d/' % self.port]
        out += ['http://%s:%d/' % (ip, self.port) for ip in lan_ips()]
        return out

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
