import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from src.control_plane.completion import can_shutdown, read_plan, write_plan


class CompletionTests(TestCase):
    def test_requires_every_target_without_errors_and_no_running_worker(self):
        good=[{'sub_id':'a','summary_chars':100,'error_count':0}]
        self.assertTrue(can_shutdown(['a'],good,{'running':False,'last_run':None}))
        self.assertFalse(can_shutdown(['a'],good,{'running':True}))
        self.assertFalse(can_shutdown(['a'],good,{'running':False,'last_run':{'stopped_by_user':True}}))
        self.assertFalse(can_shutdown(['a','b'],good,{'running':False}))
        self.assertFalse(can_shutdown([],good,{'running':False}))
        self.assertFalse(can_shutdown(['b'],good,{'running':False}))
        self.assertFalse(can_shutdown(['a'],[{'sub_id':'a','summary_chars':0,'transcript_chars':5000,'error_count':2}],{'running':False}))
        self.assertFalse(can_shutdown(['a'],[{'sub_id':'a','summary_chars':100,'error_count':1}],{'running':False}))

    def test_cancel_signal_cannot_be_undone_by_a_racing_progress_write(self):
        with tempfile.TemporaryDirectory() as folder:
            directory=Path(folder);plan={'id':'first','enabled':True}
            write_plan(directory,plan)
            (directory/'completion-cancel.txt').write_text('first',encoding='utf-8')
            write_plan(directory,dict(plan,phase='waiting'))
            self.assertFalse(read_plan(directory)['enabled'])
            write_plan(directory,{'id':'new-plan','enabled':True})
            self.assertTrue(read_plan(directory)['enabled'])

    def test_completed_handouts_never_shutdown_computer(self):
        import watch_completion as watcher
        plan={'id':'test','enabled':True,'targets':['a']}
        good=[{'sub_id':'a','summary_chars':100,'error_count':0}]
        def api(path): return good if path=='lectures' else {'running':False}
        with patch.object(watcher,'active',return_value=plan),patch.object(watcher,'get',side_effect=api),patch.object(watcher,'progress') as progress,patch.object(watcher.subprocess,'run') as shutdown:
            watcher.run()
            shutdown.assert_not_called()
            self.assertEqual(progress.call_args.args[0],'completed')
            self.assertFalse(progress.call_args.kwargs['enabled'])

    def test_incomplete_model_response_cannot_mark_a_lecture_complete(self):
        import json
        import sqlite3
        import watch_completion as watcher
        from contextlib import closing
        from src.data.schema import SCHEMA_SQL
        from src.runtime import config
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);database=root/'course.sqlite'
            with closing(sqlite3.connect(database)) as connection,connection:
                connection.executescript(SCHEMA_SQL)
                connection.execute("INSERT INTO courses VALUES('1','fixture','teacher')")
                connection.execute("INSERT INTO lectures(sub_id,course_id,transcript,error_count) VALUES('a','1','saved transcription',2)")
            fake_db=SimpleNamespace(conn=sqlite3.connect(database),get_done_ppt_pages=lambda _:[])
            generator=SimpleNamespace(system_prompt='fixture',providers=[{'name':'deepseek','api_key':'fixture-key','base_url':'https://api.deepseek.com','models':['fixture']}])
            response=MagicMock()
            response.iter_lines.return_value=[b'data: '+json.dumps({'choices':[{'delta':{'content':'partial '*30}}]}).encode()]
            session=MagicMock();session.post.return_value=response
            with patch.object(watcher,'ROOT',root),patch.object(watcher,'active',return_value={'enabled':True}),patch.object(watcher,'progress'),patch.object(config,'DB_PATH',str(database)),patch('src.data.database.Database',return_value=fake_db),patch('src.ai.summarizer.Summarizer',return_value=generator),patch('requests.Session') as factory:
                factory.return_value.__enter__.return_value=session
                self.assertFalse(watcher.repair('a'))
            with closing(sqlite3.connect(database)) as connection:
                row=connection.execute("SELECT summary,processed_at,error_count FROM lectures WHERE sub_id='a'").fetchone()
            self.assertEqual(row,(None,None,2))
