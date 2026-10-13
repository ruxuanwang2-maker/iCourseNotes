"""Finish saved transcripts using the configured LLM, keep the computer on.

Only a pre-authorized local plan enables this program. It never treats missing
audio, failed summaries, API downtime or a stopped worker as complete.
"""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from contextlib import closing
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
from src.control_plane.completion import read_plan, write_plan, can_shutdown, pending_targets

DIRECTORY = ROOT / 'data/local-control'
identity = sys.argv[1] if len(sys.argv) == 2 else ''


def active():
    plan = read_plan(DIRECTORY)
    return plan if plan.get('enabled') and plan.get('id') == identity else None


def progress(phase, message, **updates):
    plan = active()
    if plan:
        plan.update(phase=phase, message=message, **updates)
        write_plan(DIRECTORY, plan)


def get(path):
    with urlopen('http://127.0.0.1:18081/api/' + path, timeout=15) as response:
        return json.load(response)


def repair(sid):
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env', override=True)
    # Provider resolution reads the freshly loaded environment for each retry.
    # Credentials never enter progress logs.
    from src.runtime import config
    from src.ai.summarizer import Summarizer, terminology_reference
    from src.ai.bucketer import assemble
    from src.data.database import Database
    import requests
    db = Database()
    try:
        with closing(sqlite3.connect(config.DB_PATH)) as connection, connection:
            connection.row_factory = sqlite3.Row
            found = connection.execute('SELECT l.*,c.title course_title FROM lectures l JOIN courses c ON c.course_id=l.course_id WHERE l.sub_id=? AND l.deleted_at IS NULL', (sid,)).fetchone()
            row = dict(found) if found else None
        if not row or row.get('summary') or not row.get('transcript'): return False
        prompt, _ = assemble(row['transcript'], None, db.get_done_ppt_pages(sid))
        generator = Summarizer()
        summary = None
        model = None
        for provider in generator.providers:
            for model_name in provider['models']:
                if not active(): return False
                try:
                    with requests.Session() as session:
                        session.headers['Authorization'] = 'Bearer ' + provider['api_key']
                        response = session.post(provider['base_url'].rstrip('/') + '/chat/completions', json={
                            'model': model_name, 'stream': True, 'temperature': 0.2,
                            'messages': [{'role': 'system', 'content': generator.system_prompt},
                                {'role': 'user', 'content': f"以下是课程《{row['course_title']}》的原始材料。请整理为详细、连贯、适合复习的课程讲义。\n\n<course_material>\n{prompt}\n</course_material>" + terminology_reference(row['course_title'])}]},
                            timeout=(30, 120), stream=True)
                        response.raise_for_status()
                        parts = []; done = False; finish = None
                        for line in response.iter_lines():
                            if not active(): response.close(); return False
                            if not line.startswith(b'data:'): continue
                            payload = line[5:].strip()
                            if payload == b'[DONE]': done = True; break
                            for choice in json.loads(payload).get('choices', []):
                                text = choice.get('delta', {}).get('content')
                                if text: parts.append(text)
                                finish = choice.get('finish_reason') or finish
                        response.close()
                    candidate = ''.join(parts).strip()
                    if not done or finish != 'stop' or len(candidate) < 100: raise ValueError('Incomplete summary')
                    summary, model = candidate, provider['name'] + '/' + model_name
                    break
                except Exception as error:
                    progress('repairing', '模型连接或返回异常，稍后重试（' + type(error).__name__ + '）')
            if summary: break
        if not summary or not active(): return False
        with closing(sqlite3.connect(config.DB_PATH, timeout=30)) as connection, connection:
            result = connection.execute("UPDATE lectures SET summary=?,summary_model=?,processed_at=?,error_count=0,error_stage=NULL,error_msg=NULL,failure_notified_at=NULL WHERE sub_id=? AND coalesce(summary,'')='' AND transcript=? AND deleted_at IS NULL", (summary, model, datetime.now(timezone.utc).isoformat(), sid, row['transcript']))
        if result.rowcount != 1: return False
        # Include the identifier to keep same-day lectures distinct.
        course = ''.join(c for c in row['course_title'] if c not in '<>:"/\\|?*').strip(' .') or '课程'
        folder = ROOT / 'data/handouts' / course
        folder.mkdir(parents=True, exist_ok=True)
        (folder / (row['date'] + '-' + sid + '.md')).write_text(summary, encoding='utf-8')
        plan = active()
        if plan: progress('waiting', '失败讲义已修复并保存到应用', repaired=plan.get('repaired', 0) + 1)
        return True
    finally:
        db.conn.close()


def run():
    plan = active()
    attempts = plan.get('retry_attempts', {}); next_try = plan.get('retry_after', {}); deadline = None
    progress('waiting', '自动完成监视已启动', watch_pid=os.getpid())
    while active():
        try:
            status, lectures = get('status'), get('lectures')
            plan = active()
            if not plan: return
            targets = plan['targets']
            if status['running']:
                deadline = None
                progress('waiting', '等待当前课程处理结束；之后自动修复已有转写的失败讲义', countdown_at=None)
                time.sleep(30); continue
            if can_shutdown(targets, lectures, status):
                progress('completed', '全部目标讲义已完成并保存；电脑保持开机', enabled=False, countdown_at=None)
                return
            deadline = None
            pending = pending_targets(targets, lectures)
            rows = {str(row['sub_id']): row for row in lectures}
            eligible = [sid for sid in pending if sid in rows and rows[sid].get('transcript_chars') and not rows[sid].get('summary_chars') and attempts.get(sid, 0) < 3 and time.time() >= next_try.get(sid, 0)]
            if eligible and not (status.get('last_run') or {}).get('stopped_by_user'):
                sid = eligible[0]
                progress('repairing', '正在重新生成失败讲义，完整返回后保存到应用', countdown_at=None)
                attempts[sid] = attempts.get(sid, 0) + 1
                progress('repairing', '正在重新生成失败讲义', retry_attempts=attempts)
                if not repair(sid): next_try[sid] = time.time() + (300 if attempts[sid] == 1 else 1800)
                progress('waiting', '修复检查完成，继续核对所有讲义', retry_attempts=attempts, retry_after=next_try)
                continue
            progress('blocked', f'还有 {len(pending)} 个课次未完成；暂无转写或重试未成功，保持开机等待处理', countdown_at=None)
            time.sleep(60)
        except Exception as error:
            deadline = None
            progress('waiting', '检查暂不可用，保持开机等待恢复（' + type(error).__name__ + '）', countdown_at=None)
            time.sleep(30)


if __name__ == '__main__' and identity: run()
