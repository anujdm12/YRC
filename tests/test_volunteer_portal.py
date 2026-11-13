import io
import os
import re
import tempfile
import unittest
import uuid

from PIL import Image

DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False).name
os.environ['DATABASE_PATH'] = DB_FILE
os.environ['SECRET_KEY'] = 'volunteer-portal-test-secret'
import app as portal
from werkzeug.security import check_password_hash


class VolunteerPortalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.suffix = uuid.uuid4().hex[:10]
        cls.convenor_ids = []
        with portal.app.app_context():
            conn = portal.db()
            cur = conn.execute(
                "INSERT INTO students(register_no,name,department,class_name,year) VALUES(?,?,?,?,1)",
                (f'VP{cls.suffix}', 'Portal Student', 'Health', 'BSc'))
            cls.student_id = cur.lastrowid
            other = conn.execute(
                "INSERT INTO students(register_no,name,department,class_name,year) VALUES(?,?,?,?,1)",
                (f'OTHER{cls.suffix}', 'Other Student', 'Health', 'BSc'))
            cls.other_student_id = other.lastrowid
            own_event = conn.execute(
                "INSERT INTO events(name,event_date,status,semester) VALUES(?,?, 'Completed',1)",
                (f'Own event {cls.suffix}', '2026-01-01'))
            other_event = conn.execute(
                "INSERT INTO events(name,event_date,status,semester) VALUES(?,?, 'Completed',1)",
                (f'Other event {cls.suffix}', '2026-01-02'))
            for event_id, student_id in (
                    (own_event.lastrowid, cls.student_id),
                    (other_event.lastrowid, cls.other_student_id)):
                conn.execute('INSERT INTO event_participants(event_id,student_id) VALUES(?,?)',
                             (event_id, student_id))
                conn.execute(
                    "INSERT INTO attendance(event_id,student_id,status) VALUES(?,?,'Present')",
                    (event_id, student_id))
            conn.commit()
        cls.register_no = f'VP{cls.suffix}'
        cls.other_register_no = f'OTHER{cls.suffix}'

    def setUp(self):
        self.admin_client = portal.app.test_client()
        with self.admin_client.session_transaction() as session:
            session['admin_id'] = 1
            session['username'] = 'Test Admin'
            session['_csrf_token'] = 'test-token'

    @classmethod
    def tearDownClass(cls):
        with portal.app.app_context():
            conn = portal.db()
            for convenor_id in cls.convenor_ids:
                row=conn.execute('SELECT photo FROM student_convenors WHERE id=?',(convenor_id,)).fetchone()
                conn.execute('DELETE FROM student_convenors WHERE id=?',(convenor_id,))
                conn.execute("DELETE FROM admin_audit_logs WHERE entity_type='student_convenor' AND entity_id=?",
                             (convenor_id,))
            for student_id in (cls.student_id, cls.other_student_id):
                conn.execute('DELETE FROM attendance WHERE student_id=?', (student_id,))
                conn.execute('DELETE FROM event_participants WHERE student_id=?', (student_id,))
                conn.execute('DELETE FROM student_accounts WHERE student_id=?', (student_id,))
                conn.execute('DELETE FROM semester_certificates WHERE student_id=?', (student_id,))
                conn.execute('DELETE FROM semester_eligibility_history WHERE student_id=?', (student_id,))
                conn.execute("DELETE FROM admin_audit_logs WHERE entity_type='student' AND entity_id=?",
                             (student_id,))
                conn.execute('DELETE FROM students WHERE id=?', (student_id,))
            conn.execute('DELETE FROM events WHERE name LIKE ?', (f'%{cls.suffix}',))
            conn.execute('DELETE FROM login_attempts WHERE username LIKE ?',
                         (f'student:%{cls.suffix}',))
            conn.commit()

    def issue_password(self):
        response = self.admin_client.post(
            f'/students/{self.student_id}/credentials',
            data={'_csrf_token': 'test-token'})
        self.assertEqual(200, response.status_code)
        self.assertEqual('no-store', response.headers.get('Cache-Control'))
        match = re.search(rb'Password:\s*<strong>([^<]+)</strong>', response.data)
        self.assertIsNotNone(match)
        password = match.group(1).decode()
        self.assertNotIn(password, response.headers.get('Set-Cookie', ''))
        return password

    def sign_in_student(self, password, client=None):
        client = client or portal.app.test_client()
        with client.session_transaction() as session:
            session['_csrf_token'] = 'student-test-token'
        response = client.post('/volunteer', data={
            '_csrf_token': 'student-test-token',
            'register_no': self.register_no,
            'password': password,
        })
        return client, response

    def test_issued_password_is_hashed_and_displayed_only_in_uncached_response(self):
        password = self.issue_password()
        self.assertEqual('KJU', password)
        with portal.app.app_context():
            account = portal.db().execute(
                'SELECT password_hash FROM student_accounts WHERE student_id=?',
                (self.student_id,)).fetchone()
        self.assertTrue(check_password_hash(account['password_hash'], password))
        self.assertNotEqual(password, account['password_hash'])
        self.assertNotIn(password, self.admin_client.get(
            f'/students/{self.student_id}').get_data(as_text=True))

        with self.admin_client.session_transaction() as session:
            self.assertNotIn('issued_student_credential', session)

    def test_student_dashboard_is_private_and_shows_own_attendance(self):
        password = self.issue_password()
        student_client, response = self.sign_in_student(password)
        self.assertEqual(302, response.status_code)
        dashboard = student_client.get('/volunteer/home')
        body = dashboard.get_data(as_text=True)
        self.assertEqual(200, dashboard.status_code)
        self.assertIn(f'Own event {self.suffix}', body)
        self.assertNotIn(f'Other event {self.suffix}', body)
        with student_client.session_transaction() as session:
            self.assertNotIn('admin_id', session)
        self.assertEqual(302, student_client.get('/students').status_code)

    def test_login_matches_admin_sign_in_layout_and_dashboard_has_convenors(self):
        response = portal.app.test_client().get('/volunteer')
        body = response.get_data(as_text=True)
        self.assertEqual(200, response.status_code)
        self.assertIn('class="login-page student-login-page"', body)
        self.assertIn('id="studentPassword"', body)
        self.assertIn('Sign in to my portal', body)

        password = self.issue_password()
        student_client, _ = self.sign_in_student(password)
        body = student_client.get('/volunteer/home').get_data(as_text=True)
        self.assertIn('Student Convenors', body)
        self.assertIn('Saaya Rupesh', body)
        self.assertIn('Jeevan Chandapillai Koshy', body)
        self.assertIn('mailto:24bcyb59@kristujayanti.com', body)

    def test_reset_revokes_old_sessions_and_password_change_updates_current_session(self):
        old_password = self.issue_password()
        old_client, _ = self.sign_in_student(old_password)
        new_password = self.issue_password()
        self.assertEqual('KJU', old_password)
        self.assertEqual('KJU', new_password)
        self.assertEqual(302, old_client.get('/volunteer/home').status_code)

        new_client, response = self.sign_in_student(new_password)
        self.assertEqual(302, response.status_code)
        with new_client.session_transaction() as session:
            csrf_token = session['_csrf_token']
        changed = new_client.post('/volunteer/change-password', data={
            '_csrf_token': csrf_token,
            'new_password': 'changed-test-password',
        })
        self.assertEqual(302, changed.status_code)
        self.assertEqual(200, new_client.get('/volunteer/home').status_code)
        with portal.app.app_context():
            account = portal.db().execute(
                'SELECT password_hash FROM student_accounts WHERE student_id=?',
                (self.student_id,)).fetchone()
        self.assertTrue(check_password_hash(account['password_hash'], 'changed-test-password'))

    def test_student_login_is_rate_limited(self):
        client = portal.app.test_client()
        with client.session_transaction() as session:
            session['_csrf_token'] = 'student-test-token'
        for _ in range(8):
            response = client.post('/volunteer', data={
                '_csrf_token': 'student-test-token',
                'register_no': self.register_no,
                'password': 'incorrect-password',
            })
            self.assertEqual(200, response.status_code)
        response = client.post('/volunteer', data={
            '_csrf_token': 'student-test-token',
            'register_no': self.register_no,
            'password': 'incorrect-password',
        })
        self.assertEqual(429, response.status_code)

    def test_bulk_credentials_use_shared_password_and_are_hashed(self):
        response = portal.app.test_client().get('/students/credentials/bulk')
        self.assertEqual(302, response.status_code)

        response = self.admin_client.get('/students/credentials/bulk')
        self.assertEqual(200, response.status_code)
        self.assertIn(f'{2} student records', response.get_data(as_text=True))
        response = self.admin_client.post('/students/credentials/bulk', data={
            '_csrf_token': 'test-token',
            'confirm': 'issue',
        })
        self.assertEqual(200, response.status_code)
        self.assertEqual('no-store, private', response.headers.get('Cache-Control'))
        passwords = re.findall(rb'<td><code>([^<]+)</code></td>', response.data)
        self.assertEqual(2, len(passwords))
        self.assertEqual([b'KJU', b'KJU'], passwords)
        self.assertNotIn(passwords[0].decode(), response.headers.get('Set-Cookie', ''))

        with portal.app.app_context():
            conn = portal.db()
            accounts = conn.execute('''SELECT s.register_no,a.password_hash FROM student_accounts a
                JOIN students s ON s.id=a.student_id WHERE s.id IN (?,?)''',
                (self.student_id, self.other_student_id)).fetchall()
            self.assertEqual(2, len(accounts))
            stored_hashes = {row['register_no']:row['password_hash'] for row in accounts}
        for password in passwords:
            password = password.decode()
            self.assertEqual('KJU', password)
            self.assertTrue(any(check_password_hash(value, password) for value in stored_hashes.values()))
        self.assertTrue(all(check_password_hash(value, 'KJU') for value in stored_hashes.values()))
        refreshed = self.admin_client.get('/students/credentials/bulk')
        self.assertNotIn(passwords[0].decode(), refreshed.get_data(as_text=True))

    def test_admin_can_add_and_edit_convenor_name_and_photo(self):
        def image_file(color,filename):
            image=io.BytesIO()
            Image.new('RGB',(16,16),color).save(image,format='PNG')
            image.seek(0)
            return image,filename

        response=self.admin_client.get('/convenors')
        self.assertEqual(200,response.status_code)
        management_html=response.get_data(as_text=True)
        self.assertIn('name="coordinator_title"',management_html)
        self.assertNotIn('name="campus"',management_html)
        self.assertNotIn('name="campus_label"',management_html)
        first_photo,first_name=image_file('red','lead.png')
        response=self.admin_client.post('/convenors',data={
            '_csrf_token':'test-token',
            'action':'add',
            'name':'Temporary Test Lead',
            'role':'STUDENT LEAD',
            'coordinator_title':'Student Coordinator',
            'phone':'+91 1234567890',
            'email':'lead@example.edu',
            'photo':(first_photo,first_name),
        },content_type='multipart/form-data')
        self.assertEqual(302,response.status_code)
        with portal.app.app_context():
            conn=portal.db()
            lead=conn.execute("SELECT * FROM student_convenors WHERE name='Temporary Test Lead'").fetchone()
            self.assertIsNotNone(lead)
            self.convenor_id=lead['id']
            self.convenor_ids.append(self.convenor_id)
            original_photo=lead['photo']
            original_photo_data=bytes(lead['photo_data'])
        self.assertTrue(original_photo.startswith('uploads/convenors/'))

        replacement_photo,replacement_name=image_file('blue','replacement.png')
        response=self.admin_client.post('/convenors',data={
            '_csrf_token':'test-token',
            'action':'save',
            'id':str(self.convenor_id),
            'name':'Renamed Test Lead',
            'role':'CAMPUS COORDINATOR',
            'coordinator_title':'Department Convenor',
            'phone':'+91 1234567890',
            'email':'lead@example.edu',
            'sort_order':'99',
            'active':'1',
            'photo':(replacement_photo,replacement_name),
        },content_type='multipart/form-data')
        self.assertEqual(302,response.status_code)
        with portal.app.app_context():
            lead=portal.db().execute('SELECT * FROM student_convenors WHERE id=?',(self.convenor_id,)).fetchone()
            account_token=portal.secrets.token_urlsafe(32)
            portal.db().execute('''INSERT INTO student_accounts(student_id,password_hash,session_token)
                VALUES(?,?,?) ON CONFLICT(student_id) DO UPDATE SET session_token=excluded.session_token''',
                (self.student_id,portal.generate_password_hash('convenor-test-password'),account_token))
            portal.db().commit()
        self.assertEqual('Renamed Test Lead',lead['name'])
        self.assertEqual('Department Convenor',lead['coordinator_title'])
        self.assertEqual('',lead['campus'])
        self.assertEqual('',lead['campus_label'])
        self.assertTrue(lead['photo'].startswith('uploads/convenors/'))
        self.assertNotEqual(original_photo,lead['photo'])
        self.assertNotEqual(original_photo_data,bytes(lead['photo_data']))

        unauthenticated=portal.app.test_client().get(
            '/volunteer/convenor-photos/'+lead['photo'].rsplit('/',1)[-1])
        self.assertEqual(401,unauthenticated.status_code)
        student_client=portal.app.test_client()
        with student_client.session_transaction() as session:
            session['student_id']=self.student_id
            session['student_session_token']=account_token
            session['_csrf_token']='student-test-token'
        dashboard=student_client.get('/volunteer/home')
        self.assertIn('Renamed Test Lead',dashboard.get_data(as_text=True))
        photo_url='/volunteer/convenor-photos/'+lead['photo'].rsplit('/',1)[-1]
        self.assertEqual(200,student_client.get(photo_url).status_code)
        self.assertEqual(200,self.admin_client.get(
            photo_url).status_code)

if __name__ == '__main__':
    unittest.main()
