import argparse
import base64
import json
import mimetypes
import secrets
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from .service import ControlService

ROOT = Path(__file__).resolve().parents[2]


def make_handler(service, port):
    token = secrets.token_urlsafe(32)
    hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
    origins = {'http://' + h for h in hosts}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, data, mime='application/json; charset=utf-8', status=200, filename=None):
            if isinstance(data, (dict, list)):
                data = json.dumps(data, ensure_ascii=False).encode('utf-8')
            elif isinstance(data, str):
                data = data.encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Frame-Options', 'SAMEORIGIN')
            if filename:
                self.send_header('Content-Disposition', "inline; filename*=UTF-8''" + quote(filename))
            if mime.startswith('text/html'):
                self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(data)

        def valid_host(self):
            if self.headers.get('Host') not in hosts:
                self.send({'error': '主机不允许'}, status=403)
                return False
            return True

        def do_GET(self):
            if not self.valid_host():
                return
            url = urlsplit(self.path)
            path = url.path
            qs = parse_qs(url.query)
            try:
                if path == '/api/status':
                    result = service.status()
                    result['token'] = token
                    self.send(result)
                elif path == '/api/courses':
                    self.send(service.courses(qs.get('q', [''])[0], qs.get('term', [''])[0]))
                elif path == '/api/lectures':
                    self.send(service.lectures())
                elif path == '/api/attention':
                    self.send(service.attention())
                elif path == '/api/settings':
                    self.send(service.settings())
                elif path.startswith('/api/lecture/'):
                    self.send(service.lecture(path.rsplit('/', 1)[-1]))
                elif path == '/api/mail/settings':
                    self.send(service.mail_settings())
                elif path == '/api/mail/messages':
                    self.send(service.archive.list())
                elif path.startswith('/api/mail/message/'):
                    self.send(service.archive.message(path.rsplit('/', 1)[-1]))
                elif path.startswith('/api/mail/lecture/'):
                    from .mailbox import single_lecture_body
                    message_id,index=path[len('/api/mail/lecture/'):].split('/')
                    mail=service.archive.message(message_id)
                    body=single_lecture_body(mail['subject'],mail['body'],int(index))
                    self.send('<!doctype html><meta charset="utf-8"><style>body{font:16px/1.85 system-ui;padding:28px;color:#23312d}img{max-width:100%}pre{white-space:pre-wrap}</style>'+body,'text/html; charset=utf-8')
                elif path.startswith('/api/mail/body/'):
                    body = service.archive.message(path.rsplit('/', 1)[-1])['body']
                    self.send('<!doctype html><meta charset="utf-8"><style>body{font:16px/1.85 system-ui;padding:28px;color:#23312d}pre{white-space:pre-wrap}table{border-collapse:collapse}td,th{border:1px solid #ddd;padding:8px}img{max-width:100%}</style>' + body, 'text/html; charset=utf-8')
                elif path.startswith('/api/mail/attachment/'):
                    attachment, data = service.archive.attachment(path.rsplit('/', 1)[-1])
                    self.send(data, attachment['mime'], filename=attachment['filename'])
                else:
                    relative = 'index.html' if path == '/' else path.lstrip('/')
                    directory = ROOT / 'dashboard'
                    target = (directory / relative).resolve()
                    if not target.is_relative_to(directory.resolve()) or not target.is_file():
                        self.send({'error': '页面不存在'}, status=404)
                    else:
                        mime = mimetypes.guess_type(target.name)[0] or 'application/octet-stream'
                        self.send(target.read_bytes(), mime + ('; charset=utf-8' if mime.startswith('text/') else ''))
            except (ValueError, sqlite3.Error) as exc:
                self.send({'error': str(exc) if isinstance(exc, ValueError) else '本地数据库暂不可用，请稍后重试'}, status=400)
            except Exception:
                self.send({'error': '读取失败，请检查服务器日志'}, status=500)

        def do_POST(self):
            if not self.valid_host():
                return
            if self.headers.get('Origin') not in origins or not secrets.compare_digest(self.headers.get('X-Control-Token', ''), token):
                self.send({'error': '请从本机控制台操作'}, status=403)
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 42 * 1024 * 1024:
                    raise ValueError('请求过大或为空')
                value = json.loads(self.rfile.read(length))
                if not isinstance(value, dict):
                    raise ValueError('请求格式不正确')
                path = urlsplit(self.path).path
                if path == '/api/start':
                    result = service.start()
                elif path == '/api/stop':
                    result = service.stop()
                elif path == '/api/schedule':
                    result = service.schedule(value)
                elif path == '/api/courses':
                    result = service.subscribe(value.get('ids'))
                elif path == '/api/settings':
                    result = service.save_settings(value)
                elif path == '/api/completion/arm':
                    result = service.arm_completion()
                elif path == '/api/completion/cancel':
                    result = service.cancel_completion()
                elif path == '/api/mail/settings':
                    result = service.save_mail(value)
                elif path == '/api/mail/sync':
                    result = service.sync_mail()
                elif path == '/api/mail/upload':
                    raw = base64.b64decode(value.get('content', ''), validate=True)
                    if not raw:
                        raise ValueError('邮件文件为空')
                    imported = service.archive.import_message(raw, service.config().get('SMTP_EMAIL', ''))
                    result = {'message': '课程讲义已导入' if imported else '邮件已存在，或不属于项目发出的课程讲义'}
                else:
                    self.send({'error': '接口不存在'}, status=404)
                    return
                self.send(result)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self.send({'error': str(exc)}, status=400)
            except Exception:
                self.send({'error': '操作失败，请检查配置或服务器日志'}, status=500)

    return Handler


def run():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=18081)
    args = parser.parse_args()
    service = ControlService(ROOT)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(service, args.port))
    threading.Thread(target=service.scheduler_loop, daemon=True).start()
    print(f'iCourseNotes 本机控制台：http://127.0.0.1:{args.port}/', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        service.shutdown_event.set()
        server.server_close()
