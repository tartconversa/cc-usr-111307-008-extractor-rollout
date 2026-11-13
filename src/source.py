"""供应方协议演练服务，只监听回环地址。"""
from __future__ import annotations
import argparse
import base64
import copy
import gzip
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import socket
import threading
from urllib.parse import parse_qs, urlsplit
from xml.sax.saxutils import escape
DATA = json.loads((Path(__file__).resolve().parents[1] / 'fixtures/source.json').read_text(encoding='utf-8'))

class Source:

    def __init__(self):
        self.lock = threading.RLock()
        self.state = {'generation': 1, 'requests': []}

    def route(self, method, target, headers, body):
        with self.lock:
            parsed = urlsplit(target)
            path, query = (parsed.path, parse_qs(parsed.query, keep_blank_values=True))
            if path == '/__state':
                return (200, {}, json.dumps(self.state, ensure_ascii=False).encode())
            if path == '/__control' and method == 'POST':
                self.state.update(json.loads(body))
                return (200, {}, b'{}')
            self.state['requests'].append({'method': method, 'target': target, 'headers': dict(headers), 'body': body.decode('utf-8', 'replace')})
            kind = DATA['kind']
            name = path.strip('/') or DATA['entry']
            if name in DATA['pages']:
                return (200, {'Content-Type': 'text/html; charset=utf-8'}, DATA['pages'][name].encode())
            return (404, {}, b'not found')

    @staticmethod
    def json(value, status=200):
        return (status, {'Content-Type': 'application/json; charset=utf-8'}, json.dumps(value, ensure_ascii=False).encode())

def make_server(port=0):
    source = Source()

    class Handler(BaseHTTPRequestHandler):

        def log_message(self, *args):
            pass

        def do_GET(self):
            self.respond()

        def do_POST(self):
            self.respond()

        def respond(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            try:
                status, headers, payload = source.route(self.command, self.path, self.headers, body)
            except (ValueError, KeyError, IndexError) as error:
                status, headers, payload = (400, {}, str(error).encode())
            if status is None:
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            cut = headers.pop('X-Source-Cut', source.state.get('cut_after'))
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, str(value))
            chunked = headers.get('Transfer-Encoding') == 'chunked'
            if not chunked:
                self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            if chunked:
                for at in range(0, len(payload), 7):
                    chunk = payload[at:at + 7]
                    self.wfile.write(('%x\r\n' % len(chunk)).encode() + chunk + b'\r\n')
                self.wfile.write(b'0\r\nX-Source-End: yes\r\n\r\n')
            else:
                self.wfile.write(payload if cut is None else payload[:int(cut)])
            self.wfile.flush()
            self.close_connection = True
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.source = source
    server.daemon_threads = True
    return server
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='运行本机供应方协议服务')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    with make_server(args.port) as server:
        print('http://127.0.0.1:%d' % server.server_port, flush=True)
        server.serve_forever()
