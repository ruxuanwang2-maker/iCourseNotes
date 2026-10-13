"""Read-only IMAP import and safe local archiving of course emails."""
import base64
import hashlib
import html
import imaplib
import re
import sqlite3
from contextlib import contextmanager
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr
from pathlib import Path
from datetime import datetime, timedelta, timezone

import bleach

TAGS = set(bleach.sanitizer.ALLOWED_TAGS) | {'p', 'div', 'span', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'hr', 'br', 'table', 'thead', 'tbody', 'tr', 'td', 'th', 'pre', 'img'}


def mail_lecture_index(subject, body):
    """Index the sender's course headings and dated lecture headings only."""
    courses = [m.group(1).strip() for m in re.finditer(
        r'(?:^\[FiCS\]\s*|,\s*)(.*?)\s*\(\d+\)(?=,\s*|（含|$)', subject)]
    current = None
    lectures = []
    for match in re.finditer(r'<(h2|h3)\b[^>]*>(.*?)</\1>', body, flags=re.I | re.S):
        title = html.unescape(re.sub(r'<[^>]+>', '', match.group(2))).strip()
        if match.group(1).lower() == 'h2':
            if title in courses: current = title
        elif current:
            dated = re.fullmatch(r'(.*?)\s*\((\d{4}-\d{2}-\d{2})\)', title, flags=re.S)
            if dated:
                try: datetime.strptime(dated.group(2), '%Y-%m-%d')
                except ValueError: continue
                lectures.append({'course_title': current, 'sub_title': dated.group(1).strip(),
                                 'date': dated.group(2)})
    if not lectures:
        lectures = [{'course_title': course, 'sub_title': '课程汇总邮件', 'date': ''} for course in courses]
    return lectures


def single_lecture_body(subject, body, index):
    """Return only the selected dated h3 section from a sanitized course email."""
    lectures=mail_lecture_index(subject,body)
    if not 0<=index<len(lectures) or not lectures[index]['date']:
        raise ValueError('邮件没有可单独阅读的课次')
    wanted=lectures[index]
    current=None
    headings=list(re.finditer(r'<(h2|h3)\b[^>]*>(.*?)</\1>',body,re.I|re.S))
    for pos,heading in enumerate(headings):
        title=html.unescape(re.sub(r'<[^>]+>','',heading.group(2))).strip()
        if heading.group(1).lower()=='h2': current=title
        elif current==wanted['course_title']:
            dated=re.fullmatch(r'(.*?)\s*\((\d{4}-\d{2}-\d{2})\)',title,re.S)
            if dated and dated.group(1).strip()==wanted['sub_title'] and dated.group(2)==wanted['date']:
                end=headings[pos+1].start() if pos+1<len(headings) else len(body)
                return body[heading.start():end]
    raise ValueError('未找到邮件中的独立课次')


