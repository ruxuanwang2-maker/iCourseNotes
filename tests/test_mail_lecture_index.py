import unittest
from src.control_plane.mailbox import mail_lecture_index


class MailIndexTests(unittest.TestCase):
    def test_multiple_courses_and_actual_class_dates(self):
        body='<h2>数值算法</h2><h3>第1-3节 (2026-09-08)</h3><h2>讲义内容标题</h2><h3>第4-6节 (2026-09-15)</h3><h2>概率论</h2><h3>第2-5节 (2026-09-11)</h3>'
        rows=mail_lecture_index('[FiCS] 数值算法 (2), 概率论 (1)',body)
        self.assertEqual([r['course_title'] for r in rows],['数值算法','数值算法','概率论'])
        self.assertEqual([r['date'] for r in rows],['2026-09-08','2026-09-15','2026-09-11'])

    def test_entities_and_update_badge(self):
        rows=mail_lecture_index('[FiCS] A & B (1)','<h2>A &amp; B</h2><h3><span>更新</span>第1节 <small>(2026-10-09)</small></h3>')
        self.assertEqual(rows[0]['course_title'],'A & B')
        self.assertEqual(rows[0]['date'],'2026-10-09')
        self.assertEqual(mail_lecture_index('[FiCS] 算法 (I) (1)（含 PPT 识别·更新）', '')[0]['course_title'],'算法 (I)')

    def test_missing_or_invalid_class_date_does_not_use_delivery_date(self):
        rows=mail_lecture_index('[FiCS] 课程 (1)','<h2>课程</h2><h3>第1节 (2026-02-30)</h3>')
        self.assertEqual(rows[0]['date'],'')
        self.assertEqual(mail_lecture_index('[FiCS] 其他邮件','<h2>正文</h2>'),[])


if __name__=='__main__': unittest.main()
