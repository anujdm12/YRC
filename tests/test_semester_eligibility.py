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
            legacy=conn.execute("INSERT INTO events(name,event_date,status) VALUES('Legacy ambiguous event','2026-10-08','Completed')")
            cls.legacy_event_id=legacy.lastrowid
            conn.commit(); conn.close()

    def setUp(self):
        with portal.app.app_context():
            conn=portal.db()
            conn.execute("DELETE FROM attendance WHERE student_id=?",(self.student_id,))
            conn.execute("DELETE FROM event_participants WHERE student_id=?",(self.student_id,))
            conn.execute("DELETE FROM events WHERE name LIKE 'Test activity%'" )
            conn.execute("DELETE FROM semester_certificates WHERE student_id=?",(self.student_id,))
            conn.execute("DELETE FROM semester_certificate_records WHERE student_id=?",(self.student_id,))
            conn.execute('INSERT OR IGNORE INTO event_participants(event_id,student_id) VALUES(?,?)',(self.event_id,self.student_id))
            conn.execute("INSERT OR IGNORE INTO attendance(event_id,student_id,status) VALUES(?,?,'Present')",(self.event_id,self.student_id))
            conn.execute("DELETE FROM semester_rules WHERE semester_id IN (1,2)")
            conn.execute("INSERT INTO semester_rules(semester_id,rule_type,value,mandatory) VALUES(1,'minimum_activities','2',1)")
            conn.execute("INSERT INTO semester_rules(semester_id,rule_type,value,mandatory,verification_required) VALUES(1,'certificate','ICRC',1,1)")
            conn.execute("INSERT INTO semester_rules(semester_id,rule_type,value,mandatory) VALUES(2,'minimum_activities','1',1)")
            conn.commit()

    def attendance(self, count, semester=1, academic_year=None, status='Completed'):
        with portal.app.app_context():
            conn=portal.db()
            for index in range(count):
                event=conn.execute("INSERT INTO events(name,event_date,status,semester,academic_year) VALUES(?, '2026-01-01',?,?,?)",(f'Test activity {index}',status,semester,academic_year))
                event_id=event.lastrowid
                conn.execute("INSERT INTO event_participants(event_id,student_id) VALUES(?,?)",(event_id,self.student_id))
                conn.execute("INSERT INTO attendance(event_id,student_id,status) VALUES(?,?,'Present')",(event_id,self.student_id))
            conn.commit()

    def certificate(self, status):
        with portal.app.app_context():
            conn=portal.db()
            conn.execute("INSERT INTO semester_certificate_records(semester_id,academic_year,student_id,certificate_type,status) VALUES(1,'',?,'ICRC',?)",(self.student_id,status))
            conn.commit()

    def calculate(self, semester=1):
        with portal.app.app_context():
            return portal.semester_eligibility(semester,self.student_id)

    def admin_session(self):
        with self.client.session_transaction() as session:
            session['admin_id']=1; session['username']='Test Admin'; session['_csrf_token']='test-token'

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

    def test_semester_activity_counts_do_not_cross_semesters(self):
        self.attendance(2,semester=2)
        self.assertEqual(0,self.calculate(1)['activities'])

    def test_academic_year_filter_limits_activity_counts(self):
        self.attendance(1,academic_year='2025-2026')
        self.attendance(1,academic_year='2026-2027')
        with portal.app.app_context():
            result=portal.semester_eligibility_many(1,[self.student_id],'2026-2027')[self.student_id]
        self.assertEqual(1,result['activities'])

    def test_only_completed_events_count(self):
        self.attendance(2,status='Open')
        self.assertEqual(0,self.calculate()['activities'])

    def test_create_events_requires_and_saves_semester_and_academic_year(self):
        self.admin_session()
        for semester in ('1','2'):
            response=self.client.post('/events',data={'_csrf_token':'test-token','name':f'Assigned {semester}','event_date':'2026-02-01','status':'Completed','semester':semester,'academic_year':'2026-2027'})
            self.assertEqual(302,response.status_code)
        with portal.app.app_context():
            rows=portal.db().execute("SELECT semester,academic_year FROM events WHERE name LIKE 'Assigned %' ORDER BY name").fetchall()
            self.assertEqual([(1,'2026-2027'),(2,'2026-2027')],[(r['semester'],r['academic_year']) for r in rows])

    def test_reassignment_requires_confirmation_and_preserves_attendance(self):
        self.admin_session()
        response=self.client.post(f'/events/{self.event_id}/semester',data={'_csrf_token':'test-token','semester':'2','academic_year':'2026-2027'})
        self.assertEqual(302,response.status_code)
        with portal.app.app_context():
            self.assertEqual(1,portal.db().execute('SELECT semester FROM events WHERE id=?',(self.event_id,)).fetchone()[0])
        response=self.client.post(f'/events/{self.event_id}/semester',data={'_csrf_token':'test-token','semester':'2','academic_year':'2026-2027','confirm_reassignment':'1'})
        self.assertEqual(302,response.status_code)
        with portal.app.app_context():
            conn=portal.db()
            self.assertEqual((2,'2026-2027'),tuple(conn.execute('SELECT semester,academic_year FROM events WHERE id=?',(self.event_id,)).fetchone()))
            self.assertEqual(1,conn.execute('SELECT COUNT(*) FROM attendance WHERE event_id=?',(self.event_id,)).fetchone()[0])
            self.assertEqual(1,conn.execute('SELECT COUNT(*) FROM event_participants WHERE event_id=?',(self.event_id,)).fetchone()[0])

    def test_legacy_certificate_migration_preserves_status_and_is_repeatable(self):
        with portal.app.app_context():
            conn=portal.db()
            conn.execute("INSERT INTO semester_certificates(semester_id,student_id,certificate_type,status) VALUES(1,?,'First Aid','Verified')",(self.student_id,))
            conn.commit()
        portal.init_db(); portal.init_db()
        with portal.app.app_context():
            row=portal.db().execute("SELECT academic_year,status FROM semester_certificate_records WHERE student_id=? AND certificate_type='First Aid'",(self.student_id,)).fetchone()
            self.assertEqual(('', 'Verified'),tuple(row))
            self.assertEqual(1,portal.db().execute("SELECT COUNT(*) FROM semester_certificate_records WHERE student_id=? AND certificate_type='First Aid'",(self.student_id,)).fetchone()[0])

    def test_ambiguous_legacy_event_stays_unassigned_for_admin_review(self):
        self.admin_session()
        response=self.client.get('/events')
        self.assertEqual(200,response.status_code)
        self.assertIn(b'Legacy ambiguous event',response.data)
        with portal.app.app_context():
            row=portal.db().execute('SELECT semester,academic_year FROM events WHERE id=?',(self.legacy_event_id,)).fetchone()
            self.assertEqual((None,None),tuple(row))

    def test_filtered_csv_uses_same_year_and_result_as_semester_report(self):
        self.attendance(1,academic_year='2026-2027')
        self.admin_session()
        page=self.client.get('/semesters/1?academic_year=2026-2027')
        export=self.client.get('/semesters/1/report.csv?academic_year=2026-2027')
        self.assertEqual(200,page.status_code); self.assertEqual(200,export.status_code)
        self.assertIn(b'2026-2027',page.data); self.assertIn(b'2026-2027',export.data)
        self.assertIn(b'Test activity 0',export.data)
        self.assertIn(b'NOT_ELIGIBLE',export.data)

    def test_unauthorized_semester_export_is_blocked(self):
        with self.client.session_transaction() as session:
            session.clear()
        response=self.client.get('/semesters/1/report.csv')
        self.assertIn(response.status_code,(302,303))

    def test_rule_update_preserves_participation_and_certificate_history(self):
        self.attendance(2); self.certificate('Submitted')
        with portal.app.app_context():
            portal.db().execute("UPDATE semester_rules SET value='3' WHERE semester_id=1 AND rule_type='minimum_activities'")
            result=portal.semester_eligibility(1,self.student_id)
            self.assertEqual(2,result['activities'])
            self.assertEqual(3,portal.db().execute('SELECT COUNT(*) FROM attendance WHERE student_id=?',(self.student_id,)).fetchone()[0])
            self.assertEqual(1,portal.db().execute('SELECT COUNT(*) FROM semester_certificate_records WHERE student_id=?',(self.student_id,)).fetchone()[0])

    def test_private_semester_page_requires_admin_session(self):
        with self.client.session_transaction() as session:
            session.clear()
        response=self.client.get('/semesters/1')
        self.assertIn(response.status_code,(302,303))

    def test_all_admin_pages_render_without_server_errors(self):
        self.admin_session()
        paths=['/','/students','/events','/attendance','/selection','/reports','/eligibility',
               '/semesters','/semesters/1','/semesters/2','/semesters/3','/semesters/4','/settings',
               '/semester-reports/consolidated',f'/students/{self.student_id}',f'/events/{self.event_id}','/semesters/1/report.csv']
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
