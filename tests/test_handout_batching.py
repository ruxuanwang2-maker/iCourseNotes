import tempfile
import sqlite3
from pathlib import Path
from contextlib import closing
from unittest import TestCase
from unittest.mock import patch,MagicMock
from src.ai.qwen_transcriber import QwenTranscriber
from src.control_plane.mailbox import single_lecture_body
from src.control_plane.handouts import sync_handouts
from src.data.schema import SCHEMA_SQL

class HandoutBatchTests(TestCase):
    def test_mail_sections_do_not_include_other_lectures(self):
        body='<h2>概率论</h2><h3>第一课 (2026-09-11)</h3><p>A</p><h3>第二课 (2026-09-18)</h3><p>B</p>'
        first=single_lecture_body('[FiCS] 概率论(2)',body,0)
        self.assertIn('<p>A</p>',first);self.assertNotIn('第二课',first)
        self.assertIn('<p>B</p>',single_lecture_body('[FiCS] 概率论(2)',body,1))
        with self.assertRaises(ValueError): single_lecture_body('[FiCS] 概率论(2)',body,2)

    def test_exact_duplicate_files_are_archived_and_independent_lectures_kept(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'data/handouts/课程').mkdir(parents=True)
            with closing(sqlite3.connect(root/'data/icourse.db')) as db,db:
                db.executescript(SCHEMA_SQL)
                db.execute("INSERT INTO courses(course_id,title) VALUES('1','课程')")
                for sid,title,summary in [('a','第1节','正文甲'),('b','第1节','正文甲'),('c','第2节','正文甲')]:
                    db.execute("INSERT INTO lectures(sub_id,course_id,sub_title,date,summary) VALUES(?,'1',?,'2026-09-11',?)",(sid,title,summary))
            (root/'data/handouts/课程/old.md').write_text('正文甲',encoding='utf-8')
            result=sync_handouts(root)
            self.assertEqual(result['exported'],2)
            self.assertFalse((root/'data/handouts/课程/old.md').exists())
            self.assertEqual(len(list((root/'data/handouts/课程').glob('*.md'))),2)
            self.assertEqual(sync_handouts(root),{'exported':0,'duplicates':0})

    def test_batch_order_checkpoint_and_single_retry(self):
        import numpy as np
        t=QwenTranscriber();t._init=MagicMock();t.release_model=MagicMock()
        t._recognize_batch=MagicMock(return_value=[{'text':'甲','quality_state':'recognized'},None])
        t._recognize_resilient=MagicMock(return_value={'text':'乙','quality_state':'bounded_retry'})
        checkpoints=[]
        blocks=[{'chunk_id':7,'start':0,'end':1},{'chunk_id':3,'start':1,'end':2}]
        with patch('src.ai.qwen_transcriber.config.ASR_BATCH_SIZE',2):
            rows=t.recognize_blocks(blocks,lambda b:np.zeros(16000,dtype=np.float32),checkpoint=lambda r:checkpoints.append(len(r)))
        self.assertEqual([r['chunk_id'] for r in rows],[7,3]);self.assertEqual(checkpoints,[1,2])
        self.assertEqual(t._recognize_batch.call_count,1);self.assertEqual(t._recognize_resilient.call_count,1)

    def test_out_of_memory_falls_back_without_losing_blocks(self):
        import torch
        import numpy as np
        t=QwenTranscriber();t._init=MagicMock();t.release_model=MagicMock()
        t._recognize_batch=MagicMock(side_effect=torch.cuda.OutOfMemoryError('test'))
        t._recognize_resilient=MagicMock(return_value={'text':'课程'})
        blocks=[{'chunk_id':i,'start':i,'end':i+1} for i in range(4)]
        with patch('src.ai.qwen_transcriber.config.ASR_BATCH_SIZE',2),patch.object(torch.cuda,'empty_cache'):
            rows=t.recognize_blocks(blocks,lambda b:np.zeros(16000,dtype=np.float32))
        self.assertEqual(len(rows),4);self.assertEqual(t._recognize_batch.call_count,1)
        self.assertEqual(t._recognize_resilient.call_count,4)