class MailArchive:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'archive.sqlite'
        with self.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS messages (
              id TEXT PRIMARY KEY, subject TEXT, sender TEXT, date TEXT,
              body TEXT, imported_at TEXT DEFAULT CURRENT_TIMESTAMP);
              CREATE TABLE IF NOT EXISTS attachments (
              id TEXT PRIMARY KEY, message_id TEXT, filename TEXT, mime TEXT, size INTEGER);''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def import_message(self, raw, expected_sender=''):
        if len(raw) > 30 * 1024 * 1024:
            raise ValueError('单封邮件超过 30 MB')
        msg = BytesParser(policy=policy.default).parsebytes(raw)
        subject = str(msg.get('Subject', '无标题'))
        sender = parseaddr(str(msg.get('From', '')))[1]
        if not subject.startswith('[FiCS]') or '有课程课次需要处理' in subject:
            return False
        if expected_sender and sender.casefold() != expected_sender.casefold():
            return False
        message_id = hashlib.sha256(raw).hexdigest()
        with self.db() as db:
            if db.execute('SELECT 1 FROM messages WHERE id=?', (message_id,)).fetchone():
                return False
        body_part = msg.get_body(preferencelist=('html', 'plain'))
        body = body_part.get_content() if body_part else ''
        if body_part and body_part.get_content_type() != 'text/html':
            body = '<pre>' + html.escape(body) + '</pre>'
        # Inline formula images are local data URLs; external images are removed.
        for part in msg.walk():
            cid = str(part.get('Content-ID', '')).strip('<>')
            if cid and part.get_content_type() in ('image/png', 'image/jpeg', 'image/gif'):
                encoded = base64.b64encode(part.get_payload(decode=True) or b'').decode('ascii')
                body = body.replace('cid:' + cid, 'data:' + part.get_content_type() + ';base64,' + encoded)
        body = re.sub(r'<(script|style)\b[^>]*>[\s\S]*?</\1\s*>', '', body, flags=re.I)

        def attributes(tag, name, value):
            if tag == 'img':
                return name in ('alt', 'width', 'height') or (name == 'src' and value.startswith(('data:image/png;base64,', 'data:image/jpeg;base64,', 'data:image/gif;base64,')))
            return name in ('colspan', 'rowspan') and value.isdigit()

        body = bleach.clean(body, tags=TAGS, attributes=attributes, protocols={'data'}, strip=True)
        attachments = []
        for part in msg.walk():
            name = part.get_filename()
            if not name:
                continue
            name = Path(str(name).replace('\\', '/')).name
            suffix = Path(name).suffix.lower()
            if suffix not in ('.pdf', '.md', '.txt', '.docx'):
                continue
            data = part.get_payload(decode=True) or b''
            if suffix == '.pdf' and not data.startswith(b'%PDF-'):
                continue
            attachment_id = hashlib.sha256((message_id + name).encode() + data).hexdigest()
            (self.directory / attachment_id).write_bytes(data)
            mime = {'.pdf': 'application/pdf', '.md': 'text/plain; charset=utf-8', '.txt': 'text/plain; charset=utf-8', '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'}[suffix]
            attachments.append((attachment_id, message_id, name, mime, len(data)))
        with self.db() as db:
            imported_at = datetime.now(timezone(timedelta(hours=8))).isoformat()
            db.execute('INSERT INTO messages(id,subject,sender,date,body,imported_at) VALUES(?,?,?,?,?,?)', (message_id, subject, sender, str(msg.get('Date', '')), body, imported_at))
            db.executemany('INSERT OR IGNORE INTO attachments VALUES(?,?,?,?,?)', attachments)
        return True

    def list(self):
        with self.db() as db:
            rows = [dict(row) for row in db.execute('SELECT id,subject,sender,date,imported_at,body,(SELECT count(*) FROM attachments WHERE message_id=messages.id) AS attachments FROM messages ORDER BY imported_at DESC')]
        for row in rows:
            row['lectures'] = mail_lecture_index(row['subject'], row.pop('body'))
        return rows

    def message(self, message_id):
        with self.db() as db:
            row = db.execute('SELECT * FROM messages WHERE id=?', (message_id,)).fetchone()
            if not row:
                raise ValueError('邮件不存在')
            value = dict(row)
            value['attachments'] = [dict(r) for r in db.execute('SELECT * FROM attachments WHERE message_id=?', (message_id,))]
            return value

    def attachment(self, attachment_id):
        with self.db() as db:
            row = db.execute('SELECT * FROM attachments WHERE id=?', (attachment_id,)).fetchone()
        if not row:
            raise ValueError('附件不存在')
        return dict(row), (self.directory / attachment_id).read_bytes()

    def sync(self, config, progress):
        host = config.get('IMAP_HOST', '').strip()
        account = config.get('IMAP_ACCOUNT', '').strip()
        password = config.get('IMAP_PASSWORD', '')
        if not all((host, account, password)):
            raise ValueError('请先配置学校邮箱的 IMAP 地址、账号和授权码')
        imported = scanned = 0
        # Read-only mailbox and BODY.PEEK preserve unread flags and all messages.
        with imaplib.IMAP4_SSL(host, int(config.get('IMAP_PORT') or 993), timeout=60) as client:
            client.login(account, password)
            status, _ = client.select('INBOX', readonly=True)
            if status != 'OK':
                raise ValueError('无法读取收件箱')
            sender = config.get('SMTP_EMAIL', '').strip()
            criteria = '(SUBJECT "[FiCS]")'
            if sender and re.fullmatch(r'[A-Za-z0-9_.+@-]+', sender):
                criteria = '(FROM "' + sender + '" SUBJECT "[FiCS]")'
            status, results = client.uid('search', None, criteria)
            if status != 'OK':
                raise ValueError('邮箱搜索失败')
            uids = results[0].split() if results and results[0] else []
            if len(uids) > 500:
                raise ValueError('匹配邮件超过 500 封，请先使用邮件文件导入或缩小邮箱范围')
            for uid in reversed(uids):
                status, size_result = client.uid('fetch', uid, '(RFC822.SIZE)')
                size_text = b' '.join(x for x in size_result if isinstance(x, bytes))
                size = re.search(rb'RFC822.SIZE\s+(\d+)', size_text)
                if status != 'OK' or not size or int(size.group(1)) > 30 * 1024 * 1024:
                    continue
                status, parts = client.uid('fetch', uid, '(BODY.PEEK[])')
                if status == 'OK':
                    for item in parts:
                        if isinstance(item, tuple) and isinstance(item[1], bytes):
                            imported += int(self.import_message(item[1], sender))
                            scanned += 1
                progress(imported, scanned, len(uids))
        return imported, scanned
