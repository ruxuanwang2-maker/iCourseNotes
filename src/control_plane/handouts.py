"""Canonical per-lecture handouts with recoverable removal of exact duplicates."""
from pathlib import Path
import hashlib
import re
import sqlite3
from contextlib import closing

def sync_handouts(root):
    root=Path(root).resolve()
    database=root/'data/icourse.db'
    if not database.exists(): return {'exported':0,'duplicates':0}
    folder=root/'data/handouts';folder.mkdir(parents=True,exist_ok=True)
    archive=root/'data/local-control/duplicate-handouts';archive.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(database,timeout=10)) as db,db:
        db.row_factory=sqlite3.Row
        rows=[dict(r) for r in db.execute("SELECT l.*,c.title course_title FROM lectures l JOIN courses c ON c.course_id=l.course_id WHERE l.deleted_at IS NULL AND length(trim(coalesce(l.summary,'')))>0 ORDER BY l.processed_at DESC,l.sub_id DESC")]
        unique={};removed=0;exported=0;canonical={};dated={}
        for row in rows:
            fingerprint=hashlib.sha256(row['summary'].encode('utf-8')).hexdigest()
            key=(row['course_id'],row['date'],row['sub_title'],fingerprint)
            if key in unique:
                db.execute("UPDATE lectures SET deleted_at=CURRENT_TIMESTAMP WHERE sub_id=?",(row['sub_id'],));removed+=1;continue
            unique[key]=row['sub_id']
            course=re.sub(r'[<>:"/\\|?*]','',row['course_title']).strip(' .') or '课程'
            name=re.sub(r'[^0-9-]','',row['date'])+'-'+str(row['sub_id'])+'.md'
            target=folder/course/name
            if not target.resolve().is_relative_to(folder.resolve()): raise ValueError('Invalid handout path')
            target.parent.mkdir(parents=True,exist_ok=True)
            if not target.exists() or target.read_text(encoding='utf-8')!=row['summary']:
                temporary=target.with_suffix('.tmp');temporary.write_text(row['summary'],encoding='utf-8');temporary.replace(target);exported+=1
            canonical.setdefault((course,fingerprint),set()).add(target.resolve())
            dated.setdefault((course,row['date']),set()).add(target.resolve())
        for file in folder.rglob('*.md'):
            resolved=file.resolve()
            if not resolved.is_relative_to(folder.resolve()): continue
            fingerprint=hashlib.sha256(file.read_text(encoding='utf-8').encode('utf-8')).hexdigest()
            targets=canonical.get((file.parent.name,fingerprint),set())
            date_only=re.fullmatch(r'(\d{4}-\d{2}-\d{2})\.md',file.name)
            known_date=dated.get((file.parent.name,date_only.group(1)),set()) if date_only else set()
            obsolete_date_name=len(known_date)==1 and resolved not in known_date
            if (targets and resolved not in targets) or obsolete_date_name:
                destination=archive/(hashlib.sha256(str(resolved).encode()).hexdigest()[:12]+'-'+fingerprint[:12]+'-'+file.name)
                if not destination.resolve().is_relative_to(archive.resolve()): raise ValueError('Invalid archive path')
                file.replace(destination);removed+=1
    return {'exported':exported,'duplicates':removed}
