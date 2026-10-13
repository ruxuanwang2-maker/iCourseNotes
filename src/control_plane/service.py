import json
import os
import re
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from contextlib import contextmanager

import psutil
from dotenv import dotenv_values, set_key

from .mailbox import MailArchive
from .completion import public_plan

BEIJING = timezone(timedelta(hours=8))
SETTING_FIELDS = {
    'StuId': 'private', 'UISPsw': 'secret',
    'DEEPSEEK_API_KEY': 'secret', 'DEEPSEEK_BASE_URL': 'url',
    'DASHSCOPE_API_KEY': 'secret', 'DASHSCOPE_BASE_URL': 'url',
    'GEMINI_API_KEY': 'secret', 'GEMINI_BASE_URL': 'url',
    'SMTP_EMAIL': 'email', 'SMTP_PASSWORD': 'secret', 'RECEIVER_EMAILS': 'emails',
    'IMAP_ACCOUNT': 'email', 'IMAP_PASSWORD': 'secret',
    'IMAP_HOST': 'host', 'IMAP_PORT': 'port', 'DB_ENCRYPTION_KEY': 'secret',
}


class ControlService:
    def __init__(self, root, python_path=None):
        self.root = Path(root).resolve()
        self.python_path = Path(python_path) if python_path else self.root / '.venv' / 'Scripts' / 'python.exe'
        self.directory = self.root / 'data' / 'local-control'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.state_path = self.directory / 'state.json'
        self.lock = threading.RLock()
        self.state = {'schedule': {'enabled': False, 'time': '17:07', 'last_date': ''}, 'last_run': None}
        if self.state_path.exists():
            self.state.update(json.loads(self.state_path.read_text(encoding='utf-8')))
        self.process = None
        self.log = None
        self.mail_job = {'running': False, 'imported': 0, 'scanned': 0, 'total': 0, 'message': '尚未同步'}
        self.mail_auto = True
        self._mail_next_check = 0.0
        self.archive = MailArchive(self.directory / 'mail')
        self.shutdown_event = threading.Event()
        self._handouts_next_check = 0
        self.ensure_completion_worker()

    def ensure_completion_worker(self):
        from .completion import read_plan
        plan = read_plan(self.directory)
        script = self.root / 'watch_completion.py'
        if not plan.get('enabled') or not script.is_file(): return
        for process in psutil.process_iter(['cmdline']):
            try:
                args = process.info['cmdline'] or []
                if any(Path(arg).name == 'watch_completion.py' for arg in args) and plan['id'] in args and Path(process.cwd()).resolve() == self.root: return
            except (psutil.Error, OSError): pass
        python = self.python_path.with_name('pythonw.exe') if os.name == 'nt' else self.python_path
        subprocess.Popen([str(python), str(script), plan['id']], cwd=self.root,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)

    def arm_completion(self):
        import uuid
        from .completion import write_plan
        targets = sorted({str(row['sub_id']) for row in self.lectures()})
        if not targets: raise ValueError('没有可跟踪的课次')
        if not (self.root / 'watch_completion.py').is_file(): raise ValueError('缺少自动完成程序')
        with self.lock:
            plan = {'id': uuid.uuid4().hex, 'enabled': True, 'targets': targets,
                'target_count': len(targets), 'phase': 'waiting', 'repaired': 0,
                'message': '等待本轮任务结束，自动修复失败讲义；完成后保持开机', 'countdown_at': None}
            write_plan(self.directory, plan)
            self.ensure_completion_worker()
        return {'message': f'已跟踪 {len(targets)} 个课次：失败讲义自动修复，完成后保持开机。', 'target_count': len(targets)}

    def cancel_completion(self):
        from .completion import read_plan
        plan = read_plan(self.directory)
        if plan.get('id'): (self.directory / 'completion-cancel.txt').write_text(plan['id'], encoding='utf-8')
        return {'message': '已取消自动修复'}

    def config(self):
        return dotenv_values(self.root / '.env')

    def settings(self):
        config = self.config()
        public = ('DEEPSEEK_BASE_URL','DASHSCOPE_BASE_URL','GEMINI_BASE_URL','IMAP_HOST','IMAP_PORT')
        values = {key: config.get(key, '') or '' for key in public}
        from urllib.parse import urlsplit
        for key in public:
            if SETTING_FIELDS[key] == 'url' and values[key]:
                parsed = urlsplit(values[key])
                if parsed.username or parsed.password or parsed.query or parsed.fragment: values[key] = ''
        return {'values': values, 'configured': {key: bool(config.get(key)) for key in SETTING_FIELDS}}

    def save_settings(self, value):
        from urllib.parse import urlsplit
        import shutil
        import tempfile
        fields = value.get('fields')
        if not isinstance(fields, dict) or any(key not in SETTING_FIELDS for key in fields):
            raise ValueError('设置字段不正确')
        updates = {}
        for key, content in fields.items():
            if not isinstance(content, str) or len(content) > 4096 or any(c in content for c in ('\n','\r','\x00')):
                raise ValueError('配置值格式不正确')
            if not content.strip(): continue
            kind = SETTING_FIELDS[key]
            text = content if key == 'UISPsw' else content.strip()
            if key == 'StuId' and not re.fullmatch(r'\d{1,30}', text): raise ValueError('学号应为数字')
            if kind == 'url':
                parsed = urlsplit(text)
                if parsed.scheme not in ('https','http') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                    raise ValueError('API 地址应为不含凭据和查询参数的完整 HTTP/HTTPS 地址')
                if parsed.scheme == 'http' and parsed.hostname not in ('localhost','127.0.0.1','::1'):
                    raise ValueError('远程 API 地址应使用 HTTPS')
            if kind in ('email','emails'):
                addresses = re.split(r'[,;，；\s]+', text)
                if not all(re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', address) for address in addresses) or kind == 'email' and len(addresses) != 1:
                    raise ValueError('邮箱地址格式不正确')
                text = ','.join(addresses)
            if kind == 'host' and not re.fullmatch(r'[A-Za-z0-9.-]+',text): raise ValueError('IMAP 主机格式不正确')
            if kind == 'port' and (not text.isdigit() or not 1 <= int(text) <= 65535): raise ValueError('IMAP 端口范围应为 1–65535')
            updates[key] = text
        if not updates: return {'message': '未填写更改项，现有配置已保留'}
        with self.lock:
            with tempfile.TemporaryDirectory(prefix='settings-',dir=self.directory) as folder:
                temporary = Path(folder) / '.env'
                source = self.root / '.env'
                if source.exists(): shutil.copyfile(source, temporary)
                else: temporary.touch()
                for key, text in updates.items(): set_key(str(temporary),key,text)
                temporary.replace(source)
        return {'message': '设置已保存。学号、登录密码、模型 API 及发信设置在下一次启动课程任务时生效；收信设置在下一次同步时生效。'}

    def save(self):
        temp = self.state_path.with_suffix('.tmp')
        temp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(self.state_path)

    def processes(self):
        matches = []
        for p in psutil.process_iter(['name', 'cmdline', 'ppid']):
            try:
                args = p.info['cmdline'] or []
                if p.info['name'] and p.info['name'].lower() in ('python.exe', 'python') and any(Path(arg).name == 'run_local.py' for arg in args) and Path(p.cwd()).resolve() == self.root:
                    matches.append(p)
            except (psutil.Error, OSError):
                continue
        parents = {p.ppid() for p in matches}
        return [p for p in matches if p.pid not in parents]

    def status(self):
        with self.lock:
            if time.monotonic() >= self._handouts_next_check:
                from .handouts import sync_handouts
                try: sync_handouts(self.root)
                except (OSError,sqlite3.Error,ValueError): pass
                self._handouts_next_check=time.monotonic()+60
            running = self.processes()
            if not running and self.process and self.process.poll() is None:
                try:
                    running = [psutil.Process(self.process.pid)]
                except psutil.Error:
                    pass
            if self.process and self.process.poll() is not None and self.state.get('last_run') and not self.state['last_run'].get('finished_at'):
                self.state['last_run'].update(finished_at=datetime.now(BEIJING).isoformat(), exit_code=self.process.returncode)
                self.save()
            elif not running and self.state.get('last_run') and not self.state['last_run'].get('finished_at'):
                self.state['last_run'].update(finished_at=datetime.now(BEIJING).isoformat(), exit_code=None)
                self.save()
            processes = []
            for p in running:
                try:
                    processes.append({'pid': p.pid, 'memory_mb': round(p.memory_info().rss / 1048576), 'cpu_seconds': round(sum(p.cpu_times()[:2]), 1), 'started_at': datetime.fromtimestamp(p.create_time(), BEIJING).isoformat()})
                except psutil.Error:
                    pass
            counters = {'total': 0, 'transcripts': 0, 'summaries': 0, 'emailed': 0, 'failed': 0}
            try:
                with self.db() as db:
                    row = db.execute("SELECT count(*),sum(length(trim(coalesce(transcript,'')))>0),sum(length(trim(coalesce(summary,'')))>0),sum(emailed_at IS NOT NULL),sum(error_count>0) FROM lectures WHERE deleted_at IS NULL").fetchone()
                    counters = dict(zip(counters, (v or 0 for v in row)))
            except sqlite3.Error:
                pass
            logs = self.logs()
            blocks = re.findall(r'\[Qwen\] Block (\d+)/(\d+) completed', logs)
            schedule = dict(self.state['schedule'])
            now = datetime.now(BEIJING)
            due = now.replace(hour=int(schedule['time'][:2]), minute=int(schedule['time'][3:]), second=0, microsecond=0)
            if due <= now or schedule.get('last_date') == now.date().isoformat():
                due += timedelta(days=1)
            schedule['next_at'] = due.isoformat() if schedule['enabled'] else None
            return {'running': bool(processes), 'processes': processes, 'counters': counters, 'logs': logs,
                    'blocks': {'done': int(blocks[-1][0]), 'total': int(blocks[-1][1])} if blocks and processes else None,
                    'schedule': schedule, 'last_run': self.state.get('last_run'), 'mail_job': dict(self.mail_job),
                    'now': now.isoformat(), 'log_available': bool(self.state.get('last_run')), 'mail_auto': self.mail_auto,
                    'completion': public_plan(self.directory)}

    def logs(self):
        run = self.state.get('last_run')
        if not run:
            return '现有进程可通过资源占用和数据库监视。通过本站启动的新任务会记录完整日志。'
        path = self.directory / run['log']
        if not path.exists():
            return ''
        with path.open('rb') as f:
            f.seek(max(0, path.stat().st_size - 24000))
            result = f.read().decode('utf-8', errors='replace')
        for name, value in self.config().items():
            if value and any(word in name.upper() for word in ('PASSWORD', 'PSW', 'KEY', 'STUID')):
                result = result.replace(value, '[已隐藏]')
        result = re.sub(r'https?://[^\s]+', '[链接已隐藏]', result)
        return result

    def start(self, reason='manual'):
        with self.lock:
            if self.processes() or (self.process and self.process.poll() is None):
                raise ValueError('项目已在运行，无需重复启动')
            cfg = self.config()
            missing = [key for key in ('StuId', 'UISPsw', 'DB_ENCRYPTION_KEY', 'COURSE_IDS') if not cfg.get(key) or cfg[key].startswith('your_')]
            if missing:
                raise ValueError('请在 .env 中完成：' + '、'.join(missing))
            if not any(cfg.get(k) for k in ('DEEPSEEK_API_KEY', 'DASHSCOPE_API_KEY', 'GEMINI_API_KEY')):
                raise ValueError('请配置至少一个摘要模型 API 密钥')
            name = 'run-' + datetime.now(BEIJING).strftime('%Y%m%d-%H%M%S-%f') + '.log'
            self.log = (self.directory / name).open('ab', buffering=0)
            env = dict(os.environ, PYTHONUTF8='1', PYTHONUNBUFFERED='1')
            python = self.python_path
            if not python.exists():
                raise ValueError('缺少项目 .venv，请先安装依赖')
            self.process = subprocess.Popen([str(python), '-u', 'run_local.py'], cwd=self.root, env=env,
                stdout=self.log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            self.state['last_run'] = {'pid': self.process.pid, 'started_at': datetime.now(BEIJING).isoformat(), 'log': name, 'reason': reason}
            self.save()
            return {'message': '项目已启动'}

    def stop(self):
        with self.lock:
            targets = self.processes()
            if not targets and self.process and self.process.poll() is None:
                try:
                    targets = [psutil.Process(self.process.pid)]
                except psutil.Error:
                    pass
            if not targets:
                return {'message': '项目当前未运行'}
            tree = []
            for p in targets:
                try:
                    tree.extend(p.children(recursive=True))
                    tree.append(p)
                except psutil.Error:
                    pass
            for p in reversed(tree):
                try:
                    p.terminate()
                except psutil.Error:
                    pass
            _, remaining = psutil.wait_procs(tree, timeout=4)
            for p in remaining:
                try:
                    p.kill()
                except psutil.Error:
                    pass
            if self.state.get('last_run'):
                self.state['last_run'].update(finished_at=datetime.now(BEIJING).isoformat(), stopped_by_user=True)
                self.save()
            return {'message': '已停止项目及音视频子进程；已保存的笔记保留'}

    def schedule(self, value):
        clock = str(value.get('time', '17:07'))
        if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', clock):
            raise ValueError('时间格式应为 HH:MM')
        with self.lock:
            now = datetime.now(BEIJING)
            previous = self.state['schedule']
            enabled = bool(value.get('enabled'))
            last = previous.get('last_date', '')
            if enabled and (not previous['enabled'] or previous['time'] != clock) and now.strftime('%H:%M') >= clock:
                last = now.date().isoformat()
            self.state['schedule'] = {'enabled': enabled, 'time': clock, 'last_date': last}
            self.save()
        return {'message': '定时已开启' if enabled else '定时已关闭'}

    def tick(self, now=None):
        now = now or datetime.now(BEIJING)
        with self.lock:
            s = self.state['schedule']
            day = now.date().isoformat()
            if s['enabled'] and s['last_date'] != day and now.strftime('%H:%M') >= s['time']:
                s['last_date'] = day
                self.save()
                if not self.processes():
                    try:
                        self.start('schedule')
                    except Exception as exc:
                        self.state['schedule']['error'] = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                        self.save()

    def scheduler_loop(self):
        while not self.shutdown_event.wait(15):
            self.tick()
            self.check_mail_due()

    def check_mail_due(self):
        with self.lock:
            if not self.mail_auto or self.mail_job['running'] or time.monotonic() < self._mail_next_check:
                return
            cfg = self.config()
            if not all(cfg.get(k) for k in ('IMAP_HOST', 'IMAP_ACCOUNT', 'IMAP_PASSWORD')):
                return
            self._mail_next_check = time.monotonic() + 180
            self.sync_mail()

    @contextmanager
    def db(self):
        path = Path(self.config().get('DB_PATH') or 'data/icourse.db')
        if not path.is_absolute():
            path = self.root / path
        db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    def courses(self, query='', term=''):
        selected = [s.strip() for s in (self.config().get('COURSE_IDS') or '').split(',') if s.strip()]
        with self.db() as db:
            terms = [r[0] for r in db.execute('SELECT DISTINCT term FROM all_courses ORDER BY term DESC')]
            params = []
            filters = []
            if query:
                filters.append('(title LIKE ? OR teacher LIKE ? OR course_id LIKE ?)')
                params.extend(['%' + query + '%'] * 3)
            if term:
                filters.append('term=?')
                params.append(term)
            sql = 'SELECT course_id,title,teacher,term FROM all_courses'
            if filters:
                sql += ' WHERE ' + ' AND '.join(filters)
            sql += ' ORDER BY term DESC,title LIMIT 120'
            catalog = [dict(r) for r in db.execute(sql, params)]
            subscriptions = []
            for course_id in selected:
                row = db.execute('SELECT course_id,title,teacher FROM courses WHERE course_id=?', (course_id,)).fetchone()
                subscriptions.append(dict(row) if row else {'course_id': course_id, 'title': '课程 ' + course_id, 'teacher': ''})
            rows = db.execute("SELECT c.course_id,c.title,count(l.sub_id) AS total,sum(length(trim(coalesce(l.summary,'')))>0) AS completed FROM courses c LEFT JOIN lectures l ON l.course_id=c.course_id AND l.deleted_at IS NULL GROUP BY c.course_id").fetchall()
        return {'selected': selected, 'subscriptions': subscriptions, 'catalog': catalog, 'terms': terms, 'progress': [dict(r) for r in rows]}

    def subscribe(self, ids):
        if not isinstance(ids, list) or len(ids) > 100 or any(not isinstance(i, str) or not re.fullmatch(r'\d{1,20}', i) for i in ids):
            raise ValueError('课程 ID 应为数字，最多订阅 100 门课程')
        with self.lock:
            set_key(str(self.root / '.env'), 'COURSE_IDS', ','.join(dict.fromkeys(ids)))
        return {'message': '订阅已保存；下一次启动生效，已有讲义保留'}

    def lectures(self):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT l.sub_id,l.course_id,l.sub_title,l.date,c.title AS course_title,l.processed_at,l.emailed_at,l.error_count,l.error_stage,length(coalesce(l.transcript,'')) AS transcript_chars,length(coalesce(l.summary,'')) AS summary_chars FROM lectures l JOIN courses c ON c.course_id=l.course_id WHERE l.deleted_at IS NULL ORDER BY l.date DESC,l.sub_id DESC")]

    def attention(self):
        with self.db() as db:
            rows = [dict(r) for r in db.execute("SELECT l.sub_id,l.sub_title,l.date,c.title AS course_title,l.error_stage,l.error_msg,l.error_count,l.processed_at,l.emailed_at,length(coalesce(l.transcript,'')) AS transcript_chars,length(coalesce(l.summary,'')) AS summary_chars FROM lectures l JOIN courses c ON c.course_id=l.course_id WHERE l.deleted_at IS NULL AND l.error_count>0 ORDER BY l.date DESC,l.sub_id DESC")]
        sensitive = [value for key, value in self.config().items() if value and any(term in key.upper() for term in ('PASSWORD','PSW','KEY','STUID','ACCOUNT','EMAIL'))]
        for row in rows:
            message = row.get('error_msg') or '没有保存具体错误信息'
            for value in sorted(sensitive, key=len, reverse=True): message = message.replace(value, '[已隐藏]')
            message = re.sub(r'https?://[^\s]+', '[链接已隐藏]', message)
            message = re.sub(r'(?i)(bearer\s+|(?:token|api[_-]?key|password)\s*[=:]\s*)[^\s,;]+', r'\1[已隐藏]', message)
            row['error_msg'] = message[:4000]
        return rows

    def lecture(self, sub_id):
        with self.db() as db:
            row = db.execute('SELECT l.sub_id,l.sub_title,l.date,l.transcript,l.summary,c.title AS course_title FROM lectures l JOIN courses c ON c.course_id=l.course_id WHERE l.sub_id=? AND l.deleted_at IS NULL', (sub_id,)).fetchone()
        if not row:
            raise ValueError('课次不存在')
        return dict(row)

    def mail_settings(self):
        c = self.config()
        return {'host': c.get('IMAP_HOST', ''), 'port': int(c.get('IMAP_PORT') or 993), 'account': c.get('IMAP_ACCOUNT', ''), 'password_set': bool(c.get('IMAP_PASSWORD')), 'sender': c.get('SMTP_EMAIL', '')}

    def save_mail(self, value):
        host = str(value.get('host', '')).strip()
        account = str(value.get('account', '')).strip()
        port = int(value.get('port', 993))
        if not re.fullmatch(r'[A-Za-z0-9.-]+', host) or '@' not in account or not 1 <= port <= 65535:
            raise ValueError('请填写有效的邮箱地址、IMAP 主机和端口')
        with self.lock:
            for key, content in [('IMAP_HOST', host), ('IMAP_PORT', str(port)), ('IMAP_ACCOUNT', account)]:
                set_key(str(self.root / '.env'), key, content)
            if value.get('password'):
                set_key(str(self.root / '.env'), 'IMAP_PASSWORD', str(value['password']))
        return {'message': '邮箱配置已保存，授权码不会返回页面'}

    def sync_mail(self):
        with self.lock:
            if self.mail_job['running']:
                raise ValueError('正在同步邮箱，请稍候')
            cfg = self.config()
            if not all(cfg.get(k) for k in ('IMAP_HOST', 'IMAP_ACCOUNT', 'IMAP_PASSWORD')):
                raise ValueError('请先填写并保存邮箱授权码')
            self._mail_next_check = time.monotonic() + 180
            self.mail_job = {'running': True, 'imported': 0, 'scanned': 0, 'total': 0, 'message': '正在连接邮箱…'}

        def worker():
            def progress(imported, scanned, total):
                with self.lock:
                    self.mail_job.update(imported=imported, scanned=scanned, total=total, message='正在导入课程讲义…')
            try:
                imported, scanned = self.archive.sync(cfg, progress)
                message = f'同步完成：检查 {scanned} 封，新增 {imported} 封讲义邮件'
            except Exception as exc:
                message = str(exc) if isinstance(exc, ValueError) else '邮箱连接失败（' + type(exc).__name__ + '），请检查客户端协议、授权码及网络'
            with self.lock:
                self.mail_job.update(running=False, message=message)
        threading.Thread(target=worker, daemon=True).start()
        return {'message': '邮箱同步已开始'}
