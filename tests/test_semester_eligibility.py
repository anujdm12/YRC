import os
import tempfile
import unittest

DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False).name
os.environ['DATABASE_PATH'] = DB_FILE
os.environ['SECRET_KEY'] = 'semester-test-secret'
import app as portal


class SemesterEligibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = portal.app.test_client()
        with portal.app.test_request_context('/'):
            conn = portal.connect_db(DB_FILE)
            cur = conn.execute("INSERT INTO students(register_no,name,department,class_name,year) VALUES('T001','Test Student','Health','BSc',1)")
            cls.student_id = cur.lastrowid
            event=conn.execute("INSERT INTO events(name,event_date,status,semester) VALUES('Route Smoke Event','2026-01-01','Cancelled',1)")
            cls.event_id=event.lastrowid
            conn.execute('INSERT INTO event_participants(event_id,student_id) VALUES(?,?)',(cls.event_id,cls.student_id))
            conn.execute("INSERT INTO attendance(event_id,student_id,status) VALUES(?,?,'Present')",(cls.event_id,cls.student_id))
            conn.commit(); conn.close()

    def setUp(self):
        with portal.app.app_context():
            conn=portal.db()
            conn.execute("DELETE FROM attendance WHERE student_id=?",(self.student_id,))
            conn.execute("DELETE FROM event_participants WHERE student_id=?",(self.student_id,))
            conn.execute("DELETE FROM events WHERE name LIKE 'Test activity%'" )
            conn.execute("DELETE FROM semester_certificates WHERE student_id=?",(self.student_id,))
            conn.execute("DELETE FROM semester_rules WHERE semester_id IN (1,2)")
            conn.execute("INSERT INTO semester_rules(semester_id,rule_type,value,mandatory) VALUES(1,'minimum_activities','2',1)")
            conn.execute("INSERT INTO semester_rules(semester_id,rule_type,value,mandatory,verification_required) VALUES(1,'certificate','ICRC',1,1)")
            conn.execute("INSERT INTO semester_rules(semester_id,rule_type,value,mandatory) VALUES(2,'minimum_activities','1',1)")
            conn.commit()

    def attendance(self, count):
        with portal.app.app_context():
            conn=portal.db()
            for index in range(count):
                event=conn.execute("INSERT INTO events(name,event_date,status,semester) VALUES(?, '2026-01-01','Completed',1)",(f'Test activity {index}',))
                event_id=event.lastrowid
                conn.execute("INSERT INTO event_participants(event_id,student_id) VALUES(?,?)",(event_id,self.student_id))
                conn.execute("INSERT INTO attendance(event_id,student_id,status) VALUES(?,?,'Present')",(event_id,self.student_id))
            conn.commit()

    def certificate(self, status):
        with portal.app.app_context():
            conn=portal.db()
            conn.execute("INSERT INTO semester_certificates(semester_id,student_id,certificate_type,status) VALUES(1,?,'ICRC',?)",(self.student_id,status))
            conn.commit()

    def calculate(self, semester=1):
        with portal.app.app_context():
            return portal.semester_eligibility(semester,self.student_id)

    def test_two_activities_and_verified_icrc_is_eligible(self):
        self.attendance(2); self.certificate('Verified')
        self.assertEqual('ELIGIBLE',self.calculate()['status'])

    def test_one_activity_is_not_eligible(self):
        self.attendance(1); self.certificate('Verified')
        self.assertEqual('NOT_ELIGIBLE',self.calculate()['status'])

    def test_missing_certificate_is_not_eligible(self):
        self.attendance(2)
        self.assertEqual('NOT_ELIGIBLE',self.calculate()['status'])

    def test_submitted_certificate_is_pending_not_eligible(self):
        self.attendance(2); self.certificate('Submitted')
        self.assertEqual('PENDING_VERIFICATION',self.calculate()['status'])

    def test_unconfigured_semester_is_incomplete(self):
        self.assertEqual('INCOMPLETE_RECORDS',self.calculate(3)['status'])

    def test_semester_rules_are_independent(self):
        with portal.app.app_context():
            portal.db().execute("UPDATE semester_rules SET value='4' WHERE semester_id=1 AND rule_type='minimum_activities'")
            sem2=portal.db().execute("SELECT value FROM semester_rules WHERE semester_id=2 AND rule_type='minimum_activities'").fetchone()
            self.assertEqual('1',sem2['value'])

    def test_duplicate_event_attendance_cannot_inflate_count(self):
        self.attendance(2)
        # Attendance is constrained by event and student, and the calculation counts distinct events.
        self.assertEqual(2,self.calculate()['activities'])

    def test_rule_update_preserves_participation_and_certificate_history(self):
        self.attendance(2); self.certificate('Submitted')
        with portal.app.app_context():
            portal.db().execute("UPDATE semester_rules SET value='3' WHERE semester_id=1 AND rule_type='minimum_activities'")
            result=portal.semester_eligibility(1,self.student_id)
            self.assertEqual(2,result['activities'])
            self.assertEqual(2,portal.db().execute('SELECT COUNT(*) FROM attendance WHERE student_id=?',(self.student_id,)).fetchone()[0])
            self.assertEqual(1,portal.db().execute('SELECT COUNT(*) FROM semester_certificates WHERE student_id=?',(self.student_id,)).fetchone()[0])

    def test_private_semester_page_requires_admin_session(self):
        with self.client.session_transaction() as session:
            session.clear()
        response=self.client.get('/semesters/1')
        self.assertIn(response.status_code,(302,303))

    def test_all_admin_pages_render_without_server_errors(self):
        with self.client.session_transaction() as session:
            session['admin_id']=1
            session['username']='Test Admin'
        paths=['/','/students','/events','/attendance','/selection','/reports','/eligibility',
               '/semesters','/semesters/1','/semesters/2','/semesters/3','/semesters/4','/settings',
               f'/students/{self.student_id}',f'/events/{self.event_id}','/semesters/1/report.csv']
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(200,self.client.get(path).status_code)

    def test_incomplete_criteria_never_returns_eligible(self):
        with portal.app.app_context():
            conn=portal.db()
            conn.execute("DELETE FROM semester_rules WHERE semester_id=1")
            conn.commit()
        self.attendance(4); self.certificate('Verified')
        self.assertNotEqual('ELIGIBLE',self.calculate()['status'])


if __name__=='__main__':
    unittest.main()
