import base64
from contextlib import closing
from datetime import datetime
from email.message import EmailMessage
import http.client
import json
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from dotenv import dotenv_values
from http.server import ThreadingHTTPServer
from src.control_plane.server import make_handler
from src.control_plane.service import BEIJING, ControlService
from src.data.schema import SCHEMA_SQL


class ControlPlaneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / '.env').write_text('StuId=fixture-student\nUISPsw=fixture-password\nDB_ENCRYPTION_KEY=fixture-db-key\nCOURSE_IDS=123\nDEEPSEEK_API_KEY=private-test-key\nSMTP_EMAIL=sender@example.com\nDB_PATH=data/icourse.db\n', encoding='utf-8')
        (self.root / 'data').mkdir()
        with closing(sqlite3.connect(self.root / 'data/icourse.db')) as db, db:
            db.executescript(SCHEMA_SQL)
            db.execute('INSERT INTO courses VALUES(?,?,?)', ('123', '测试课程', '测试教师'))
            db.execute('INSERT INTO lectures(sub_id,course_id,sub_title,date) VALUES(?,?,?,?)', ('l1', '123', '第一课', '2026-10-09'))
        self.service = ControlService(self.root, python_path=sys.executable)

    def tearDown(self):
        self.service.stop()
        if self.service.log:
            self.service.log.close()
        self.temp.cleanup()

    def email(self):
        msg = EmailMessage()
        msg['Subject'] = '[FiCS] 测试课程 (1)'
        msg['From'] = 'sender@example.com'
        msg['To'] = 'student@example.edu.cn'
        msg.set_content('课程正文')
        msg.add_alternative('<h2>测试讲义</h2><script>alert(1)</script><img src="https://tracker.test/a"><p onclick="bad()">公式与正文</p>', subtype='html')
        msg.add_attachment(b'%PDF-1.4\n%%EOF', maintype='application', subtype='pdf', filename='../../lecture.pdf')
        return msg.as_bytes()

    def test_import_deduplicates_and_sanitizes(self):
        raw = self.email()
        self.assertTrue(self.service.archive.import_message(raw, 'sender@example.com'))
        self.assertFalse(self.service.archive.import_message(raw, 'sender@example.com'))
        item = self.service.archive.message(self.service.archive.list()[0]['id'])
        self.assertNotIn('alert(1)', item['body'])
        self.assertNotIn('https://tracker.test', item['body'])
        self.assertNotIn('onclick', item['body'])
        self.assertEqual(item['attachments'][0]['filename'], 'lecture.pdf')
        self.assertEqual(self.service.archive.attachment(item['attachments'][0]['id'])[1], b'%PDF-1.4\n%%EOF')
        self.assertFalse(self.service.archive.import_message(raw, 'another@example.com'))

    def test_imap_preserves_mailbox_flags(self):
        raw = self.email()
        from unittest.mock import MagicMock
        client = MagicMock()
        client.select.return_value = ('OK', [])
        def uid(command, *args):
            if command == 'search': return 'OK', [b'1']
            if args[-1] == '(RFC822.SIZE)': return 'OK', [b'1 (RFC822.SIZE 1000)']
            return 'OK', [(b'1 BODY[]', raw)]
        client.uid.side_effect = uid
        with patch('src.control_plane.mailbox.imaplib.IMAP4_SSL') as factory:
            factory.return_value.__enter__.return_value = client
            self.assertEqual(self.service.archive.sync({'IMAP_HOST':'mail.example.com','IMAP_ACCOUNT':'student@example.edu.cn','IMAP_PASSWORD':'secret','SMTP_EMAIL':'sender@example.com'}, lambda *args: None), (1, 1))
        client.select.assert_called_once_with('INBOX', readonly=True)
        self.assertTrue(any(call.args[-1] == '(BODY.PEEK[])' for call in client.uid.call_args_list))
        self.assertFalse(any(call.args[0] in ('store','expunge') for call in client.uid.call_args_list))

    def test_subscription_preserves_credentials(self):
        self.service.subscribe(['123', '456', '456'])
        cfg = dotenv_values(self.root / '.env')
        self.assertEqual(cfg['COURSE_IDS'], '123,456')
        self.assertEqual(cfg['DEEPSEEK_API_KEY'], 'private-test-key')
        with self.assertRaises(ValueError): self.service.subscribe(['../../foo'])

    def test_settings_keep_blanks_private_and_preserve_other_configuration(self):
        public=self.service.settings()
        self.assertTrue(public['configured']['UISPsw'])
        self.assertNotIn('fixture-password',json.dumps(public))
        self.assertNotIn('private-test-key',json.dumps(public))
        self.service.save_settings({'fields':{'StuId':'123456789','UISPsw':'',
            'DEEPSEEK_API_KEY':"new-key'quoted",'DEEPSEEK_BASE_URL':'https://api.deepseek.com/v1',
            'RECEIVER_EMAILS':'a@example.com; b@example.com'}})
        config=dotenv_values(self.root/'.env')
        self.assertEqual(config['UISPsw'],'fixture-password')
        self.assertEqual(config['DEEPSEEK_API_KEY'],"new-key'quoted")
        self.assertEqual(config['RECEIVER_EMAILS'],'a@example.com,b@example.com')
        self.assertEqual(config['COURSE_IDS'],'123')
        self.assertEqual(config['DB_ENCRYPTION_KEY'],'fixture-db-key')
        self.assertNotIn('123456789',json.dumps(self.service.settings()))

    def test_settings_validation_is_atomic_and_does_not_echo_submitted_secrets(self):
        before=(self.root/'.env').read_bytes()
        for fields in ({'StuId':'123','IMAP_PORT':'0'}, {'DEEPSEEK_API_KEY':'secret\nINJECT=1'},
                       {'COURSE_IDS':'999'}, {'DEEPSEEK_BASE_URL':'https://user:secret@example.com'},
                       {'SMTP_EMAIL':'bad-address'}, {'DEEPSEEK_BASE_URL':'http://remote.example.com'}):
            with self.assertRaises(ValueError):self.service.save_settings({'fields':fields})
            self.assertEqual((self.root/'.env').read_bytes(),before)

    def test_attention_details_are_read_only_and_redact_private_values(self):
        error='private-test-key fixture-password sender@example.com https://example.test/video?signature=secret Bearer unknown-secret'
        with closing(sqlite3.connect(self.root / 'data/icourse.db')) as db, db:
            db.execute("UPDATE lectures SET error_count=2,error_stage='summarize',error_msg=?,transcript='saved text' WHERE sub_id='l1'",(error,))
            db.execute("INSERT INTO lectures(sub_id,course_id,sub_title,date,error_count,deleted_at) VALUES('deleted','123','deleted','2026-10-09',3,'2026-10-09')")
        rows=self.service.attention()
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['transcript_chars'],10)
        self.assertEqual(rows[0]['error_stage'],'summarize')
        self.assertNotIn('transcript',rows[0])
        for private in ('private-test-key','fixture-password','sender@example.com','signature=secret','unknown-secret'):
            self.assertNotIn(private,rows[0]['error_msg'])
        with closing(sqlite3.connect(self.root / 'data/icourse.db')) as db, db:
            self.assertEqual(db.execute("SELECT error_count,error_msg FROM lectures WHERE sub_id='l1'").fetchone(),(2,error))

    def test_automatic_mail_polling_is_throttled(self):
        cfg={'IMAP_HOST':'imap.qq.com','IMAP_ACCOUNT':'student@qq.com','IMAP_PASSWORD':'fixture-code'}
        with patch.object(self.service,'config',return_value=cfg), patch.object(self.service,'sync_mail') as sync:
            self.service.check_mail_due(); self.service.check_mail_due()
            sync.assert_called_once()
        self.service._mail_next_check=0
        self.service.mail_job['running']=True
        with patch.object(self.service,'sync_mail') as sync:
            self.service.check_mail_due()
            sync.assert_not_called()

    def test_schedule_once_daily_without_overlap(self):
        self.service.state['schedule'] = {'enabled': True, 'time':'17:07', 'last_date':''}
        now = datetime(2026,10,9,17,8,tzinfo=BEIJING)
        with patch.object(self.service, 'processes', return_value=[]), patch.object(self.service, 'start') as start:
            self.service.tick(now); self.service.tick(now)
            start.assert_called_once_with('schedule')
        self.service.state['schedule']['last_date'] = ''
        with patch.object(self.service, 'processes', return_value=[object()]), patch.object(self.service, 'start') as start:
            self.service.tick(now)
            start.assert_not_called()
        self.service.schedule({'enabled':False,'time':'17:07'})
        self.assertFalse(ControlService(self.root).state['schedule']['enabled'])

    def test_real_local_process_start_stop_and_double_start_guard(self):
        (self.root / 'run_local.py').write_text('import time\nprint("test worker running",flush=True)\ntime.sleep(60)\n', encoding='utf-8')
        self.service.start()
        deadline = time.time() + 10
        while (not self.service.processes() or 'test worker running' not in self.service.logs()) and time.time() < deadline: time.sleep(.1)
        self.assertTrue(self.service.status()['running'])
        with self.assertRaises(ValueError): self.service.start()
        self.service.stop()
        self.assertFalse(self.service.status()['running'])
        self.assertIn('test worker running', self.service.logs())

    def test_http_auth_secret_privacy_and_attachment(self):
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0)); port=s.getsockname()[1]
        server=ThreadingHTTPServer(('127.0.0.1',port),make_handler(self.service,port))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def request(path, data=None, headers=None):
            conn=http.client.HTTPConnection('127.0.0.1',port,timeout=10)
            conn.request('GET' if data is None else 'POST',path,body=json.dumps(data).encode() if data is not None else None,headers=headers or {})
            response=conn.getresponse(); result=response.status,response.read(); conn.close();return result
        try:
            code,data=request('/api/status');self.assertEqual(code,200); token=json.loads(data)['token']
            self.assertNotIn(b'private-test-key',data)
            self.assertEqual(request('/api/schedule',{'enabled':True,'time':'17:07'})[0],403)
            headers={'Origin':f'http://127.0.0.1:{port}','X-Control-Token':token}
            self.assertEqual(request('/api/settings',{'fields':{'DEEPSEEK_API_KEY':'fake-new-key'}})[0],403)
            settings=json.loads(request('/api/settings')[1]);self.assertTrue(settings['configured']['UISPsw'])
            code,saved=request('/api/settings',{'fields':{'DEEPSEEK_API_KEY':'fake-new-key'}},headers)
            self.assertEqual(code,200);self.assertNotIn(b'fake-new-key',saved)
            self.assertNotIn(b'fake-new-key',request('/api/settings')[1])
            self.assertEqual(request('/api/schedule',{'enabled':False,'time':'17:07'},headers)[0],200)
            evil=dict(headers,Origin='http://evil.example');self.assertEqual(request('/api/stop',{},evil)[0],403)
            self.assertEqual(request('/api/status',headers={'Host':'evil.example'})[0],403)
            self.service.save_mail({'host':'imap.example.com','account':'student@example.edu.cn','port':993,'password':'do-not-expose'})
            self.assertNotIn(b'do-not-expose',request('/api/mail/settings')[1])
            upload={'content':base64.b64encode(self.email()).decode()}
            self.assertEqual(request('/api/mail/upload',upload,headers)[0],200)
            mail=json.loads(request('/api/mail/messages')[1])[0]
            detail=json.loads(request('/api/mail/message/'+mail['id'])[1])
            self.assertEqual(request('/api/mail/attachment/'+detail['attachments'][0]['id'])[0],200)
        finally:
            server.shutdown();server.server_close();thread.join()


if __name__ == '__main__': unittest.main()
