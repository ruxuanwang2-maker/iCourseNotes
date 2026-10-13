"""Strict completion policy and an independently cancellable local plan."""
import json
import time
from pathlib import Path


def pending_targets(targets, lectures):
    rows = {str(row['sub_id']): row for row in lectures}
    return [sid for sid in targets if sid not in rows or not rows[sid].get('summary_chars') or rows[sid].get('error_count', 0)]


def can_shutdown(targets, lectures, status):
    return bool(targets) and not status.get('running') and not (status.get('last_run') or {}).get('stopped_by_user') and not pending_targets(targets, lectures)


def read_plan(directory):
    try:
        plan = json.loads((Path(directory) / 'completion.json').read_text(encoding='utf-8'))
        signal = Path(directory) / 'completion-cancel.txt'
        if signal.exists() and signal.read_text(encoding='utf-8').strip() == plan['id']:
            plan.update(enabled=False, phase='cancelled', message='自动修复已取消')
        return plan
    except (OSError, ValueError, KeyError):
        return {}


def write_plan(directory, plan):
    path = Path(directory) / 'completion.json'
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def public_plan(directory):
    plan = read_plan(directory)
    return {key: plan.get(key) for key in ('enabled', 'phase', 'message', 'target_count', 'countdown_at', 'repaired')}
