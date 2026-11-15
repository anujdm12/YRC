import os
import secrets
import time
import click
import csv
import io
import json
import re
import unicodedata
from datetime import datetime, date
from functools import wraps
from flask import Flask, g, render_template, request, redirect, url_for, session, flash, jsonify, Response
from PIL import Image, ImageOps, UnidentifiedImageError
from werkzeug.security import generate_password_hash, check_password_hash
from database import connect_db, DB_ERROR, DB_INTEGRITY_ERROR, DB_IMPORT_ERROR, USING_POSTGRES
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from urllib.request import Request, urlopen

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY') or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
                  SESSION_COOKIE_SECURE=os.getenv('COOKIE_SECURE', '0') == '1')
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024
DB_PATH = os.getenv('DATABASE_PATH', 'yrc.db')
ELIGIBILITY_EVENT_THRESHOLD = 3
@app.context_processor
def inject_now():
    today=date.today()
    return {'now': today.strftime('%d %b %Y'), 'today_long': today.strftime('%A, %d %B %Y').upper()}

@app.template_filter('display_batch')
def display_batch(value):
    label=str(value or '')
    return '2026 batch' if label.strip().casefold()=='current batch' else label

SCHEMA = '''
CREATE TABLE IF NOT EXISTS admins(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, display_name TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active', last_login TEXT, session_token TEXT NOT NULL DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS admin_roles(admin_id INTEGER PRIMARY KEY REFERENCES admins(id) ON DELETE CASCADE, role TEXT NOT NULL DEFAULT 'super_admin', department TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS departments(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, code TEXT NOT NULL UNIQUE, leader_admin_id INTEGER REFERENCES admins(id) ON DELETE SET NULL, status TEXT NOT NULL DEFAULT 'active', created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS admin_department_assignments(id INTEGER PRIMARY KEY, admin_id INTEGER NOT NULL REFERENCES admins(id) ON DELETE CASCADE, department_id INTEGER NOT NULL REFERENCES departments(id) ON DELETE CASCADE, role TEXT NOT NULL DEFAULT 'department_admin', can_manage_volunteers INTEGER NOT NULL DEFAULT 0, can_manage_events INTEGER NOT NULL DEFAULT 0, can_view_reports INTEGER NOT NULL DEFAULT 1, created_at TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(admin_id,department_id));
CREATE TABLE IF NOT EXISTS cohorts(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, admission_year INTEGER, status TEXT NOT NULL DEFAULT 'active', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS cohort_departments(cohort_id INTEGER NOT NULL REFERENCES cohorts(id) ON DELETE CASCADE, department_id INTEGER NOT NULL REFERENCES departments(id) ON DELETE CASCADE, created_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(cohort_id,department_id));
CREATE TABLE IF NOT EXISTS cohort_prefix_mappings(prefix TEXT PRIMARY KEY, cohort_id INTEGER NOT NULL REFERENCES cohorts(id), active INTEGER NOT NULL DEFAULT 1, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS students(id INTEGER PRIMARY KEY, register_no TEXT UNIQUE NOT NULL, name TEXT NOT NULL, department TEXT NOT NULL, department_id INTEGER REFERENCES departments(id), department_verification TEXT NOT NULL DEFAULT 'pending', class_name TEXT NOT NULL DEFAULT '', section TEXT NOT NULL DEFAULT '', year INTEGER NOT NULL, email TEXT, phone TEXT, yrc_status TEXT NOT NULL DEFAULT 'Active', cohort TEXT NOT NULL DEFAULT '2026 batch', cohort_id INTEGER REFERENCES cohorts(id), cohort_verification TEXT NOT NULL DEFAULT 'pending', created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS department_transfers(id INTEGER PRIMARY KEY, student_id INTEGER NOT NULL REFERENCES students(id), from_department_id INTEGER REFERENCES departments(id), to_department_id INTEGER NOT NULL REFERENCES departments(id), approved_by INTEGER REFERENCES admins(id), performed_by INTEGER REFERENCES admins(id), reason TEXT NOT NULL DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS student_accounts(id INTEGER PRIMARY KEY, student_id INTEGER UNIQUE NOT NULL REFERENCES students(id) ON DELETE CASCADE, password_hash TEXT NOT NULL, session_token TEXT NOT NULL DEFAULT '', updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT, event_date TEXT NOT NULL, start_time TEXT, end_time TEXT, venue TEXT, max_participants INTEGER, deadline TEXT, status TEXT NOT NULL DEFAULT 'Upcoming', semester INTEGER, counts_for_eligibility INTEGER NOT NULL DEFAULT 1, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS event_departments(event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE, department_id INTEGER NOT NULL REFERENCES departments(id), assigned_by INTEGER REFERENCES admins(id), created_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(event_id,department_id));
CREATE TABLE IF NOT EXISTS event_participants(id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE, student_id INTEGER NOT NULL REFERENCES students(id), selection_status TEXT NOT NULL DEFAULT 'Selected', selected_at TEXT DEFAULT CURRENT_TIMESTAMP, selected_by INTEGER, UNIQUE(event_id,student_id));
CREATE TABLE IF NOT EXISTS selection_history(id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL, student_id INTEGER NOT NULL, decision TEXT NOT NULL, attended_count INTEGER NOT NULL DEFAULT 0, seat_offers INTEGER NOT NULL DEFAULT 0, priority_score INTEGER NOT NULL DEFAULT 0, tie_break TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '', decided_by INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS attendance(id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL, student_id INTEGER NOT NULL, status TEXT NOT NULL, marked_at TEXT DEFAULT CURRENT_TIMESTAMP, marked_by INTEGER, credit_points INTEGER NOT NULL DEFAULT 0, UNIQUE(event_id,student_id), FOREIGN KEY(event_id,student_id) REFERENCES event_participants(event_id,student_id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS login_attempts(id INTEGER PRIMARY KEY, username TEXT NOT NULL, attempted_at INTEGER NOT NULL, success INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS admin_audit_logs(id INTEGER PRIMARY KEY, admin_id INTEGER, action TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id INTEGER, old_value TEXT, new_value TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS participation_import_batches(id TEXT PRIMARY KEY, admin_id INTEGER NOT NULL, filename TEXT NOT NULL, response_count INTEGER NOT NULL DEFAULT 0, unique_respondents INTEGER NOT NULL DEFAULT 0, repeated_respondents INTEGER NOT NULL DEFAULT 0, missing_register_count INTEGER NOT NULL DEFAULT 0, record_count INTEGER NOT NULL DEFAULT 0, imported_count INTEGER NOT NULL DEFAULT 0, skipped_count INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'Preview', created_at TEXT DEFAULT CURRENT_TIMESTAMP, completed_at TEXT);
CREATE TABLE IF NOT EXISTS participation_import_rows(id INTEGER PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES participation_import_batches(id) ON DELETE CASCADE, source_row INTEGER NOT NULL, register_no TEXT NOT NULL, student_name TEXT NOT NULL, event_name TEXT NOT NULL, student_id INTEGER, error TEXT NOT NULL DEFAULT '', match_method TEXT NOT NULL DEFAULT 'register_no', UNIQUE(batch_id,source_row,event_name));
CREATE TABLE IF NOT EXISTS semesters(id INTEGER PRIMARY KEY, name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, locked INTEGER NOT NULL DEFAULT 0, academic_year TEXT NOT NULL DEFAULT '', updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS semester_rules(id INTEGER PRIMARY KEY, semester_id INTEGER NOT NULL REFERENCES semesters(id) ON DELETE CASCADE, rule_type TEXT NOT NULL, value TEXT NOT NULL DEFAULT '', mandatory INTEGER NOT NULL DEFAULT 1, verification_required INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1, UNIQUE(semester_id,rule_type));
CREATE TABLE IF NOT EXISTS semester_certificates(id INTEGER PRIMARY KEY, semester_id INTEGER NOT NULL REFERENCES semesters(id), student_id INTEGER NOT NULL REFERENCES students(id), certificate_type TEXT NOT NULL, reference TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'Submitted', verified_by INTEGER, verified_at TEXT DEFAULT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(semester_id,student_id,certificate_type));
CREATE TABLE IF NOT EXISTS semester_eligibility_history(id INTEGER PRIMARY KEY, semester_id INTEGER NOT NULL REFERENCES semesters(id), student_id INTEGER NOT NULL REFERENCES students(id), status TEXT NOT NULL, reasons TEXT NOT NULL DEFAULT '', calculated_at TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(semester_id,student_id));
CREATE TABLE IF NOT EXISTS student_convenors(id INTEGER PRIMARY KEY, sort_order INTEGER NOT NULL DEFAULT 0, name TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'STUDENT LEAD', department_id INTEGER REFERENCES departments(id), phone TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '', photo TEXT NOT NULL DEFAULT '', photo_data BYTEA, active INTEGER NOT NULL DEFAULT 1, coordinator_title TEXT NOT NULL DEFAULT 'Student Coordinator', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS department_leadership_appointments(id INTEGER PRIMARY KEY, department_id INTEGER NOT NULL REFERENCES departments(id), leadership_role TEXT NOT NULL, name TEXT NOT NULL, designation TEXT NOT NULL, phone TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '', photo TEXT NOT NULL DEFAULT '', photo_data BYTEA, status TEXT NOT NULL DEFAULT 'active', active_from TEXT NOT NULL DEFAULT '', active_until TEXT, appointed_by INTEGER REFERENCES admins(id), updated_at TEXT DEFAULT CURRENT_TIMESTAMP, created_at TEXT DEFAULT CURRENT_TIMESTAMP, CHECK(leadership_role IN ('department_leader','department_secretary')), CHECK(status IN ('active','retired')));
'''

def db():
    if 'db' not in g:
        g.db = connect_db(DB_PATH)
    return g.db

def department_for_label(conn, label):
    """Return a registered department for an exact legacy display label."""
    return conn.execute('SELECT id,name FROM departments WHERE name=? AND status=\'active\'',
                        (' '.join((label or '').split()).strip(),)).fetchone()

@app.teardown_appcontext
def close_db(_):
    conn = g.pop('db', None)
    if conn: conn.close()

def process_convenor_photo(upload):
    if not upload or not upload.filename:
        return None
    content=upload.stream.read(5*1024*1024+1)
    if len(content)>5*1024*1024:
        raise ValueError('Choose an image smaller than 5 MB.')
    try:
        with Image.open(io.BytesIO(content)) as image:
            if image.format not in ('JPEG','PNG','WEBP'):
                raise ValueError('Use a JPG, PNG, or WebP image.')
            if image.width*image.height>25_000_000:
                raise ValueError('Choose an image with fewer than 25 megapixels.')
            image.verify()
        with Image.open(io.BytesIO(content)) as image:
            image=ImageOps.exif_transpose(image)
            image.thumbnail((1400,1400),Image.Resampling.LANCZOS)
            if 'A' in image.getbands():
                rgba=image.convert('RGBA')
                normalized=Image.new('RGB',rgba.size,'white')
                normalized.paste(rgba,mask=rgba.getchannel('A'))
            else:
                normalized=image.convert('RGB')
            output=io.BytesIO()
            normalized.save(output,format='JPEG',quality=88,optimize=True)
    except (UnidentifiedImageError,OSError,Image.DecompressionBombError) as exc:
        raise ValueError('The uploaded file is not a valid supported image.') from exc
    filename=secrets.token_hex(16)+'.jpg'
    return 'uploads/convenors/'+filename,output.getvalue()

def init_db():
    conn = connect_db(DB_PATH)
    conn.executescript(SCHEMA)
    admin_columns={row[1] for row in conn.execute('PRAGMA table_info(admins)')}
    for column,definition in (
        ('display_name',"TEXT NOT NULL DEFAULT ''"),('email',"TEXT NOT NULL DEFAULT ''"),
        ('status',"TEXT NOT NULL DEFAULT 'active'"),('last_login','TEXT'),
        ('session_token',"TEXT NOT NULL DEFAULT ''")):
        if column not in admin_columns:
            conn.execute(f'ALTER TABLE admins ADD COLUMN {column} {definition}')
    for admin in conn.execute("SELECT id FROM admins WHERE session_token='' OR session_token IS NULL").fetchall():
        conn.execute('UPDATE admins SET session_token=? WHERE id=?',(secrets.token_urlsafe(32),admin['id']))
    convenor_columns={row[1] for row in conn.execute('PRAGMA table_info(student_convenors)')}
    if 'photo_data' not in convenor_columns:
        conn.execute('ALTER TABLE student_convenors ADD COLUMN photo_data BYTEA')
    if 'coordinator_title' not in convenor_columns:
        conn.execute("ALTER TABLE student_convenors ADD COLUMN coordinator_title TEXT NOT NULL DEFAULT 'Student Coordinator'")
    if 'department_id' not in convenor_columns:
        department_id_type='BIGINT' if USING_POSTGRES else 'INTEGER'
        conn.execute(f'ALTER TABLE student_convenors ADD COLUMN department_id {department_id_type} REFERENCES departments(id)')
    if {'campus', 'campus_label'} <= convenor_columns:
        conn.execute("UPDATE student_convenors SET campus='', campus_label='' WHERE campus<>'' OR campus_label<>''")
    leadership_columns={row[1] for row in conn.execute('PRAGMA table_info(department_leadership_appointments)')}
    if 'leadership_role' not in leadership_columns:
        conn.execute("ALTER TABLE department_leadership_appointments ADD COLUMN leadership_role TEXT NOT NULL DEFAULT 'department_leader'")
        if 'position' in leadership_columns:
            conn.execute("UPDATE department_leadership_appointments SET leadership_role=CASE WHEN LOWER(position) LIKE '%secretar%' THEN 'department_secretary' ELSE 'department_leader' END")
    for semester_number in range(1, 5):
        conn.execute('INSERT OR IGNORE INTO semesters(id,name) VALUES(?,?)', (semester_number, f'Semester {semester_number}'))
    conn.execute("INSERT OR IGNORE INTO semester_rules(semester_id,rule_type,value,mandatory,verification_required) VALUES(1,'minimum_activities','2',1,0)")
    student_columns = {row[1] for row in conn.execute('PRAGMA table_info(students)')}
    if 'department_id' not in student_columns:
        department_id_type='BIGINT' if USING_POSTGRES else 'INTEGER'
        conn.execute(f'ALTER TABLE students ADD COLUMN department_id {department_id_type} REFERENCES departments(id)')
    if 'department_verification' not in student_columns:
        conn.execute("ALTER TABLE students ADD COLUMN department_verification TEXT NOT NULL DEFAULT 'pending'")
    # Create registry entries from the exact legacy labels. Do not infer or rewrite
    # volunteer identity; rows with a blank/unknown label remain unassigned.
    for department_row in conn.execute("SELECT DISTINCT TRIM(department) AS name FROM students WHERE TRIM(department)<>'' ORDER BY 1").fetchall():
        name=department_row['name']
        existing=conn.execute('SELECT id FROM departments WHERE name=?',(name,)).fetchone()
        if not existing:
            cur=conn.execute("INSERT INTO departments(name,code,status) VALUES(?,?,'active')",(name,'D'+secrets.token_hex(4).upper()))
            department_id=cur.lastrowid
        else:
            department_id=existing['id']
        conn.execute('UPDATE students SET department_id=? WHERE department_id IS NULL AND TRIM(department)=?',(department_id,name))
    conn.execute("UPDATE students SET department_verification='verified' WHERE department_id IS NOT NULL AND department_verification='pending'")
    conn.execute('CREATE INDEX IF NOT EXISTS idx_students_department_id ON students(department_id)')
    conn.execute('''INSERT OR IGNORE INTO cohort_departments(cohort_id,department_id)
        SELECT DISTINCT cohort_id,department_id FROM students WHERE cohort_id IS NOT NULL AND department_id IS NOT NULL''')
    if 'class_name' not in student_columns:
        conn.execute("ALTER TABLE students ADD COLUMN class_name TEXT NOT NULL DEFAULT ''")
    if 'section' not in student_columns:
        conn.execute("ALTER TABLE students ADD COLUMN section TEXT NOT NULL DEFAULT ''")
    if 'cohort' not in student_columns:
        conn.execute("ALTER TABLE students ADD COLUMN cohort TEXT NOT NULL DEFAULT '2026 batch'")
    if 'cohort_id' not in student_columns:
        cohort_id_type='BIGINT' if USING_POSTGRES else 'INTEGER'
        conn.execute(f'ALTER TABLE students ADD COLUMN cohort_id {cohort_id_type} REFERENCES cohorts(id)')
    if 'cohort_verification' not in student_columns:
        conn.execute("ALTER TABLE students ADD COLUMN cohort_verification TEXT NOT NULL DEFAULT 'pending'")
    conn.execute('CREATE INDEX IF NOT EXISTS idx_students_cohort_id ON students(cohort_id)')
    for cohort_row in conn.execute("SELECT DISTINCT TRIM(cohort) AS name FROM students WHERE TRIM(cohort)<>''").fetchall():
        conn.execute("INSERT OR IGNORE INTO cohorts(name,status) VALUES(?,'active')",(cohort_row['name'],))
    conn.execute('''UPDATE students SET cohort_id=(SELECT c.id FROM cohorts c WHERE c.name=students.cohort)
        WHERE cohort_id IS NULL AND EXISTS(SELECT 1 FROM cohorts c WHERE c.name=students.cohort)''')
    legacy_batch=conn.execute("SELECT id FROM cohorts WHERE LOWER(TRIM(name))='current batch'").fetchone()
    named_batch=conn.execute("SELECT id FROM cohorts WHERE LOWER(TRIM(name))='2026 batch'").fetchone()
    if legacy_batch and not named_batch:
        conn.execute("UPDATE cohorts SET name='2026 batch',admission_year=COALESCE(admission_year,2026) WHERE id=?",(legacy_batch['id'],))
    conn.execute("UPDATE students SET cohort='2026 batch' WHERE LOWER(TRIM(cohort))='current batch'")
    for prefix, admission_year in (('26',2026),('27',2027),('28',2028)):
        label=f'{admission_year} Cohort'
        conn.execute("INSERT OR IGNORE INTO cohorts(name,admission_year,status) VALUES(?,?,'active')",(label,admission_year))
        conn.execute('UPDATE cohorts SET admission_year=? WHERE name=? AND admission_year IS NULL',(admission_year,label))
        cohort_row=conn.execute('SELECT id FROM cohorts WHERE name=?',(label,)).fetchone()
        conn.execute('INSERT OR IGNORE INTO cohort_prefix_mappings(prefix,cohort_id,active) VALUES(?,?,1)',(prefix,cohort_row['id']))
    account_columns={row[1] for row in conn.execute('PRAGMA table_info(student_accounts)')}
    if 'session_token' not in account_columns:
        conn.execute("ALTER TABLE student_accounts ADD COLUMN session_token TEXT NOT NULL DEFAULT ''")
    for account in conn.execute("SELECT student_id FROM student_accounts WHERE session_token='' OR session_token IS NULL").fetchall():
        conn.execute('UPDATE student_accounts SET session_token=? WHERE student_id=?',(secrets.token_urlsafe(32),account['student_id']))
    event_columns={row[1] for row in conn.execute('PRAGMA table_info(events)')}
    if 'semester' not in event_columns:
        conn.execute('ALTER TABLE events ADD COLUMN semester INTEGER')
    if 'counts_for_eligibility' not in event_columns:
        conn.execute('ALTER TABLE events ADD COLUMN counts_for_eligibility INTEGER NOT NULL DEFAULT 1')
    attendance_columns={row[1] for row in conn.execute('PRAGMA table_info(attendance)')}
    if 'credit_points' not in attendance_columns:
        conn.execute('ALTER TABLE attendance ADD COLUMN credit_points INTEGER NOT NULL DEFAULT 0')
    import_row_columns={row[1] for row in conn.execute('PRAGMA table_info(participation_import_rows)')}
    if 'match_method' not in import_row_columns:
        conn.execute("ALTER TABLE participation_import_rows ADD COLUMN match_method TEXT NOT NULL DEFAULT 'register_no'")
    # Older records stored department and program together, separated by an em dash.
    if USING_POSTGRES:
        conn.execute("UPDATE students SET class_name=TRIM(SUBSTRING(department FROM POSITION('—' IN department) + 1)), department=TRIM(SUBSTRING(department FROM 1 FOR POSITION('—' IN department) - 1)) WHERE class_name='' AND POSITION('—' IN department) > 0")
    else:
        conn.execute("UPDATE students SET class_name=TRIM(SUBSTR(department, INSTR(department, '—') + 1)), department=TRIM(SUBSTR(department, 1, INSTR(department, '—') - 1)) WHERE class_name='' AND INSTR(department, '—') > 0")
    if USING_POSTGRES:
        conn.execute("INSERT INTO admin_roles(admin_id,role,department) SELECT id,'super_admin','' FROM admins ON CONFLICT (admin_id) DO NOTHING")
    else:
        conn.execute("INSERT OR IGNORE INTO admin_roles(admin_id,role,department) SELECT id,'super_admin','' FROM admins")
    conn.execute("UPDATE admin_roles SET role='super_admin' WHERE role NOT IN ('super_admin','department_admin')")
    conn.execute("DELETE FROM admin_department_assignments WHERE admin_id IN (SELECT admin_id FROM admin_roles WHERE role<>'department_admin')")
    for role_row in conn.execute("SELECT ar.admin_id,ar.department FROM admin_roles ar WHERE ar.role='department_admin' AND NOT EXISTS (SELECT 1 FROM admin_department_assignments x WHERE x.admin_id=ar.admin_id)").fetchall():
        department=conn.execute("SELECT id FROM departments WHERE name=? AND status='active'",(role_row['department'],)).fetchone()
        if department:
            conn.execute('''INSERT OR IGNORE INTO admin_department_assignments
                (admin_id,department_id,role,can_manage_volunteers,can_manage_events,can_view_reports)
                VALUES(?,?,'department_admin',1,1,0)''',(role_row['admin_id'],department['id']))
        else:
            conn.execute("UPDATE admin_roles SET role='super_admin',department='' WHERE admin_id=?",(role_row['admin_id'],))
    conn.execute('''INSERT OR IGNORE INTO event_departments(event_id,department_id)
        SELECT DISTINCT p.event_id,s.department_id FROM event_participants p
        JOIN students s ON s.id=p.student_id WHERE s.department_id IS NOT NULL''')
    bootstrap_username=os.getenv('INITIAL_ADMIN_USERNAME','').strip()
    bootstrap_password=os.getenv('INITIAL_ADMIN_PASSWORD','')
    if bool(bootstrap_username) != bool(bootstrap_password):
        conn.close()
        raise RuntimeError('Set both INITIAL_ADMIN_USERNAME and INITIAL_ADMIN_PASSWORD to bootstrap the first admin.')
    if bootstrap_username and len(bootstrap_password) < 8:
        conn.close()
        raise RuntimeError('INITIAL_ADMIN_PASSWORD must contain at least 8 characters.')
    if bootstrap_username and not conn.execute('SELECT 1 FROM admins LIMIT 1').fetchone():
        cur=conn.execute('INSERT INTO admins(username,password_hash,session_token) VALUES(?,?,?)',(bootstrap_username,generate_password_hash(bootstrap_password),secrets.token_urlsafe(32)))
        conn.execute("INSERT INTO admin_roles(admin_id,role,department) VALUES(?,'super_admin','')",(cur.lastrowid,))
    conn.commit(); conn.close()

# Initialize schema for both `python app.py` and Flask/WSGI launchers.
init_db()

@app.cli.command('create-admin')
@click.option('--username', prompt=True)
@click.option('--password', prompt=True, hide_input=True, confirmation_prompt=True)
def create_admin(username, password):
    """Create the initial administrator without a built-in credential."""
    if len(password) < 8:
        raise click.ClickException('Use a password with at least 8 characters.')
    conn=connect_db(DB_PATH)
    try:
        cur=conn.execute('INSERT INTO admins(username,password_hash,session_token) VALUES(?,?,?)',(username.strip(),generate_password_hash(password),secrets.token_urlsafe(32)))
        conn.execute("INSERT INTO admin_roles(admin_id,role,department) VALUES(?,'super_admin','')",(cur.lastrowid,))
        conn.commit()
    except DB_INTEGRITY_ERROR as exc:
        raise click.ClickException('That username already exists.') from exc
    finally: conn.close()

@app.cli.command('set-admin-password')
@click.option('--username', prompt=True)
@click.option('--password', prompt=True, hide_input=True, confirmation_prompt=True)
def set_admin_password(username, password):
    """Reset an existing administrator password."""
    if len(password) < 8:
        raise click.ClickException('Use a password with at least 8 characters.')
    conn=connect_db(DB_PATH)
    cur=conn.execute('UPDATE admins SET password_hash=?,session_token=? WHERE username=?',(generate_password_hash(password),secrets.token_urlsafe(32),username.strip()))
    conn.commit(); conn.close()
    if cur.rowcount != 1: raise click.ClickException('Administrator username was not found.')

@app.before_request
def csrf_protect():
    if request.method == 'POST':
        expected=session.get('_csrf_token','')
        supplied=request.form.get('_csrf_token','')
        if not expected or not secrets.compare_digest(expected,supplied):
            return 'Invalid or expired form token. Reload the page and try again.', 400

@app.context_processor
def csrf_context():
    if '_csrf_token' not in session: session['_csrf_token']=secrets.token_urlsafe(32)
    return {'csrf_token':session['_csrf_token']}

DEPARTMENT_ADMIN_ENDPOINTS={
    'dashboard','students','student_detail','student_edit','student_status','student_delete','student_remove',
    'import_students','issue_student_credentials','cohort_management','cohort_create','student_cohort_assign',
    'events','event_detail','event_eligibility_update','event_delete','event_semester_update','recommend','select_student','select_multiple_students','import_event_participants',
    'remove_participant','attendance_page','attendance_event_csv','mark_attendance','mark_all_present',
    'semester_list','semester_detail','semester_report_csv','eligibility','event_permanent_delete',
}
DEPARTMENT_ADMIN_READ_ONLY_POSTS={'semester_detail'}

def admin_department_id(admin_id=None):
    row=db().execute('''SELECT x.department_id FROM admin_department_assignments x
        JOIN departments d ON d.id=x.department_id
        WHERE x.admin_id=? AND d.status='active' ORDER BY x.id''',
        (admin_id or session.get('admin_id'),)).fetchall()
    return row[0]['department_id'] if len(row)==1 else None

def admin_department_scope_ids(admin_id=None):
    """Department data scope; CA and CS admins share both department scopes."""
    primary=admin_department_id(admin_id)
    if not primary:
        return []
    row=db().execute('SELECT code,name FROM departments WHERE id=? AND status=\'active\'',(primary,)).fetchone()
    if row and (row['code']=='CA' or row['name'] in ('Computer Applications','Computational Studies')):
        pair=db().execute("SELECT id FROM departments WHERE status='active' AND (code='CA' OR name IN ('Computer Applications','Computational Studies')) ORDER BY id").fetchall()
        if len(pair)==2:
            return [item['id'] for item in pair]
    return [primary]

def department_scope_sql(column='department_id'):
    ids=getattr(g,'admin_department_ids',None)
    return (f"{column} IN ({','.join('?' for _ in ids)})",tuple(ids)) if ids else ('1=0',())

def student_belongs_to_admin_department(student_id, department_id=None):
    ids=[department_id] if department_id else getattr(g,'admin_department_ids',None) or ([getattr(g,'admin_department_id',None)] if getattr(g,'admin_department_id',None) else [])
    if not ids:return False
    return bool(db().execute(f"SELECT 1 FROM students WHERE id=? AND department_id IN ({','.join('?' for _ in ids)}) AND department_verification='verified'",(student_id,*ids)).fetchone())

def event_belongs_to_admin_department(event_id, department_id=None):
    ids=[department_id] if department_id else getattr(g,'admin_department_ids',None) or ([getattr(g,'admin_department_id',None)] if getattr(g,'admin_department_id',None) else [])
    if not ids:return False
    return bool(db().execute(f"SELECT 1 FROM event_departments WHERE event_id=? AND department_id IN ({','.join('?' for _ in ids)})",(event_id,*ids)).fetchone())

def event_is_shared(event_id):
    return db().execute('SELECT COUNT(*) FROM event_departments WHERE event_id=?',(event_id,)).fetchone()[0]>1

def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get('admin_id'): return redirect(url_for('login'))
        admin=db().execute('''SELECT a.id,a.username,a.display_name,a.email,a.status,a.session_token,r.role
            FROM admins a JOIN admin_roles r ON r.admin_id=a.id WHERE a.id=?''',(session['admin_id'],)).fetchone()
        token=session.get('admin_session_token','')
        if not admin or admin['status']!='active' or not token or not secrets.compare_digest(admin['session_token'],token):
            session.clear(); flash('Your administrator session is no longer active. Sign in again.','error')
            return redirect(url_for('login'))
        g.admin=admin
        g.admin_department_id=admin_department_id(admin['id']) if admin['role']=='department_admin' else None
        g.admin_department_ids=admin_department_scope_ids(admin['id']) if admin['role']=='department_admin' else []
        session['admin_role']=admin['role']
        if admin['role']=='department_admin':
            if not g.admin_department_id: return 'Department administrator access is not assigned to one active department.',403
            if request.endpoint not in DEPARTMENT_ADMIN_ENDPOINTS: return 'This section is restricted to Super Admins.',403
            if request.method=='POST' and request.endpoint in DEPARTMENT_ADMIN_READ_ONLY_POSTS: return 'Semester criteria and settings are managed by Super Admins.',403
            if request.endpoint in ('cohort_mapping_save','cohort_mapping_toggle','cohort_status_update'):
                return 'Registration mappings and global batch status are managed by Super Admins.',403
            if 'sid' in kwargs and not student_belongs_to_admin_department(kwargs['sid']): return 'Volunteer not found.',404
            if 'eid' in kwargs and not event_belongs_to_admin_department(kwargs['eid']): return 'Event not found.',404
            if (request.method=='POST' and 'eid' in kwargs and request.endpoint in
                ('event_semester_update','event_eligibility_update','event_delete','event_permanent_delete') and event_is_shared(kwargs['eid'])):
                return 'Shared event settings are managed by Super Admins.',403
            if 'cohort_id' in kwargs and not db().execute(f'''SELECT 1 FROM cohort_departments WHERE department_id IN ({','.join('?' for _ in g.admin_department_ids)}) AND cohort_id=?
                UNION SELECT 1 FROM students WHERE department_id IN ({','.join('?' for _ in g.admin_department_ids)}) AND cohort_id=? LIMIT 1''',
                (*g.admin_department_ids,kwargs['cohort_id'],*g.admin_department_ids,kwargs['cohort_id'])).fetchone(): return 'Batch not found.',404
            if 'batch_id' in kwargs:
                return 'Participation sheet imports are restricted to Super Admins.',403
            if 'attendance_id' in kwargs:
                row=db().execute(f'''SELECT 1 FROM attendance a JOIN students s ON s.id=a.student_id
                    WHERE a.id=? AND s.department_id IN ({','.join('?' for _ in g.admin_department_ids)}) AND s.department_verification='verified' ''',
                    (kwargs['attendance_id'],*g.admin_department_ids)).fetchone()
                if not row: return 'Attendance record not found.',404
        return fn(*args, **kwargs)
    return wrapped

def central_admin_required(fn):
    @wraps(fn)
    @login_required
    def wrapped(*args, **kwargs):
        role=db().execute('SELECT role FROM admin_roles WHERE admin_id=?',(session['admin_id'],)).fetchone()
        if not role or role['role']!='super_admin': return 'Super Admin access required.',403
        return fn(*args, **kwargs)
    return wrapped

def student_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get('student_id'):
            return redirect(url_for('volunteer_page'))
        account=db().execute('SELECT session_token FROM student_accounts WHERE student_id=?',(session['student_id'],)).fetchone()
        token=session.get('student_session_token','')
        if not account or not token or not secrets.compare_digest(account['session_token'],token):
            session.clear()
            flash('Your volunteer session has ended. Sign in again to continue.','error')
            return redirect(url_for('volunteer_page'))
        return fn(*args, **kwargs)
    return wrapped

def audit(action, entity, entity_id, old='', new=''):
    db().execute('INSERT INTO admin_audit_logs(admin_id,action,entity_type,entity_id,old_value,new_value) VALUES(?,?,?,?,?,?)', (session.get('admin_id'), action, entity, entity_id, str(old), str(new)))

class DriveFolderError(ValueError):
    pass

def extract_drive_folder_id(folder_url):
    try:
        parsed=urlsplit(folder_url.strip())
    except ValueError as exc:
        raise DriveFolderError('Enter a valid Google Drive folder link.') from exc
    if parsed.scheme != 'https' or parsed.hostname != 'drive.google.com':
        raise DriveFolderError('Enter a Google Drive folder link using https://drive.google.com.')
    match=re.fullmatch(r'/drive/folders/([A-Za-z0-9_-]+)', parsed.path)
    if match:
        return match.group(1)
    folder_id=parse_qs(parsed.query).get('id',[''])[0]
    if folder_id and re.fullmatch(r'[A-Za-z0-9_-]+',folder_id):
        return folder_id
    raise DriveFolderError('The link must point to a Google Drive folder.')

def fetch_drive_pdf_names(folder_id, api_key):
    if not api_key:
        raise DriveFolderError('Drive checking is not configured. Set GOOGLE_DRIVE_API_KEY in the server environment.')
    folder_params=urlencode({'fields':'id,mimeType','supportsAllDrives':'true','key':api_key})
    folder_url=f'https://www.googleapis.com/drive/v3/files/{quote(folder_id,safe="")}?{folder_params}'
    try:
        with urlopen(Request(folder_url,headers={'Accept':'application/json'}),timeout=20) as response:
            folder=json.loads(response.read().decode('utf-8'))
    except HTTPError as exc:
        raise DriveFolderError('Google Drive could not read this folder. Check that it is shared for public viewing and that the Drive API key is valid.') from exc
    except URLError as exc:
        raise DriveFolderError('Could not connect to Google Drive. Check the server connection and try again.') from exc
    except (UnicodeDecodeError,json.JSONDecodeError) as exc:
        raise DriveFolderError('Google Drive returned an unreadable response. Try again later.') from exc
    if not isinstance(folder,dict) or folder.get('mimeType')!='application/vnd.google-apps.folder':
        raise DriveFolderError('The Drive link does not point to a folder.')
    names=[]
    page_token=''
    for _ in range(50):
        params={
            'q':f"'{folder_id}' in parents and mimeType='application/pdf' and trashed=false",
            'fields':'nextPageToken,files(name)',
            'pageSize':1000,
            'supportsAllDrives':'true',
            'includeItemsFromAllDrives':'true',
            'key':api_key,
        }
        if page_token:
            params['pageToken']=page_token
        url='https://www.googleapis.com/drive/v3/files?'+urlencode(params)
        req=Request(url,headers={'Accept':'application/json'})
        try:
            with urlopen(req,timeout=20) as response:
                payload=json.loads(response.read().decode('utf-8'))
        except HTTPError as exc:
            raise DriveFolderError('Google Drive could not list this folder. Check that it is shared for public viewing and that the Drive API key is valid.') from exc
        except URLError as exc:
            raise DriveFolderError('Could not connect to Google Drive. Check the server connection and try again.') from exc
        except (UnicodeDecodeError,json.JSONDecodeError) as exc:
            raise DriveFolderError('Google Drive returned an unreadable response. Try again later.') from exc
        files=payload.get('files')
        if not isinstance(files,list) or any(not isinstance(item,dict) or not isinstance(item.get('name'),str) for item in files):
            raise DriveFolderError('Google Drive returned an unexpected file listing. Check the folder link and try again.')
        names.extend(item['name'] for item in files)
        page_token=payload.get('nextPageToken','')
        if not page_token:
            return names
    raise DriveFolderError('This folder has too many pages to scan in one check. Split the certificates into smaller folders and try again.')

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        username=request.form.get('username','').strip()
        cutoff=int(time.time())-900
        tries=db().execute('SELECT COUNT(*) FROM login_attempts WHERE username=? AND attempted_at>? AND success=0',(username,cutoff)).fetchone()[0]
        if tries >= 8:
            flash('Too many attempts. Wait 15 minutes before trying again.', 'error')
            return render_template('login.html'), 429
        user = db().execute('''SELECT a.*,r.role FROM admins a JOIN admin_roles r ON r.admin_id=a.id
            WHERE a.username=?''',(username,)).fetchone()
        valid_department_admin=(user and user['role']=='department_admin' and admin_department_id(user['id']) is not None)
        if user and user['status']=='active' and (user['role']=='super_admin' or valid_department_admin) and check_password_hash(user['password_hash'], request.form.get('password','')):
            db().execute('INSERT INTO login_attempts(username,attempted_at,success) VALUES(?,?,1)',(username,int(time.time())))
            db().execute('UPDATE admins SET last_login=CURRENT_TIMESTAMP WHERE id=?',(user['id'],))
            db().commit()
            session.clear(); session['admin_id']=user['id']; session['username']=user['username']; session['admin_display_name']=user['display_name'] or user['username']; session['admin_session_token']=user['session_token']; session['admin_role']=user['role']; session['admin_welcome_nonce']=secrets.token_urlsafe(18); session['_csrf_token']=secrets.token_urlsafe(32)
            if user['role']=='department_admin':
                scope_ids=admin_department_scope_ids(user['id'])
                scope_names=[row['name'] for row in db().execute(f"SELECT name FROM departments WHERE id IN ({','.join('?' for _ in scope_ids)}) ORDER BY name",tuple(scope_ids)).fetchall()]
                session['admin_department_label']='CA + CS Departments' if len(scope_names)>1 else (scope_names[0] if scope_names else 'Assigned Department')
            else:
                session['admin_department_label']='All Departments'
            return redirect(url_for('dashboard'))
        db().execute('INSERT INTO login_attempts(username,attempted_at,success) VALUES(?,?,0)',(username,int(time.time())))
        db().commit()
        flash('Those credentials did not match an admin account.', 'error')
    return render_template('login.html')

@app.route('/volunteer', methods=['GET','POST'])
def volunteer_page():
    """Student sign-in using the register number and password issued by staff."""
    if session.get('student_id'):
        return redirect(url_for('volunteer_home'))
    if request.method=='POST':
        register_no=request.form.get('register_no','').strip()
        attempt_key='student:'+register_no
        cutoff=int(time.time())-900
        tries=db().execute('SELECT COUNT(*) FROM login_attempts WHERE username=? AND attempted_at>? AND success=0',(attempt_key,cutoff)).fetchone()[0]
        if tries>=8:
            flash('Too many attempts. Wait 15 minutes before trying again.','error')
            return render_template('volunteer_login.html'),429
        account=db().execute('SELECT a.password_hash,a.session_token,a.student_id,s.name,s.yrc_status FROM student_accounts a JOIN students s ON s.id=a.student_id WHERE s.register_no=?',(register_no,)).fetchone()
        password=request.form.get('password','')
        if account and account['yrc_status']=='Active' and check_password_hash(account['password_hash'],password):
            db().execute('INSERT INTO login_attempts(username,attempted_at,success) VALUES(?,?,1)',(attempt_key,int(time.time())))
            db().commit()
            session.clear(); session['student_id']=account['student_id']; session['student_name']=account['name']; session['student_session_token']=account['session_token']; session['volunteer_welcome_nonce']=secrets.token_urlsafe(18); session['_csrf_token']=secrets.token_urlsafe(32)
            return redirect(url_for('volunteer_home'))
        db().execute('INSERT INTO login_attempts(username,attempted_at,success) VALUES(?,?,0)',(attempt_key,int(time.time())))
        db().commit()
        flash('Those details did not match an active volunteer account. Ask your YRC coordinator for access.','error')
    return render_template('volunteer_login.html')

@app.get('/volunteer/home')
@student_required
def volunteer_home():
    sid=session['student_id']; conn=db()
    if not session.get('volunteer_welcome_nonce'):
        session['volunteer_welcome_nonce']=secrets.token_urlsafe(18)
    student=conn.execute('SELECT * FROM students WHERE id=?',(sid,)).fetchone()
    if not student or student['yrc_status']!='Active':
        session.clear(); flash('This volunteer account is inactive. Contact your YRC coordinator.','error'); return redirect(url_for('volunteer_page'))
    history=conn.execute("SELECT e.name,e.event_date,e.semester,e.venue,a.status,a.credit_points FROM attendance a JOIN events e ON e.id=a.event_id WHERE a.student_id=? ORDER BY e.event_date DESC,e.id DESC",(sid,)).fetchall()
    upcoming=conn.execute("""SELECT e.*,
        (SELECT selection_status FROM event_participants p WHERE p.event_id=e.id AND p.student_id=?) AS my_status
        FROM events e WHERE e.status IN ('Upcoming','Open') AND e.event_date>=?
        ORDER BY e.event_date,e.start_time,e.id LIMIT 8""",(sid,date.today().isoformat())).fetchall()
    registered=[event for event in upcoming if event['my_status']=='Selected']
    semesters=conn.execute('SELECT * FROM semesters ORDER BY id').fetchall()
    leadership_status='ready'
    if not student['department_id'] or student['department_verification']!='verified':
        student_convenors=[]
        leadership_status='department_pending'
    else:
        student_convenors=conn.execute('''SELECT l.id,l.name,l.leadership_role AS role,l.designation AS coordinator_title,
                l.phone,l.email,l.photo,d.name AS department_name
            FROM department_leadership_appointments l
            JOIN departments d ON d.id=l.department_id
            WHERE l.department_id=? AND d.status='active' AND l.status='active'
              AND (l.active_from='' OR l.active_from<=?)
              AND (l.active_until IS NULL OR l.active_until='' OR l.active_until>=?)
            ORDER BY CASE l.leadership_role WHEN 'department_leader' THEN 0 ELSE 1 END,l.id''',
            (student['department_id'],date.today().isoformat(),date.today().isoformat())).fetchall()
        has_leader=any(person['role']=='department_leader' for person in student_convenors)
        has_secretary=any(person['role']=='department_secretary' for person in student_convenors)
        leadership_status='leader_vacancy' if not has_leader else ('secretary_vacancy' if not has_secretary else 'ready')
    progress=[{'semester':sem,'result':semester_eligibility(sem['id'],sid)} for sem in semesters]
    conn.commit()
    return render_template('volunteer_home.html',student=student,history=history,semester_progress=progress,student_convenors=student_convenors,leadership_status=leadership_status,upcoming=upcoming,registered=registered,welcome_nonce=session['volunteer_welcome_nonce'])

@app.post('/volunteer/change-password')
@student_required
def volunteer_change_password():
    password=request.form.get('new_password','')
    if len(password)<8:
        flash('Choose a password with at least 8 characters.','error')
    else:
        token=secrets.token_urlsafe(32)
        db().execute('UPDATE student_accounts SET password_hash=?,session_token=?,updated_at=CURRENT_TIMESTAMP WHERE student_id=?',(generate_password_hash(password),token,session['student_id']))
        session['student_session_token']=token
        db().commit(); flash('Your password has been changed.','success')
    return redirect(url_for('volunteer_home'))

@app.post('/volunteer/update-profile')
@student_required
def volunteer_update_profile():
    email=request.form.get('email','').strip()
    phone=request.form.get('phone','').strip()
    valid_email=not email or (len(email)<=254 and re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email))
    valid_phone=not phone or (len(phone)<=30 and re.fullmatch(r'\+?[0-9().\- ]{7,30}',phone) and sum(character.isdigit() for character in phone)>=7)
    if not valid_email:
        flash('Enter a valid college email address.','error')
    elif not valid_phone:
        flash('Enter a valid phone number.','error')
    else:
        db().execute('UPDATE students SET email=?,phone=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',(email or None,phone or None,session['student_id']))
        db().commit()
        flash('Your profile contact details have been updated.','success')
    return redirect(url_for('volunteer_home',_anchor='profile-settings'))

@app.post('/logout')
def logout(): session.clear(); return redirect(url_for('login'))

@app.post('/volunteer/logout')
def volunteer_logout(): session.clear(); return redirect(url_for('volunteer_page'))

@app.get('/admins')
@central_admin_required
def admin_management():
    conn=db()
    admins=conn.execute('''SELECT a.id,a.username,a.display_name,a.email,a.status,a.last_login,r.role,
        d.id department_id,d.name department_name
        ,(SELECT l.action FROM admin_audit_logs l WHERE l.admin_id=a.id ORDER BY l.id DESC LIMIT 1) last_action,
        (SELECT l.created_at FROM admin_audit_logs l WHERE l.admin_id=a.id ORDER BY l.id DESC LIMIT 1) last_action_at
        FROM admins a JOIN admin_roles r ON r.admin_id=a.id
        LEFT JOIN admin_department_assignments x ON x.admin_id=a.id
        LEFT JOIN departments d ON d.id=x.department_id
        ORDER BY CASE r.role WHEN 'super_admin' THEN 0 ELSE 1 END,a.username''').fetchall()
    departments=conn.execute("SELECT id,name FROM departments WHERE status='active' ORDER BY name").fetchall()
    activity_rows=conn.execute('''SELECT admin_id,action,entity_type,entity_id,created_at
        FROM admin_audit_logs WHERE admin_id IS NOT NULL ORDER BY id DESC LIMIT 500''').fetchall()
    activity_by_admin={}
    for item in activity_rows:
        activity_by_admin.setdefault(item['admin_id'],[])
        if len(activity_by_admin[item['admin_id']])<8:
            activity_by_admin[item['admin_id']].append(item)
    return render_template('admins.html',admins=admins,departments=departments,activity_by_admin=activity_by_admin)

@app.get('/audit-logs')
@central_admin_required
def audit_logs():
    logs=db().execute('''SELECT l.*,a.username,a.display_name FROM admin_audit_logs l
        LEFT JOIN admins a ON a.id=l.admin_id ORDER BY l.id DESC LIMIT 1000''').fetchall()
    return render_template('audit_logs.html',logs=logs)

@app.post('/admins/create')
@central_admin_required
def admin_create():
    username=request.form.get('username','').strip()
    display_name=request.form.get('display_name','').strip()
    email=request.form.get('email','').strip()
    password=request.form.get('password','')
    role=request.form.get('role','')
    status=request.form.get('status','active')
    department_id=request.form.get('department_id','').strip()
    department=None
    if role=='department_admin' and department_id.isdigit():
        department=db().execute("SELECT id,name FROM departments WHERE id=? AND status='active'",(int(department_id),)).fetchone()
    if (not re.fullmatch(r'[A-Za-z0-9._-]{3,40}',username) or not display_name or len(display_name)>120 or
        (email and (len(email)>160 or not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+',email))) or
        len(password)<8 or len(password)>128 or role not in ('super_admin','department_admin') or
        status not in ('active','suspended','revoked') or (role=='department_admin' and not department)):
        flash('Check the admin name, login ID, email, password (at least 8 characters), role, and department assignment.','error')
        return redirect(url_for('admin_management'))
    conn=db()
    try:
        cur=conn.execute('''INSERT INTO admins(username,password_hash,display_name,email,status,session_token)
            VALUES(?,?,?,?,?,?)''',(username,generate_password_hash(password),display_name,email,status,secrets.token_urlsafe(32)))
        admin_id=cur.lastrowid
        conn.execute('INSERT INTO admin_roles(admin_id,role,department) VALUES(?,?,?)',
                     (admin_id,role,department['name'] if department else ''))
        if department:
            conn.execute('''INSERT INTO admin_department_assignments
                (admin_id,department_id,role,can_manage_volunteers,can_manage_events,can_view_reports)
                VALUES(?,?,'department_admin',1,1,0)''',(admin_id,department['id']))
        audit('Admin account created','admin',admin_id,'',f'{username}; {role}; {department["name"] if department else "all departments"}; {status}')
        conn.commit(); flash('Admin account created.','success')
    except DB_INTEGRITY_ERROR:
        conn.rollback(); flash('That login ID is already in use.','error')
    return redirect(url_for('admin_management'))

@app.post('/admins/<int:admin_id>/update')
@central_admin_required
def admin_update(admin_id):
    conn=db(); current=conn.execute('''SELECT a.*,r.role FROM admins a JOIN admin_roles r ON r.admin_id=a.id WHERE a.id=?''',(admin_id,)).fetchone()
    if not current: return 'Admin account not found.',404
    username=request.form.get('username','').strip()
    display_name=request.form.get('display_name','').strip()
    email=request.form.get('email','').strip()
    role=request.form.get('role','')
    status=request.form.get('status','')
    password=request.form.get('password','')
    department_id=request.form.get('department_id','').strip()
    department=None
    if role=='department_admin' and department_id.isdigit():
        department=conn.execute("SELECT id,name FROM departments WHERE id=? AND status='active'",(int(department_id),)).fetchone()
    if (not re.fullmatch(r'[A-Za-z0-9._-]{3,40}',username) or not display_name or len(display_name)>120 or (email and (len(email)>160 or not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+',email))) or
        role not in ('super_admin','department_admin') or status not in ('active','suspended','revoked') or
        (password and (len(password)<8 or len(password)>128)) or (role=='department_admin' and not department)):
        flash('Check the username, name, email, password, role, status, and required department assignment.','error')
        return redirect(url_for('admin_management'))
    duplicate=conn.execute('SELECT 1 FROM admins WHERE username=? AND id<>?',(username,admin_id)).fetchone()
    if duplicate:
        flash('That login ID is already in use.','error')
        return redirect(url_for('admin_management'))
    if current['role']=='super_admin' and current['status']=='active' and (role!='super_admin' or status!='active'):
        active_super=conn.execute("SELECT COUNT(*) FROM admins a JOIN admin_roles r ON r.admin_id=a.id WHERE r.role='super_admin' AND a.status='active'").fetchone()[0]
        if active_super<=1:
            flash('The last active Super Admin cannot be demoted, suspended, or revoked. Create another Super Admin first.','error')
            return redirect(url_for('admin_management'))
    token=secrets.token_urlsafe(32)
    if password:
        conn.execute('UPDATE admins SET username=?,display_name=?,email=?,status=?,password_hash=?,session_token=? WHERE id=?',
                     (username,display_name,email,status,generate_password_hash(password),token,admin_id))
    else:
        conn.execute('UPDATE admins SET username=?,display_name=?,email=?,status=?,session_token=? WHERE id=?',
                     (username,display_name,email,status,token,admin_id))
    conn.execute('UPDATE admin_roles SET role=?,department=? WHERE admin_id=?',(role,department['name'] if department else '',admin_id))
    conn.execute('DELETE FROM admin_department_assignments WHERE admin_id=?',(admin_id,))
    if department:
        conn.execute('''INSERT INTO admin_department_assignments
            (admin_id,department_id,role,can_manage_volunteers,can_manage_events,can_view_reports)
            VALUES(?,?,'department_admin',1,1,0)''',(admin_id,department['id']))
    audit('Admin account updated','admin',admin_id,
          f'{current["username"]}; {current["role"]}; {current["status"]}; {current["display_name"]}',
          f'{username}; {role}; {status}; {display_name}; {department["name"] if department else "all departments"}')
    conn.commit(); flash('Admin account updated. Its previous sessions have been revoked.','success')
    return redirect(url_for('admin_management'))

@app.get('/')
@login_required
def dashboard():
    conn=db()
    if g.admin['role']=='department_admin':
        department=conn.execute('SELECT id,name FROM departments WHERE id=? AND status=\'active\'',(g.admin_department_id,)).fetchone()
        if len(g.admin_department_ids)>1: department=dict(department); department['name']='CA + CS Departments'
        placeholders=','.join('?' for _ in g.admin_department_ids); scope_args=tuple(g.admin_department_ids)
        student_rows=conn.execute(f"SELECT id FROM students WHERE department_id IN ({placeholders}) AND department_verification='verified' AND yrc_status='Active' ORDER BY id",scope_args).fetchall()
        ids=[row['id'] for row in student_rows]
        stats={
            'students':conn.execute(f"SELECT COUNT(*) FROM students WHERE department_id IN ({placeholders}) AND department_verification='verified'",scope_args).fetchone()[0],
            'active_students':len(ids),
            'events':conn.execute(f'SELECT COUNT(DISTINCT event_id) FROM event_departments WHERE department_id IN ({placeholders})',scope_args).fetchone()[0],
            'attendance':conn.execute(f"SELECT COUNT(DISTINCT a.student_id) FROM attendance a JOIN students s ON s.id=a.student_id WHERE s.department_id IN ({placeholders}) AND s.department_verification='verified' AND a.status='Present'",scope_args).fetchone()[0],
            'attendance_records':conn.execute(f"SELECT COUNT(*) FROM attendance a JOIN students s ON s.id=a.student_id WHERE s.department_id IN ({placeholders}) AND s.department_verification='verified' AND a.status='Present'",scope_args).fetchone()[0],
        }
        batches=conn.execute(f'''SELECT s.cohort,COUNT(*) volunteers,SUM(CASE WHEN s.yrc_status='Active' THEN 1 ELSE 0 END) active
            FROM students s WHERE s.department_id IN ({placeholders}) AND s.department_verification='verified' GROUP BY s.cohort ORDER BY s.cohort''',scope_args).fetchall()
        semester_overview=[]
        for semester in conn.execute('SELECT * FROM semesters ORDER BY id').fetchall():
            results=semester_eligibility_many(semester['id'],ids)
            semester_overview.append({'semester':semester,'eligible':sum(r['status']=='ELIGIBLE' for r in results.values()),
                'not_eligible':sum(r['status']=='NOT_ELIGIBLE' for r in results.values()),
                'incomplete':sum(r['status']=='INCOMPLETE_RECORDS' for r in results.values())})
        recent=conn.execute(f'''SELECT DISTINCT e.* FROM events e JOIN event_departments x ON x.event_id=e.id
            WHERE x.department_id IN ({placeholders}) ORDER BY e.event_date DESC LIMIT 5''',scope_args).fetchall()
        conn.commit()
        return render_template('department_dashboard.html',department=department,stats=stats,batches=batches,
            semester_overview=semester_overview,recent=recent)
    stats={
      'students':conn.execute("SELECT COUNT(*) FROM students WHERE yrc_status='Active'").fetchone()[0],
      'events':conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
      'attendance':conn.execute("SELECT COUNT(DISTINCT a.student_id) FROM attendance a JOIN events e ON e.id=a.event_id WHERE a.status='Present' AND e.status<>'Cancelled'").fetchone()[0],
      'attendance_records':conn.execute("SELECT COUNT(*) FROM attendance a JOIN events e ON e.id=a.event_id WHERE a.status='Present' AND e.status<>'Cancelled'").fetchone()[0],
    }
    dist=conn.execute('SELECT event_count, COUNT(*) AS total FROM (SELECT s.id, COUNT(DISTINCT a.event_id) event_count FROM students s LEFT JOIN attendance a ON a.student_id=s.id AND a.status="Present" WHERE s.yrc_status="Active" GROUP BY s.id) GROUP BY event_count').fetchall()
    buckets=[0,0,0,0]
    for r in dist:
        i=0 if r['event_count']==0 else 1 if r['event_count']<3 else 2 if r['event_count']<6 else 3; buckets[i]+=r['total']
    recent=conn.execute('SELECT * FROM events ORDER BY event_date DESC LIMIT 5').fetchall()
    return render_template('dashboard.html', stats=stats, buckets=buckets, recent=recent)

@app.route('/departments', methods=['GET','POST'])
@central_admin_required
def department_management():
    conn=db()
    if request.method=='POST':
        name=' '.join(request.form.get('name','').split()).strip()
        code=''.join(request.form.get('code','').upper().split())
        if not name or len(name)>120 or not re.fullmatch(r'[A-Z0-9_-]{2,20}',code):
            flash('Enter a department name and a 2–20 character code using letters, numbers, _ or -.','error')
            return redirect(url_for('department_management'))
        try:
            cur=conn.execute("INSERT INTO departments(name,code,status) VALUES(?,?,'active')",(name,code))
            audit('Department created','department',cur.lastrowid,'',f'{name} ({code})')
            conn.commit(); flash('Department created.','success')
        except DB_INTEGRITY_ERROR:
            conn.rollback(); flash('That department name or code is already in use.','error')
        return redirect(url_for('department_management'))
    departments=conn.execute('''SELECT d.*, a.username leader_name,
        (SELECT COUNT(*) FROM students s WHERE s.department_id=d.id) volunteer_count,
        (SELECT COUNT(*) FROM admin_department_assignments x WHERE x.department_id=d.id) admin_count
        FROM departments d LEFT JOIN admins a ON a.id=d.leader_admin_id ORDER BY d.name''').fetchall()
    return render_template('departments.html',departments=departments)

@app.post('/departments/<int:department_id>/update')
@central_admin_required
def department_update(department_id):
    conn=db(); department=conn.execute('SELECT * FROM departments WHERE id=?',(department_id,)).fetchone()
    if not department: return 'Department not found.',404
    name=' '.join(request.form.get('name','').split()).strip()
    code=''.join(request.form.get('code','').upper().split())
    status=request.form.get('status','')
    if not name or len(name)>120 or not re.fullmatch(r'[A-Z0-9_-]{2,20}',code) or status not in ('active','inactive'):
        flash('Check the department name, code, and status.','error'); return redirect(url_for('department_management'))
    try:
        conn.execute('UPDATE departments SET name=?,code=?,status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
            (name,code,status,department_id))
        conn.execute('UPDATE admin_roles SET department=? WHERE admin_id IN (SELECT admin_id FROM admin_department_assignments WHERE department_id=?) AND role=\'department_admin\'',(name,department_id))
        audit('Department updated','department',department_id,department['name'],name)
        conn.commit(); flash('Department details and assignments saved.','success')
    except DB_INTEGRITY_ERROR:
        conn.rollback(); flash('That department name or code is already in use.','error')
    return redirect(url_for('department_management'))

@app.post('/departments/<int:department_id>/status')
@central_admin_required
def department_status(department_id):
    status=request.form.get('status','')
    if status not in ('active','inactive'): return 'Invalid department status.',400
    conn=db(); row=conn.execute('SELECT name,status FROM departments WHERE id=?',(department_id,)).fetchone()
    if not row: return 'Department not found.',404
    conn.execute('UPDATE departments SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',(status,department_id))
    audit('Department status changed','department',department_id,row['status'],status)
    conn.commit(); flash('Department status updated.','success')
    return redirect(url_for('department_management'))

@app.route('/students', methods=['GET','POST'])
@login_required
def students():
    conn=db()
    if request.method=='POST':
        f=request.form
        if f.get('section','').strip().upper() not in tuple('ABCDEFG'):
            flash('Choose a valid section from A through G.','error'); return redirect(url_for('students'))
        try: year=int(f.get('year',''))
        except (TypeError,ValueError):
            flash('Choose a valid year of study.','error'); return redirect(url_for('students'))
        if not f.get('register_no','').strip() or not f.get('name','').strip() or not f.get('department','').strip() or not f.get('class_name','').strip() or year not in range(1,9) or len(f.get('cohort','').strip())>100:
            flash('Enter the register number, name, department, programme, and a year from 1 to 8.','error'); return redirect(url_for('students'))
        department_record=conn.execute("SELECT id,name FROM departments WHERE id=? AND status='active'",(g.admin_department_id,)).fetchone() if g.admin['role']=='department_admin' else department_for_label(conn,f.get('department',''))
        if not department_record:
            flash('Choose a registered active department. Add the department in Department management first.','error'); return redirect(url_for('students'))
        if f.get('yrc_status','Active') not in ('Active','Inactive','Removed'):
            flash('Choose a valid YRC status.','error'); return redirect(url_for('students'))
        try:
            register_no=f['register_no'].strip().upper(); requested_cohort=f.get('cohort','').strip(); mapping=cohort_mapping_for_register(conn,register_no)
            if requested_cohort:
                cohort=requested_cohort; cohort_id=ensure_cohort(conn,cohort); verification='manual'
            elif mapping:
                cohort=mapping['name']; cohort_id=mapping['id']; verification='automatic'
            else:
                cohort='Pending verification'; cohort_id=None; verification='pending'
            if g.admin['role']=='department_admin' and cohort_id:
                conn.execute('INSERT OR IGNORE INTO cohort_departments(cohort_id,department_id) VALUES(?,?)',(cohort_id,department_record['id']))
            cur=conn.execute('''INSERT INTO students(register_no,name,department,department_id,department_verification,class_name,section,year,email,phone,yrc_status,cohort,cohort_id,cohort_verification)
                VALUES(?,?,?,?,'verified',?,?,?,?,?,?,?,?,?)''',(register_no,f['name'].strip(),department_record['name'],department_record['id'],f['class_name'].strip(),f['section'].strip().upper(),year,f.get('email'),f.get('phone'),f.get('yrc_status','Active'),cohort,cohort_id,verification))
            audit('Student added','student',cur.lastrowid,'',f'{register_no}; cohort {cohort} ({verification})'); conn.commit(); flash('Student added.','success')
        except DB_INTEGRITY_ERROR: conn.rollback(); flash('Register number already exists.','error')
        return redirect(url_for('students'))
    q=request.args.get('q','').strip(); status=request.args.get('status',''); dept=request.args.get('department',''); section=request.args.get('section','').upper(); cohort=request.args.get('cohort',''); cohort_verification=request.args.get('cohort_verification',''); year_filter=request.args.get('year','')
    where=[]; args=[]
    if g.admin['role']=='department_admin':
        where.append(f"department_id IN ({','.join('?' for _ in g.admin_department_ids)}) AND department_verification='verified'"); args.extend(g.admin_department_ids)
    if q: where.append('(name LIKE ? OR register_no LIKE ?)'); args += [f'%{q}%',f'%{q}%']
    if status: where.append('yrc_status=?'); args.append(status)
    else: where.append("yrc_status<>'Removed'")
    if dept: where.append('department=?'); args.append(dept)
    if section=='UNASSIGNED': where.append("section=''")
    elif section: where.append('section=?'); args.append(section)
    if cohort: where.append('cohort=?'); args.append(cohort)
    if cohort_verification in ('pending','automatic','manual'):
        where.append('cohort_verification=?'); args.append(cohort_verification)
    if year_filter.isdigit() and int(year_filter) in range(1,9):
        where.append('year=?'); args.append(int(year_filter))
    else:
        year_filter=''
    sql='SELECT s.*, (SELECT COUNT(DISTINCT a.event_id) FROM attendance a WHERE a.student_id=s.id AND a.status="Present") events FROM students s'+(' WHERE '+' AND '.join(where) if where else '')+' ORDER BY s.name'
    rows=conn.execute(sql,args).fetchall()
    if g.admin['role']=='department_admin':
        dept_ph=','.join('?' for _ in g.admin_department_ids)
        departments=[r[0] for r in conn.execute(f'SELECT DISTINCT department FROM students WHERE department_id IN ({dept_ph}) ORDER BY department',tuple(g.admin_department_ids)).fetchall()]
        cohorts=[r[0] for r in conn.execute(f"SELECT DISTINCT cohort FROM students WHERE cohort_id IS NOT NULL AND department_id IN ({dept_ph}) ORDER BY cohort",tuple(g.admin_department_ids)).fetchall()]
    else:
        departments=[r[0] for r in conn.execute('SELECT DISTINCT department FROM students ORDER BY department').fetchall()]
        cohorts=[r[0] for r in conn.execute("SELECT name FROM cohorts UNION SELECT DISTINCT cohort FROM students WHERE cohort_id IS NULL AND cohort<>'' ORDER BY 1")]
    return render_template('students.html', students=rows,q=q,status=status,department=dept,departments=departments,section=section,cohort=cohort,cohorts=cohorts,cohort_verification=cohort_verification,year_filter=year_filter)

@app.get('/cohorts')
@login_required
def cohort_management():
    conn=db()
    dept_clause=' AND s.department_id IN ('+','.join('?' for _ in g.admin_department_ids)+')' if g.admin['role']=='department_admin' else ''
    dept_args=tuple(g.admin_department_ids) if dept_clause else ()
    cohort_scope=' OR EXISTS(SELECT 1 FROM cohort_departments x WHERE x.cohort_id=c.id AND x.department_id IN ('+','.join('?' for _ in g.admin_department_ids)+'))' if dept_clause else ''
    cohorts=conn.execute('''SELECT c.*,
        (SELECT COUNT(*) FROM students s WHERE s.cohort_id=c.id'''+dept_clause+''') volunteer_count,
        (SELECT COUNT(*) FROM students s WHERE s.cohort_id=c.id AND s.yrc_status='Active' '''+dept_clause+''') active_count,
        (SELECT COUNT(*) FROM students s WHERE s.cohort_id=c.id AND s.cohort_verification='pending' '''+dept_clause+''') pending_count,
        (SELECT COUNT(*) FROM event_participants p JOIN students s ON s.id=p.student_id WHERE s.cohort_id=c.id AND p.selection_status='Selected' '''+dept_clause+''') participation_count
        FROM cohorts c WHERE EXISTS(SELECT 1 FROM students s WHERE s.cohort_id=c.id'''+dept_clause+')'+cohort_scope+''' ORDER BY COALESCE(c.admission_year,9999),c.name''',dept_args*6).fetchall()
    mappings=[] if g.admin['role']=='department_admin' else conn.execute('''SELECT m.prefix,m.active,c.id cohort_id,c.name cohort_name,c.status cohort_status
        FROM cohort_prefix_mappings m JOIN cohorts c ON c.id=m.cohort_id ORDER BY m.prefix''').fetchall()
    pending_rows=conn.execute(f"SELECT id,register_no,name,department,year,cohort FROM students WHERE cohort_verification='pending' AND department_id IN ({','.join('?' for _ in g.admin_department_ids)}) ORDER BY register_no,name",tuple(g.admin_department_ids)).fetchall() if g.admin['role']=='department_admin' else conn.execute("SELECT id,register_no,name,department,year,cohort FROM students WHERE cohort_verification='pending' ORDER BY register_no,name").fetchall()
    pending=[]
    for row in pending_rows:
        suggestion=cohort_mapping_for_register(conn,row['register_no']) if g.admin['role']=='super_admin' else None
        item={key:row[key] for key in row.keys()}
        item['suggested_cohort_id']=suggestion['id'] if suggestion else None
        item['suggested_cohort_name']=suggestion['name'] if suggestion else ''
        pending.append(item)
    active_mapping_count=sum(1 for row in mappings if row['active'] and row['cohort_status']=='active')
    return render_template('cohorts.html',cohorts=cohorts,mappings=mappings,pending=pending,active_mapping_count=active_mapping_count)

@app.post('/cohorts/create')
@login_required
def cohort_create():
    name=' '.join(request.form.get('name','').split()).strip()
    raw_year=request.form.get('admission_year','').strip()
    try: admission_year=int(raw_year) if raw_year else None
    except ValueError: admission_year=None
    if not name or len(name)>100 or (raw_year and (admission_year is None or admission_year not in range(2000,2201))):
        flash('Enter a cohort name and a valid admission year.','error'); return redirect(url_for('cohort_management'))
    try:
        conn=db()
        if g.admin['role']=='department_admin':
            existing=conn.execute('SELECT id FROM cohorts WHERE name=?',(name,)).fetchone()
            cohort_id=existing['id'] if existing else conn.execute("INSERT INTO cohorts(name,admission_year,status) VALUES(?,?,'active')",(name,admission_year)).lastrowid
            for dept_id in g.admin_department_ids:
                conn.execute('INSERT OR IGNORE INTO cohort_departments(cohort_id,department_id) VALUES(?,?)',(cohort_id,dept_id))
            cur=type('Created',(),{'lastrowid':cohort_id})()
        else:
            cur=conn.execute("INSERT INTO cohorts(name,admission_year,status) VALUES(?,?,'active')",(name,admission_year))
        audit('Cohort created','cohort',cur.lastrowid,'',name); db().commit(); flash('Cohort added.','success')
    except DB_INTEGRITY_ERROR:
        db().rollback(); flash('That cohort name already exists.','error')
    return redirect(url_for('cohort_management'))

@app.post('/cohorts/mappings')
@login_required
def cohort_mapping_save():
    prefix=request.form.get('prefix','').strip()
    try: cohort_id=int(request.form.get('cohort_id',''))
    except (TypeError,ValueError): cohort_id=0
    cohort=db().execute("SELECT name FROM cohorts WHERE id=? AND status='active'",(cohort_id,)).fetchone()
    if not re.fullmatch(r'[0-9]{2,4}',prefix) or not cohort:
        flash('Use a two to four digit registration prefix and choose a cohort.','error'); return redirect(url_for('cohort_management'))
    previous=db().execute('SELECT cohort_id FROM cohort_prefix_mappings WHERE prefix=?',(prefix,)).fetchone()
    db().execute('''INSERT INTO cohort_prefix_mappings(prefix,cohort_id,active) VALUES(?,?,1)
        ON CONFLICT(prefix) DO UPDATE SET cohort_id=excluded.cohort_id,active=1''',(prefix,cohort_id))
    audit('Cohort registration prefix configured','cohort_prefix',None,f'{prefix}: {previous["cohort_id"] if previous else "unmapped"}',f'{prefix}: {cohort["name"]}'); db().commit()
    flash(f'Registration prefix {prefix} now maps to {cohort["name"]}. New records only will use this mapping.','success')
    return redirect(url_for('cohort_management'))

@app.post('/cohorts/mappings/<prefix>/toggle')
@login_required
def cohort_mapping_toggle(prefix):
    row=db().execute('SELECT active,cohort_id FROM cohort_prefix_mappings WHERE prefix=?',(prefix,)).fetchone()
    if not row:
        flash('Registration prefix mapping not found.','error'); return redirect(url_for('cohort_management'))
    active=0 if row['active'] else 1
    db().execute('UPDATE cohort_prefix_mappings SET active=? WHERE prefix=?',(active,prefix))
    audit('Cohort registration prefix status changed','cohort_prefix',None,f'{prefix}: {row["active"]}',f'{prefix}: {active}'); db().commit()
    flash('Registration prefix mapping updated.','success'); return redirect(url_for('cohort_management'))

@app.post('/cohorts/<int:cohort_id>/status')
@login_required
def cohort_status_update(cohort_id):
    status=request.form.get('status','')
    if status not in ('active','archived'):
        flash('Choose a valid cohort status.','error'); return redirect(url_for('cohort_management'))
    row=db().execute('SELECT name,status FROM cohorts WHERE id=?',(cohort_id,)).fetchone()
    if not row:
        flash('Cohort not found.','error'); return redirect(url_for('cohort_management'))
    db().execute('UPDATE cohorts SET status=? WHERE id=?',(status,cohort_id))
    audit('Cohort status changed','cohort',cohort_id,row['status'],status); db().commit()
    flash(f'{row["name"]} is now {status}. Volunteer records and history were preserved.','success')
    return redirect(url_for('cohort_management'))

@app.post('/students/<int:sid>/cohort-assign')
@login_required
def student_cohort_assign(sid):
    try: cohort_id=int(request.form.get('cohort_id',''))
    except (TypeError,ValueError): cohort_id=0
    conn=db(); student=conn.execute('SELECT cohort,department_id FROM students WHERE id=?',(sid,)).fetchone()
    cohort_sql="SELECT c.name FROM cohorts c WHERE c.id=? AND c.status='active'"
    cohort_args=[cohort_id]
    if g.admin['role']=='department_admin':
        cohort_sql+=f" AND EXISTS(SELECT 1 FROM cohort_departments x WHERE x.cohort_id=c.id AND x.department_id IN ({','.join('?' for _ in g.admin_department_ids)}))"
        cohort_args.extend(g.admin_department_ids)
    cohort=conn.execute(cohort_sql,cohort_args).fetchone()
    if not student or not cohort:
        flash('Choose a valid student and cohort. No records were changed.','error'); return redirect(url_for('cohort_management'))
    if g.admin['role']=='department_admin' and student['department_id'] not in g.admin_department_ids: return 'Volunteer not found.',404
    conn.execute("UPDATE students SET cohort=?,cohort_id=?,cohort_verification='manual',updated_at=CURRENT_TIMESTAMP WHERE id=?",(cohort['name'],cohort_id,sid))
    audit('Student cohort manually confirmed','student',sid,student['cohort'],cohort['name']); conn.commit()
    flash(f'{cohort["name"]} was confirmed for this volunteer. Existing attendance and participation history was preserved.','success')
    return redirect(url_for('cohort_management'))

@app.post('/students/import')
@login_required
def import_students():
    import csv, io
    upload=request.files.get('file')
    if not upload or not upload.filename.lower().endswith('.csv'):
        flash('Choose a CSV file with register_no, name, department, and year columns.','error'); return redirect(url_for('students'))
    try: reader=csv.DictReader(io.StringIO(upload.stream.read().decode('utf-8-sig')))
    except UnicodeDecodeError:
        flash('CSV must be saved as UTF-8. No students were imported.','error'); return redirect(url_for('students'))
    required={'register_no','name','department','year'}
    if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
        flash('CSV needs register_no, name, department, and year columns. No students were imported.','error'); return redirect(url_for('students'))
    records=[]; errors=[]; known={r[0].strip().casefold() for r in db().execute('SELECT register_no FROM students')}
    for row_num,row in enumerate(reader,start=2):
        if not any(str(value or '').strip() for value in row.values()):
            continue
        reg=(row.get('register_no') or '').strip().upper(); name=(row.get('name') or '').strip(); department=(row.get('department') or '').strip(); class_name=(row.get('class_name') or row.get('class') or '').strip(); section=(row.get('section') or '').strip().upper()
        if '—' in department and not class_name:
            department, class_name = (part.strip() for part in department.split('—',1))
        try: year=int((row.get('year') or '').strip())
        except ValueError: year=0
        status=(row.get('yrc_status') or 'Active').strip().title()
        if not reg or not name or not department or not class_name or year<1: errors.append(f'Row {row_num}: register number, name, department, class, and a positive year are required.'); continue
        department_record=(db().execute(f"SELECT id,name FROM departments WHERE id IN ({','.join('?' for _ in g.admin_department_ids)}) AND name=? AND status='active'",(*g.admin_department_ids,department)).fetchone()
                           if g.admin['role']=='department_admin' else department_for_label(db(),department))
        if not department_record: errors.append(f'Row {row_num}: department {department!r} is not registered or active.'); continue
        if section and section not in tuple('ABCDEFG'): errors.append(f'Row {row_num}: section must be a letter from A through G.'); continue
        if status not in ('Active','Inactive','Removed'): errors.append(f'Row {row_num}: invalid YRC status {status!r}.'); continue
        if reg.casefold() in known:
            errors.append(f'Row {row_num}: register number is already in use.' if g.admin['role']=='department_admin' else f'Row {row_num}: duplicate register number {reg}.')
            continue
        known.add(reg.casefold())
        requested_cohort=(row.get('cohort') or '').strip()
        if len(requested_cohort)>100:
            errors.append(f'Row {row_num}: cohort label must be 100 characters or fewer.'); continue
        mapping=cohort_mapping_for_register(db(),reg)
        if requested_cohort:
            cohort=requested_cohort; cohort_id=None; cohort_verification='manual'
        elif mapping:
            cohort=mapping['name']; cohort_id=mapping['id']; cohort_verification='automatic'
        else:
            cohort='Pending verification'; cohort_id=None; cohort_verification='pending'
        if g.admin['role']=='department_admin' and cohort_id:
            db().execute('INSERT OR IGNORE INTO cohort_departments(cohort_id,department_id) VALUES(?,?)',(cohort_id,department_record['id']))
        records.append((reg,name,department_record['name'],department_record['id'],'verified',class_name,section,year,(row.get('email') or '').strip(),(row.get('phone') or '').strip(),status,cohort,cohort_id,cohort_verification))
    if errors:
        preview=' '.join(errors[:5]); more=f' (+{len(errors)-5} more)' if len(errors)>5 else ''
        flash(f'No students imported. Fix {len(errors)} issue(s): {preview}{more}','error'); return redirect(url_for('students'))
    if not records:
        flash('The CSV has no student rows. No students were imported.','error'); return redirect(url_for('students'))
    conn=db()
    try:
        records=[record[:11]+(ensure_cohort(conn,record[10]) if record[12]=='manual' else record[11],record[12]) for record in records]
        conn.executemany('INSERT INTO students(register_no,name,department,department_id,department_verification,class_name,section,year,email,phone,yrc_status,cohort,cohort_id,cohort_verification) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',records)
        audit('Student CSV imported','student',0,'',f'{len(records)} added'); conn.commit()
    except DB_INTEGRITY_ERROR:
        conn.rollback(); flash('The import was rejected by a database constraint. No students were imported; check register numbers and required fields.','error'); return redirect(url_for('students'))
    flash(f'Imported all {len(records)} students successfully.','success'); return redirect(url_for('students'))

@app.get('/students/<int:sid>')
@login_required
def student_detail(sid, issued_credential=None):
    dept=g.admin_department_id if g.admin['role']=='department_admin' else None
    dept_ids=g.admin_department_ids if dept else []
    dept_ph=','.join('?' for _ in dept_ids)
    history_scope=f' AND EXISTS(SELECT 1 FROM event_departments x WHERE x.event_id=a.event_id AND x.department_id IN ({dept_ph}))' if dept else ''
    event_scope=f' AND EXISTS(SELECT 1 FROM event_departments x WHERE x.event_id=e.id AND x.department_id IN ({dept_ph}))' if dept else ''
    parameters=(*dept_ids,*dept_ids,*dept_ids,sid) if dept else (sid,)
    query='SELECT s.*,(SELECT COUNT(DISTINCT a.event_id) FROM attendance a WHERE a.student_id=s.id AND a.status="Present"'+history_scope+') events,(SELECT MAX(e.event_date) FROM attendance a JOIN events e ON e.id=a.event_id WHERE a.student_id=s.id AND a.status="Present"'+history_scope+') last_event,(SELECT COUNT(*) FROM events e WHERE e.status="Completed"'+event_scope+') total_events FROM students s WHERE s.id=?'
    s=db().execute(query,parameters).fetchone()
    if not s: return redirect(url_for('students'))
    history=db().execute('SELECT e.name,e.event_date,e.semester,a.status,a.credit_points FROM attendance a JOIN events e ON e.id=a.event_id WHERE a.student_id=?'+(f' AND EXISTS(SELECT 1 FROM event_departments x WHERE x.event_id=e.id AND x.department_id IN ({dept_ph}))' if dept else '')+' ORDER BY e.event_date DESC',(sid,*dept_ids) if dept else (sid,)).fetchall()
    semester_progress=[{'semester':db().execute('SELECT * FROM semesters WHERE id=?',(n,)).fetchone(), 'result':semester_eligibility(n,sid)} for n in range(1,5)]
    db().commit()
    has_account=bool(db().execute('SELECT 1 FROM student_accounts WHERE student_id=?',(sid,)).fetchone())
    response=app.make_response(render_template('student_detail.html',student=s,history=history,semester_progress=semester_progress,issued_credential=issued_credential,has_student_account=has_account))
    if issued_credential:
        response.headers['Cache-Control']='no-store'
    return response

@app.route('/convenors', methods=['GET','POST'])
@central_admin_required
def convenor_management():
    conn=db()
    if request.method=='POST':
        action=request.form.get('action','')
        try:
            convenor_id=int(request.form.get('id','0'))
        except ValueError:
            convenor_id=0
        existing=conn.execute('SELECT * FROM student_convenors WHERE id=?',(convenor_id,)).fetchone() if convenor_id else None
        if action in ('save','hide') and not existing:
            return 'Student convenor not found',404
        if action=='hide':
            conn.execute('UPDATE student_convenors SET active=0 WHERE id=?',(convenor_id,))
            audit('Student convenor hidden','student_convenor',convenor_id,existing['name'],'')
            conn.commit()
            flash('Student convenor removed from the volunteer home.','success')
            return redirect(url_for('convenor_management'))
        if action not in ('add','save'):
            return 'Invalid student convenor action',400
        fields={
            'name':request.form.get('name','').strip(),
            'coordinator_title':request.form.get('coordinator_title','').strip(),
            'phone':request.form.get('phone','').strip(),
            'email':request.form.get('email','').strip(),
        }
        try: department_id=int(request.form.get('department_id',''))
        except (TypeError,ValueError): department_id=0
        department=conn.execute("SELECT id FROM departments WHERE id=? AND status='active'",(department_id,)).fetchone()
        try:
            sort_order=int(request.form.get('sort_order','0')) if action=='save' else None
            if not department:
                raise ValueError('Choose an active department to assign this contact to.')
            if not fields['name'] or len(fields['name'])>120:
                raise ValueError('Enter a contact name of at most 120 characters.')
            if not fields['coordinator_title'] or len(fields['coordinator_title'])>80:
                raise ValueError('Enter a coordinator title of at most 80 characters.')
            if len(fields['phone'])>40 or (fields['phone'] and not re.fullmatch(r'[+0-9(). -]+',fields['phone'])):
                raise ValueError('Enter a valid phone number using digits, spaces, +, parentheses, dots, or hyphens.')
            if len(fields['email'])>160 or (fields['email'] and not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+',fields['email'])):
                raise ValueError('Enter a valid email address.')
            if action=='save' and sort_order is not None and not 0<=sort_order<=10000:
                raise ValueError('Display order must be a whole number from 0 to 10000.')
        except ValueError as exc:
            flash(str(exc),'error')
            return redirect(url_for('convenor_management'))
        new_photo=None
        new_photo_data=None
        try:
            uploaded_photo=request.files.get('photo')
            if uploaded_photo and uploaded_photo.filename:
                new_photo,new_photo_data=process_convenor_photo(uploaded_photo)
        except ValueError as exc:
            flash(str(exc),'error')
            return redirect(url_for('convenor_management'))
        photo=new_photo or (existing['photo'] if existing else '')
        photo_data=new_photo_data if new_photo_data is not None else (existing['photo_data'] if existing else None)
        active=1 if action=='add' or request.form.get('active')=='1' else 0
        try:
            if action=='add':
                sort_order=conn.execute('SELECT COALESCE(MAX(sort_order),0)+1 FROM student_convenors').fetchone()[0]
                cur=conn.execute('''INSERT INTO student_convenors
                    (sort_order,name,role,department_id,coordinator_title,phone,email,photo,photo_data,active)
                    VALUES(?,?,?,?,?,?,?,?,?,?)''',
                    (sort_order,fields['name'],'DEPARTMENT CONTACT',department_id,fields['coordinator_title'],
                     fields['phone'],fields['email'],photo,photo_data,active))
                convenor_id=cur.lastrowid
                audit('Student convenor added','student_convenor',convenor_id,'',fields['name'])
            else:
                conn.execute('''UPDATE student_convenors SET name=?,department_id=?,coordinator_title=?,
                    phone=?,email=?,photo=?,photo_data=?,sort_order=?,active=? WHERE id=?''',
                    (fields['name'],department_id,fields['coordinator_title'],fields['phone'],
                     fields['email'],photo,photo_data,sort_order,active,convenor_id))
                audit('Student convenor updated','student_convenor',convenor_id,existing['name'],fields['name'])
            conn.commit()
        except DB_ERROR:
            conn.rollback()
            app.logger.exception('Could not save student convenor')
            flash('The student convenor could not be saved. Please try again.','error')
            return redirect(url_for('convenor_management'))
        flash('Student convenor saved.','success')
        return redirect(url_for('convenor_management'))
    convenors=conn.execute('''SELECT c.*,d.name department_name FROM student_convenors c
        LEFT JOIN departments d ON d.id=c.department_id ORDER BY c.sort_order,c.id''').fetchall()
    departments=conn.execute('SELECT id,name FROM departments WHERE status=\'active\' ORDER BY name').fetchall()
    leadership=conn.execute('''SELECT l.*,d.name department_name,a.username appointed_by_name
        FROM department_leadership_appointments l JOIN departments d ON d.id=l.department_id
        LEFT JOIN admins a ON a.id=l.appointed_by
        ORDER BY d.name,CASE l.leadership_role WHEN 'department_leader' THEN 0 ELSE 1 END,l.created_at DESC,l.id DESC''').fetchall()
    photo_sources=conn.execute('SELECT id,name,photo FROM student_convenors WHERE active=1 ORDER BY sort_order,id').fetchall()
    return render_template('convenors.html',convenors=convenors,departments=departments,leadership=leadership,photo_sources=photo_sources,today=date.today().isoformat())

@app.post('/convenors/department')
@central_admin_required
def department_leadership_appoint():
    conn=db()
    try: department_id=int(request.form.get('department_id',''))
    except (TypeError,ValueError): department_id=0
    department=conn.execute("SELECT id,name FROM departments WHERE id=? AND status='active'",(department_id,)).fetchone()
    position=request.form.get('position','')
    name=request.form.get('name','').strip()
    designation=request.form.get('designation','').strip()
    phone=request.form.get('phone','').strip()
    email=request.form.get('email','').strip()
    active_from=request.form.get('active_from','').strip() or date.today().isoformat()
    active_until=request.form.get('active_until','').strip() or None
    try:
        from_date=date.fromisoformat(active_from)
        until_date=date.fromisoformat(active_until) if active_until else None
        valid_dates=(from_date<=date.today() and (not until_date or until_date>=date.today()))
    except ValueError:
        valid_dates=False
    valid_email=not email or (len(email)<=160 and re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+',email))
    if (not department or position not in ('department_leader','department_secretary') or
        not name or len(name)>120 or not designation or len(designation)>80 or len(phone)>40 or
        (phone and not re.fullmatch(r'[+0-9(). -]+',phone)) or not valid_email or not valid_dates):
        flash('Check the department, leadership role, profile details, contact information, and appointment dates.','error')
        return redirect(url_for('convenor_management'))
    photo=''; photo_data=None
    source_id=request.form.get('photo_source_id','').strip()
    if source_id.isdigit():
        source=conn.execute('SELECT photo,photo_data FROM student_convenors WHERE id=? AND active=1',(int(source_id),)).fetchone()
        if source:
            if source['photo_data'] is not None:
                photo='uploads/convenors/'+secrets.token_hex(16)+'.jpg'
                photo_data=bytes(source['photo_data'])
            else:
                photo=source['photo']
    try:
        upload=request.files.get('photo')
        if upload and upload.filename:
            photo,photo_data=process_convenor_photo(upload)
    except ValueError as exc:
        flash(str(exc),'error'); return redirect(url_for('convenor_management'))
    try:
        conn.execute('''UPDATE department_leadership_appointments
            SET status='retired',
                active_until=CASE WHEN active_until IS NULL OR active_until>=? THEN ? ELSE active_until END,
                updated_at=CURRENT_TIMESTAMP
            WHERE department_id=? AND leadership_role=? AND status='active' ''',
            (from_date.isoformat(),(from_date-date.resolution).isoformat(),department_id,position))
        appointment_columns={row[1] for row in conn.execute('PRAGMA table_info(department_leadership_appointments)')}
        legacy_position_column=',position' if 'position' in appointment_columns else ''
        legacy_position_value=',?' if 'position' in appointment_columns else ''
        legacy_position_arg=(position,) if 'position' in appointment_columns else ()
        cur=conn.execute(f'''INSERT INTO department_leadership_appointments
            (department_id{legacy_position_column},leadership_role,name,designation,phone,email,photo,photo_data,status,active_from,active_until,appointed_by)
            VALUES(?{legacy_position_value},?,?,?,?,?,?,?,'active',?,?,?)''',
            (department_id,*legacy_position_arg,position,name,designation,phone,email,photo,photo_data,from_date.isoformat(),active_until,session['admin_id']))
        audit('Department leadership appointed','department_leadership',cur.lastrowid,'',f'{department["name"]}; {position}; {name}')
        conn.commit()
    except DB_ERROR:
        conn.rollback(); app.logger.exception('Could not appoint department leadership')
        flash('The appointment could not be saved. Please try again.','error')
        return redirect(url_for('convenor_management'))
    flash(f'{name} was appointed as {designation} for {department["name"]}.','success')
    return redirect(url_for('convenor_management'))

@app.post('/convenors/department/<int:appointment_id>/save')
@central_admin_required
def department_leadership_save(appointment_id):
    conn=db(); current=conn.execute('SELECT * FROM department_leadership_appointments WHERE id=?',(appointment_id,)).fetchone()
    if not current: return 'Leadership appointment not found.',404
    if current['status']!='active':
        flash('Retired appointments remain in history and cannot be reactivated. Make a new appointment instead.','error')
        return redirect(url_for('convenor_management'))
    name=request.form.get('name','').strip(); designation=request.form.get('designation','').strip()
    phone=request.form.get('phone','').strip(); email=request.form.get('email','').strip()
    active_from=request.form.get('active_from','').strip() or current['active_from']
    active_until=request.form.get('active_until','').strip() or None
    try:
        from_date=date.fromisoformat(active_from)
        until_date=date.fromisoformat(active_until) if active_until else None
        valid_dates=(from_date<=date.today() and (not until_date or until_date>=date.today()))
    except ValueError: valid_dates=False
    valid_email=not email or (len(email)<=160 and re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+',email))
    if not name or len(name)>120 or not designation or len(designation)>80 or len(phone)>40 or (phone and not re.fullmatch(r'[+0-9(). -]+',phone)) or not valid_email or not valid_dates:
        flash('Check the profile details, contact information, and appointment dates.','error')
        return redirect(url_for('convenor_management'))
    photo=current['photo']; photo_data=current['photo_data']
    try:
        upload=request.files.get('photo')
        if upload and upload.filename: photo,photo_data=process_convenor_photo(upload)
    except ValueError as exc:
        flash(str(exc),'error'); return redirect(url_for('convenor_management'))
    conn.execute('''UPDATE department_leadership_appointments SET name=?,designation=?,phone=?,email=?,photo=?,photo_data=?,
        active_from=?,active_until=?,updated_at=CURRENT_TIMESTAMP WHERE id=?''',
        (name,designation,phone,email,photo,photo_data,from_date.isoformat(),active_until,appointment_id))
    audit('Department leadership profile updated','department_leadership',appointment_id,current['name'],name)
    conn.commit(); flash('Leadership profile updated.','success')
    return redirect(url_for('convenor_management'))

@app.post('/convenors/department/<int:appointment_id>/retire')
@central_admin_required
def department_leadership_retire(appointment_id):
    conn=db(); current=conn.execute('SELECT name,status FROM department_leadership_appointments WHERE id=?',(appointment_id,)).fetchone()
    if not current: return 'Leadership appointment not found.',404
    if current['status']=='active':
        conn.execute("UPDATE department_leadership_appointments SET status='retired',active_until=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                     (date.today().isoformat(),appointment_id))
        audit('Department leadership appointment retired','department_leadership',appointment_id,current['name'],'retired')
        conn.commit()
    flash('Appointment retired. Its history has been preserved.','success')
    return redirect(url_for('convenor_management'))

@app.get('/volunteer/convenor-photos/<filename>')
def student_convenor_photo(filename):
    if not re.fullmatch(r'[0-9a-f]{32}\.jpg',filename):
        return 'Photo not found',404
    if session.get('admin_id'):
        admin=db().execute('''SELECT a.status,a.session_token,r.role FROM admins a
            JOIN admin_roles r ON r.admin_id=a.id WHERE a.id=?''',(session['admin_id'],)).fetchone()
        token=session.get('admin_session_token','')
        if not admin or admin['status']!='active' or not token or not secrets.compare_digest(admin['session_token'],token):
            session.clear(); return '',401
        if admin['role']!='super_admin': return 'Photo not found',404
    photo=db().execute('SELECT photo_data,active FROM student_convenors WHERE photo=?',('uploads/convenors/'+filename,)).fetchone()
    if photo and photo['photo_data'] is not None:
        if not session.get('admin_id'): return 'Photo not found',404
        response=Response(bytes(photo['photo_data']),mimetype='image/jpeg')
        response.headers['Cache-Control']='private, max-age=3600'
        return response
    photo=db().execute('''SELECT photo_data,department_id,status,active_from,active_until
        FROM department_leadership_appointments WHERE photo=?''',('uploads/convenors/'+filename,)).fetchone()
    if not photo or photo['photo_data'] is None:
        return 'Photo not found',404
    if not session.get('admin_id'):
        student_id=session.get('student_id')
        if not student_id: return '',401
        account=db().execute('''SELECT a.session_token,s.yrc_status,s.department_id,s.department_verification FROM student_accounts a
            JOIN students s ON s.id=a.student_id WHERE a.student_id=?''',(student_id,)).fetchone()
        token=session.get('student_session_token','')
        if not account or account['yrc_status']!='Active' or not token or not secrets.compare_digest(account['session_token'],token):
            return '',401
        today=date.today().isoformat()
        if (account['department_verification']!='verified' or account['department_id']!=photo['department_id'] or
            photo['status']!='active' or (photo['active_from'] and photo['active_from']>today) or
            (photo['active_until'] and photo['active_until']<today)):
            return 'Photo not found',404
    response=Response(bytes(photo['photo_data']),mimetype='image/jpeg')
    response.headers['Cache-Control']='private, max-age=3600'
    return response

@app.post('/students/<int:sid>/credentials')
@login_required
def issue_student_credentials(sid):
    student=db().execute('SELECT id,register_no FROM students WHERE id=?',(sid,)).fetchone()
    if not student: return 'Student not found',404
    password='KJU'
    hashed=generate_password_hash(password)
    token=secrets.token_urlsafe(32)
    db().execute('INSERT INTO student_accounts(student_id,password_hash,session_token) VALUES(?,?,?) ON CONFLICT(student_id) DO UPDATE SET password_hash=excluded.password_hash,session_token=excluded.session_token,updated_at=CURRENT_TIMESTAMP',(sid,hashed,token))
    audit('Student portal credentials issued','student',sid,'','password reset')
    db().commit()
    credential={'username':student['register_no'],'password':password}
    return student_detail(sid,issued_credential=credential)

@app.route('/students/credentials/bulk', methods=['GET','POST'])
@login_required
def student_credentials_bulk():
    conn=db()
    if request.method=='GET':
        student_count=conn.execute('SELECT COUNT(*) FROM students').fetchone()[0]
        return render_template('student_credentials_bulk.html',student_count=student_count,issued_count=None,issued_rows=None)
    if request.form.get('confirm')!='issue':
        flash('Confirm the bulk password reset before continuing.','error')
        return redirect(url_for('student_credentials_bulk'))
    student_count=conn.execute('SELECT COUNT(*) FROM students').fetchone()[0]
    password='KJU'
    password_hash=generate_password_hash(password)
    token=secrets.token_urlsafe(32)
    try:
        conn.execute('''INSERT INTO student_accounts(student_id,password_hash,session_token)
            SELECT id,?,? FROM students WHERE TRUE
            ON CONFLICT(student_id) DO UPDATE SET password_hash=excluded.password_hash,
            session_token=excluded.session_token,updated_at=CURRENT_TIMESTAMP''',
            (password_hash,token))
        audit('Bulk student portal credentials issued','student',0,'',f'{student_count} accounts')
        conn.commit()
    except DB_ERROR:
        conn.rollback()
        app.logger.exception('Could not issue bulk student portal credentials')
        flash('The bulk password reset failed. No credentials were issued; please try again.','error')
        return redirect(url_for('student_credentials_bulk'))
    response=app.make_response(render_template(
        'student_credentials_bulk.html',
        student_count=student_count,
        issued_count=student_count,
        issued_rows=[]))
    response.headers['Cache-Control']='no-store, private'
    response.headers['Pragma']='no-cache'
    response.headers['Expires']='0'
    return response

@app.post('/students/<int:sid>/status')
@login_required
def student_status(sid):
    value=request.form['status']; db().execute('UPDATE students SET yrc_status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',(value,sid)); audit('Student status changed','student',sid,'',value); db().commit(); return redirect(url_for('student_detail',sid=sid))

@app.post('/students/<int:sid>/delete')
@login_required
def student_delete(sid):
    conn=db(); student=conn.execute('SELECT register_no,name,yrc_status FROM students WHERE id=?',(sid,)).fetchone()
    if not student:
        flash('Student was already deleted.','error'); return redirect(url_for('students'))
    conn.execute("UPDATE students SET yrc_status='Inactive',updated_at=CURRENT_TIMESTAMP WHERE id=?",(sid,))
    audit('Student deactivated','student',sid,student['yrc_status'],'Inactive')
    conn.commit(); flash('Student deactivated. Historical participation records were preserved.','success')
    return redirect(url_for('students'))

@app.post('/students/<int:sid>/remove')
@login_required
def student_remove(sid):
    conn=db(); student=conn.execute('SELECT register_no,name,yrc_status FROM students WHERE id=?',(sid,)).fetchone()
    if not student:
        flash('Student was not found.','error'); return redirect(url_for('students'))
    try:
        register_key=normalize_register_no(student['register_no'])
        staged_rows=conn.execute('SELECT id,register_no,student_id FROM participation_import_rows').fetchall()
        for row in staged_rows:
            if row['student_id']==sid or normalize_register_no(row['register_no'])==register_key:
                conn.execute('DELETE FROM participation_import_rows WHERE id=?',(row['id'],))
        conn.execute('DELETE FROM attendance WHERE student_id=?',(sid,))
        conn.execute('DELETE FROM event_participants WHERE student_id=?',(sid,))
        conn.execute('DELETE FROM selection_history WHERE student_id=?',(sid,))
        conn.execute("DELETE FROM admin_audit_logs WHERE entity_type='student' AND entity_id=?",(sid,))
        conn.execute("""DELETE FROM admin_audit_logs WHERE
            (entity_type='attendance' AND new_value=?) OR
            (entity_type='event' AND action IN ('Manual participant selection','Participant deselected') AND new_value=?)""",
            (str(sid),str(sid)))
        conn.execute('DELETE FROM students WHERE id=?',(sid,))
        conn.commit()
    except DB_ERROR:
        conn.rollback(); flash('The student and history could not be deleted. No partial changes were saved.','error')
        return redirect(url_for('students'))
    flash('Student and related history permanently deleted.','success')
    return redirect(url_for('students'))

@app.post('/students/<int:sid>/edit')
@login_required
def student_edit(sid):
    f=request.form; conn=db()
    if f.get('section','').strip().upper() not in tuple('ABCDEFG'):
        flash('Section must be a letter from A through G.','error'); return redirect(url_for('student_detail',sid=sid))
    try: year=int(f.get('year',''))
    except (TypeError,ValueError):
        flash('Choose a valid year of study.','error'); return redirect(url_for('student_detail',sid=sid))
    if year not in range(1,9) or any(not f.get(k,'').strip() for k in ('register_no','name','department','class_name','cohort')) or len(f.get('cohort','').strip())>100:
        flash('Enter all required student details and choose a year from 1 to 8.','error'); return redirect(url_for('student_detail',sid=sid))
    department_record=department_for_label(conn,f.get('department',''))
    if not department_record:
        flash('Choose a registered active department. Add it in Department management first.','error'); return redirect(url_for('student_detail',sid=sid))
    if g.admin['role']=='department_admin' and department_record['id'] not in g.admin_department_ids:
        return 'Volunteer not found.',404
    try:
        cohort=f['cohort'].strip(); existing=conn.execute('SELECT cohort,cohort_id,cohort_verification FROM students WHERE id=?',(sid,)).fetchone()
        if not existing:
            flash('Student record was not found.','error'); return redirect(url_for('students'))
        if cohort!=existing['cohort']:
            cohort_id=ensure_cohort(conn,cohort); cohort_verification='manual'
        else:
            cohort_id=existing['cohort_id']; cohort_verification=existing['cohort_verification']
        if g.admin['role']=='department_admin' and cohort_id:
            conn.execute('INSERT OR IGNORE INTO cohort_departments(cohort_id,department_id) VALUES(?,?)',(cohort_id,department_record['id']))
        conn.execute('''UPDATE students SET register_no=?,name=?,department=?,department_id=?,department_verification='verified',class_name=?,section=?,year=?,email=?,phone=?,cohort=?,cohort_id=?,cohort_verification=?,updated_at=CURRENT_TIMESTAMP WHERE id=?''',(f['register_no'].strip(),f['name'].strip(),department_record['name'],department_record['id'],f['class_name'].strip(),f['section'].strip().upper(),year,f.get('email'),f.get('phone'),cohort,cohort_id,cohort_verification,sid))
        audit('Student edited','student',sid,'',f'profile updated; cohort {cohort} ({cohort_verification})'); conn.commit(); flash('Student profile updated.','success')
    except DB_INTEGRITY_ERROR: conn.rollback(); flash('Register number already belongs to another student.','error')
    return redirect(url_for('student_detail',sid=sid))

@app.route('/events',methods=['GET','POST'])
@login_required
def events():
    conn=db()
    if request.method=='POST':
        f=request.form
        name=f.get('name','').strip()
        semester=f.get('semester','')
        try:
            date.fromisoformat(f.get('event_date',''))
            capacity=int(f.get('max_participants') or 0) or None
        except (TypeError,ValueError):
            flash('Enter a valid date and seat capacity.','error'); return redirect(url_for('events'))
        status=f.get('status','Upcoming')
        if (semester not in ('1','2','3','4') or not name or (capacity is not None and capacity<1) or
            status not in ('Draft','Upcoming','Open','Closed','Completed','Cancelled')):
            flash('Enter the event details and choose a semester from 1 to 4.','error'); return redirect(url_for('events'))
        cur=conn.execute('''INSERT INTO events(name,description,event_date,start_time,end_time,venue,max_participants,deadline,status,semester)
            VALUES(?,?,?,?,?,?,?,?,?,?)''',(name,f.get('description'),f['event_date'],f.get('start_time'),f.get('end_time'),f.get('venue'),capacity,f.get('deadline'),status,int(semester)))
        if g.admin['role']=='department_admin':
            conn.execute('INSERT INTO event_departments(event_id,department_id,assigned_by) VALUES(?,?,?)',(cur.lastrowid,g.admin_department_id,session['admin_id']))
        audit('Event created','event',cur.lastrowid,'',f['name']); conn.commit(); flash('Event created.','success'); return redirect(url_for('events'))
    q=request.args.get('q','').strip(); status=request.args.get('status','All')
    semester=request.args.get('semester','All')
    where=[]; args=[]
    if q: where.append('(e.name LIKE ? OR e.venue LIKE ?)'); args.extend((f'%{q}%',f'%{q}%'))
    if status in ('Draft','Upcoming','Open','Closed','Completed','Cancelled'): where.append('e.status=?'); args.append(status)
    if semester in ('1','2','3','4'): where.append('e.semester=?'); args.append(int(semester))
    elif semester=='Unassigned': where.append('e.semester IS NULL')
    else: semester='All'
    if g.admin['role']=='department_admin':
        where.append(f"EXISTS(SELECT 1 FROM event_departments x WHERE x.event_id=e.id AND x.department_id IN ({','.join('?' for _ in g.admin_department_ids)}))"); args.extend(g.admin_department_ids)
        dept_literal=','.join(str(int(x)) for x in g.admin_department_ids)
        participant_scope=f' JOIN students ps ON ps.id=p.student_id AND ps.department_id IN ({dept_literal})'
    else: participant_scope=''
    attendance_scope=f' AND ats.department_id IN ({dept_literal})' if g.admin['role']=='department_admin' else ''
    rows=conn.execute('SELECT e.*,(SELECT COUNT(*) FROM event_participants p'+participant_scope+' WHERE p.event_id=e.id AND p.selection_status="Selected") selected,(SELECT COUNT(*) FROM attendance a JOIN students ats ON ats.id=a.student_id'+attendance_scope+' WHERE a.event_id=e.id AND a.status="Present") present FROM events e'+(' WHERE '+' AND '.join(where) if where else '')+' ORDER BY e.event_date DESC',args).fetchall()
    summary=conn.execute("SELECT COUNT(*) total, SUM(CASE WHEN status IN ('Upcoming','Open') THEN 1 ELSE 0 END) active, SUM(CASE WHEN status='Cancelled' THEN 1 ELSE 0 END) cancelled FROM events e"+(f" WHERE EXISTS(SELECT 1 FROM event_departments x WHERE x.event_id=e.id AND x.department_id IN ({','.join('?' for _ in g.admin_department_ids)}))" if g.admin['role']=='department_admin' else ''),tuple(g.admin_department_ids) if g.admin['role']=='department_admin' else ()).fetchone()
    return render_template('events.html',events=rows,q=q,status=status,semester=semester,summary=summary)

@app.get('/attendance')
@login_required
def attendance_page():
    ids=g.admin_department_ids if g.admin['role']=='department_admin' else []
    ph=','.join('?' for _ in ids)
    event_scope=f"EXISTS(SELECT 1 FROM event_departments x WHERE x.event_id=e.id AND x.department_id IN ({ph}))" if ids else '1=1'
    student_scope=f's.department_id IN ({ph})' if ids else '1=1'
    rows=db().execute('''SELECT e.*,
        (SELECT COUNT(*) FROM event_participants p JOIN students s ON s.id=p.student_id WHERE p.event_id=e.id AND p.selection_status='Selected' AND '''+student_scope+''') selected,
        (SELECT COUNT(*) FROM attendance a JOIN event_participants p ON p.event_id=a.event_id AND p.student_id=a.student_id JOIN students s ON s.id=a.student_id
          WHERE a.event_id=e.id AND p.selection_status='Selected' AND '''+student_scope+''') marked,
        (SELECT COUNT(*) FROM attendance a JOIN event_participants p ON p.event_id=a.event_id AND p.student_id=a.student_id JOIN students s ON s.id=a.student_id
          WHERE a.event_id=e.id AND a.status='Present' AND p.selection_status='Selected' AND '''+student_scope+''') present
        FROM events e WHERE e.status<>'Cancelled' AND '''+event_scope+''' ORDER BY e.event_date DESC,e.name''',(*ids,*ids,*ids,*ids)).fetchall()
    pending=sum(max(0,r['selected']-r['marked']) for r in rows)
    summary=db().execute('''SELECT COUNT(DISTINCT a.student_id) unique_students, COUNT(*) attendance_records
        FROM attendance a JOIN events e ON e.id=a.event_id JOIN students s ON s.id=a.student_id
        WHERE a.status='Present' AND e.status<>'Cancelled' AND '''+student_scope,tuple(ids)).fetchone()
    return render_template('attendance.html',events=rows,pending=pending,summary=summary)

@app.get('/attendance/export/<int:eid>.csv')
@login_required
def attendance_event_csv(eid):
    conn=db()
    event=conn.execute("SELECT id,name,event_date FROM events WHERE id=? AND status<>'Cancelled'",(eid,)).fetchone()
    if not event:
        flash('That event is unavailable for attendance export.','error')
        return redirect(url_for('attendance_page'))
    export_scope=f" AND s.department_id IN ({','.join('?' for _ in g.admin_department_ids)})" if g.admin['role']=='department_admin' else ''
    export_args=(eid,*g.admin_department_ids) if g.admin['role']=='department_admin' else (eid,)
    students=conn.execute('''SELECT s.register_no,s.name,s.department,s.class_name,s.section,a.status,a.credit_points,e.semester
        FROM attendance a JOIN students s ON s.id=a.student_id
        JOIN events e ON e.id=a.event_id
        JOIN event_participants p ON p.event_id=a.event_id AND p.student_id=a.student_id AND p.selection_status='Selected'
        WHERE a.event_id=? AND a.status='Present' '''+export_scope+'''
        ORDER BY s.name,s.register_no''',export_args).fetchall()
    output=io.StringIO(newline='')
    writer=csv.writer(output)
    writer.writerow(['Event','Event Date','Semester','Register Number','Student Name','Department','Class','Section','Attendance','Credit Points'])
    for student in students:
        values=[event['name'],event['event_date'],f"Semester {student['semester']}" if student['semester'] else 'Unassigned',
                student['register_no'],student['name'],student['department'],student['class_name'],
                student['section'],student['status'],student['credit_points']]
        writer.writerow([("'"+str(value) if str(value or '').lstrip().startswith(('=','+','-','@')) else value)
                         for value in values])
    filename=f"event-{eid}-attended-students.csv"
    return Response('\ufeff'+output.getvalue(),mimetype='text/csv; charset=utf-8',
                    headers={'Content-Disposition':f'attachment; filename="{filename}"'})

def normalize_import_key(value):
    return re.sub(r'[^a-z0-9]+',' ',str(value or '').casefold()).strip()

def normalize_register_no(value):
    return re.sub(r'[^A-Z0-9]','',str(value or '').upper())

def cohort_mapping_for_register(conn, register_no):
    normalized=normalize_register_no(register_no)
    if not re.fullmatch(r'[0-9]{2}[A-Z0-9]{4,}',normalized):
        return None
    mappings=conn.execute('''SELECT m.prefix,c.id,c.name FROM cohort_prefix_mappings m
        JOIN cohorts c ON c.id=m.cohort_id WHERE m.active=1 AND c.status='active' ORDER BY LENGTH(m.prefix) DESC,m.prefix''').fetchall()
    return next((mapping for mapping in mappings if normalized.startswith(mapping['prefix'])),None)

def ensure_cohort(conn, name, admission_year=None):
    label=' '.join(str(name or '').split()).strip()
    if not label:
        return None
    conn.execute("INSERT OR IGNORE INTO cohorts(name,admission_year,status) VALUES(?,?,'active')",(label,admission_year))
    return conn.execute('SELECT id FROM cohorts WHERE name=?',(label,)).fetchone()['id']

def normalize_student_name(value):
    ascii_name=unicodedata.normalize('NFKD',str(value or '')).encode('ascii','ignore').decode('ascii').casefold()
    return re.sub(r'[^a-z0-9]+',' ',ascii_name).strip()

def resolve_import_student(register_no,student_name,roster_by_reg,roster_by_name):
    register_key=normalize_register_no(register_no)
    register_matches=roster_by_reg.get(register_key,[]) if register_key else []
    if len(register_matches)==1: return register_matches[0],'register_no',''
    if len(register_matches)>1: return None,'','Register number matches multiple roster records.'
    name_key=normalize_student_name(student_name)
    name_matches=roster_by_name.get(name_key,[]) if name_key else []
    if len(name_matches)==1: return name_matches[0],'name',''
    if len(name_matches)>1: return None,'','Name matches multiple roster records; review manually.'
    return None,'','No exact register number or name match.'

def canonical_event_name(value):
    label=' '.join(str(value or '').split()).strip()[:120]
    key=normalize_import_key(label)
    if 'ethnic day' in key: return 'Ethnic Day'
    aliases={
        'drug awareness':'Drug Awareness',
        'tatva':'TATVA',
        'orientation':'Orientation',
        'first aid':'First Aid',
        'park cleaning drive':'Park Cleaning Drive',
    }
    return aliases.get(key,label)

def parse_event_choices(value):
    choices=[]
    for part in re.split(r'[,;\n]+',str(value or '')):
        item=' '.join(part.split()).strip()
        if not item or normalize_import_key(item) in ('none','none of the above','no activities','other','other please specify'):
            continue
        label=canonical_event_name(item)
        if label and label not in choices: choices.append(label)
    return choices

def parse_sheet_timestamp(value, fallback):
    raw=str(value or '').strip()
    for fmt in ('%m/%d/%Y %H:%M:%S','%m/%d/%Y %I:%M:%S %p','%d/%m/%Y %H:%M:%S','%Y-%m-%d %H:%M:%S'):
        try: return (datetime.strptime(raw,fmt),fallback)
        except ValueError: pass
    try: return (datetime.fromisoformat(raw.replace('Z','+00:00')).replace(tzinfo=None),fallback)
    except ValueError: return (datetime.min,fallback)

def participation_import_preview(batch_id):
    conn=db()
    batch=conn.execute('SELECT * FROM participation_import_batches WHERE id=?',(batch_id,)).fetchone()
    if not batch or batch['status']!='Preview': return None
    source_rows=conn.execute('SELECT * FROM participation_import_rows WHERE batch_id=? ORDER BY event_name,student_name',(batch_id,)).fetchall()
    active_events=conn.execute("SELECT * FROM events WHERE status<>'Cancelled' ORDER BY event_date,name").fetchall()
    roster_names={row['id']:row['name'] for row in conn.execute('SELECT id,name FROM students').fetchall()}
    grouped={}
    for row in source_rows: grouped.setdefault(row['event_name'],[]).append(row)
    summaries=[]
    unmatched=[]
    duplicates=[]
    importable_count=0
    duplicate_count=0
    ambiguous_count=0
    upload_names_by_event={}
    name_match_count=sum(1 for row in source_rows if row['student_id'] and row['match_method']=='name')
    for event_name,rows in sorted(grouped.items()):
        key=normalize_import_key(canonical_event_name(event_name))
        matches=[event for event in active_events if normalize_import_key(canonical_event_name(event['name']))==key]
        matched_event=matches[0] if len(matches)==1 else None
        ambiguous=len(matches)>1
        if ambiguous: ambiguous_count+=1
        upload_event_key=matched_event['id'] if matched_event else key
        upload_names=upload_names_by_event.setdefault(upload_event_key,{})
        existing_students={}
        existing_names={}
        if matched_event:
            existing_rows=conn.execute('''SELECT DISTINCT s.id,s.name
                FROM event_participants p JOIN students s ON s.id=p.student_id
                WHERE p.event_id=?
                UNION
                SELECT DISTINCT s.id,s.name
                FROM attendance a JOIN students s ON s.id=a.student_id
                WHERE a.event_id=?''',(matched_event['id'],matched_event['id'])).fetchall()
            existing_students={row['id']:row['name'] for row in existing_rows}
            existing_names={normalize_student_name(row['name']):row['name'] for row in existing_rows
                            if normalize_student_name(row['name'])}
        ready=[]
        event_duplicates=0
        event_unmatched=0
        for row in rows:
            if not row['student_id']:
                event_unmatched+=1; unmatched.append(row); continue
            name_key=normalize_student_name(roster_names.get(row['student_id'],row['student_name']))
            reason=''
            if matched_event and row['student_id'] in existing_students:
                reason=f"{existing_students[row['student_id']]} is already in this event."
            elif matched_event and name_key in existing_names:
                reason=f"The name already appears in this event as {existing_names[name_key]}."
            elif name_key and name_key in upload_names:
                reason=f"The same name appears more than once for this event in the CSV (first roll number: {upload_names[name_key]})."
            if reason:
                event_duplicates+=1
                duplicates.append({**dict(row),'duplicate_reason':reason})
                continue
            if name_key:
                upload_names[name_key]=row['register_no'] or row['student_id']
            ready.append(row)
        importable_count+=len(ready)
        summaries.append({'name':event_name,'rows':len(rows),'matched':len(rows)-event_unmatched,'unmatched':event_unmatched,
                          'already':event_duplicates,'to_import':len(ready),'event':matched_event,
                          'ambiguous':ambiguous,'date_field':'event_date_'+str(len(summaries))})
        duplicate_count+=event_duplicates
    return {'batch':batch,'events':summaries,'unmatched':unmatched,'unmatched_count':len(unmatched),
            'duplicates':duplicates,'duplicate_count':duplicate_count,
            'importable_count':importable_count,'already_count':duplicate_count,'ambiguous_count':ambiguous_count,
            'name_match_count':name_match_count}

@app.route('/attendance/import',methods=['GET','POST'])
@login_required
def participation_import():
    conn=db()
    if request.method=='GET':
        conn.execute('DELETE FROM participation_import_rows WHERE batch_id IN (SELECT id FROM participation_import_batches WHERE status="Preview" AND created_at < datetime("now","-1 day"))')
        conn.execute('UPDATE participation_import_batches SET status="Expired" WHERE status="Preview" AND created_at < datetime("now","-1 day")')
        conn.commit()
        recent=conn.execute('SELECT * FROM participation_import_batches ORDER BY created_at DESC LIMIT 8').fetchall()
        return render_template('participation_import.html',preview=None,recent=recent)
    upload=request.files.get('file')
    if not upload or not upload.filename:
        flash('Choose a Google Sheets CSV export first.','error'); return redirect(url_for('participation_import'))
    raw=upload.read()
    try: text=raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        try: text=raw.decode('cp1252')
        except UnicodeDecodeError:
            flash('The CSV encoding could not be read. Export the sheet as CSV and try again.','error'); return redirect(url_for('participation_import'))
    try: dialect=csv.Sniffer().sniff(text[:8192],delimiters=',;\t')
    except csv.Error: dialect=csv.excel
    reader=csv.DictReader(io.StringIO(text),dialect=dialect)
    headers=reader.fieldnames or []
    normalized={re.sub(r'[^a-z0-9]+',' ',h.casefold()).strip():h for h in headers}
    name_col=next((h for key,h in normalized.items() if 'full name' in key or 'student name' in key or key in ('name','your name')),None)
    register_col=next((h for key,h in normalized.items() if ((('register' in key or 'registration' in key or 'roll' in key) and ('number' in key or 'no' in key)) or key in ('usn','usn no'))),None)
    activity_col=next((h for key,h in normalized.items() if 'participated in so far' in key or
                       ('yrc' in key and ('activity' in key or 'event' in key)) or
                       ('activities' in key and ('participat' in key or 'attend' in key))),None)
    event_yes_cols=[]
    for header in headers:
        cleaned=re.sub(r'^\s*\d+\s*[.)-]?\s*','',header).strip()
        match=re.match(r'(?:did you\s+)?participat(?:e|ed)\s+in\s+(.+?)[?!.]*\s*$',cleaned,re.I)
        if match and header!=activity_col:
            event_label=canonical_event_name(match.group(1).strip())
            if event_label and normalize_import_key(event_label) not in ('so far','any events','any activities'):
                event_yes_cols.append((header,event_label))
    timestamp_col=next((h for key,h in normalized.items() if key=='timestamp'),None)
    other_col=next((h for key,h in normalized.items() if key=='column 1'),None)
    if not name_col or not register_col or (not activity_col and not event_yes_cols):
        if 'event name' in normalized and 'attendee count' in normalized:
            flash('This CSV contains event totals, not individual student responses. It has no student names or register numbers, so attendance cannot be assigned. Export the original Google Forms responses tab as CSV and upload that file. No data was imported.','error')
            return redirect(url_for('participation_import'))
        found=', '.join(headers[:8]) or 'no headers found'
        flash(f'Could not identify Full Name, Register Number, and an activity column or event participation column. Headers found: {found}. No data was imported.','error')
        return redirect(url_for('participation_import'))
    def choices_from_row(row):
        choices=parse_event_choices(row.get(activity_col,'')) if activity_col else []
        if other_col:
            choices.extend(choice for choice in parse_event_choices(row.get(other_col,'')) if choice not in choices)
        for header,event_label in event_yes_cols:
            if normalize_import_key(row.get(header,'')) in ('yes','y','true','1','participated','present') and event_label not in choices:
                choices.append(event_label)
        return choices
    responses=[]
    missing_register_rows=[]
    response_count=0
    for row_number,row in enumerate(reader,start=2):
        if not any(str(value or '').strip() for value in row.values()): continue
        response_count+=1
        register_no=' '.join(str(row.get(register_col) or '').split())
        name=' '.join(str(row.get(name_col) or '').split())
        if not register_no:
            choices=choices_from_row(row)
            missing_register_rows.append({'row_number':row_number,'name':name,'choices':choices})
            continue
        key=normalize_register_no(register_no)
        choices=choices_from_row(row)
        responses.append({'row_number':row_number,'register_no':register_no,'register_key':key,'name':name,
                          'choices':choices,'timestamp':str(row.get(timestamp_col) or '') if timestamp_col else ''})
    if not responses and not any(item['choices'] for item in missing_register_rows):
        flash('No usable responses with register numbers were found. No data was imported.','error'); return redirect(url_for('participation_import'))
    latest={}
    for response in responses:
        previous=latest.get(response['register_key'])
        if not previous or parse_sheet_timestamp(response['timestamp'],response['row_number'])>parse_sheet_timestamp(previous['timestamp'],previous['row_number']):
            latest[response['register_key']]=response
    roster_by_reg={}; roster_by_name={}
    for student in conn.execute('SELECT id,register_no,name FROM students').fetchall():
        roster_by_reg.setdefault(normalize_register_no(student['register_no']),[]).append(student['id'])
        roster_by_name.setdefault(normalize_student_name(student['name']),[]).append(student['id'])
    flat=[]
    for response in latest.values():
        seen=set()
        for event_name in response['choices']:
            if event_name in seen: continue
            seen.add(event_name)
            student_id,match_method,error=resolve_import_student(response['register_no'],response['name'],roster_by_reg,roster_by_name)
            flat.append((response['row_number'],response['register_no'],response['name'],event_name,student_id,error,match_method))
    for response in missing_register_rows:
        for event_name in response['choices']:
            student_id,match_method,error=resolve_import_student('',response['name'],roster_by_reg,roster_by_name)
            flat.append((response['row_number'],'',response['name'],event_name,student_id,error or 'Register number is missing; matched by exact name.',match_method))
    if not flat:
        flash('The responses contain no selected YRC activities to import.','error'); return redirect(url_for('participation_import'))
    batch_id=secrets.token_urlsafe(24)
    try:
        conn.execute('DELETE FROM participation_import_rows WHERE batch_id IN (SELECT id FROM participation_import_batches WHERE status="Preview" AND created_at < datetime("now","-1 day"))')
        conn.execute('DELETE FROM participation_import_batches WHERE status="Preview" AND created_at < datetime("now","-1 day")')
        conn.execute('INSERT INTO participation_import_batches(id,admin_id,filename,response_count,unique_respondents,repeated_respondents,missing_register_count,record_count,status) VALUES(?,?,?,?,?,?,?,?,?)',
                     (batch_id,session['admin_id'],os.path.basename(upload.filename)[:180],response_count,len(latest),len(responses)-len(latest),len(missing_register_rows),len(flat),'Preview'))
        conn.executemany('INSERT INTO participation_import_rows(batch_id,source_row,register_no,student_name,event_name,student_id,error,match_method) VALUES(?,?,?,?,?,?,?,?)',
                         [(batch_id,*item) for item in flat])
        conn.commit()
    except DB_ERROR:
        conn.rollback(); flash('The sheet could not be staged for review. No records were imported.','error'); return redirect(url_for('participation_import'))
    return redirect(url_for('participation_import_review',batch_id=batch_id))

@app.post('/attendance/import/history/clear')
@login_required
def participation_import_history_clear():
    conn=db()
    batches=conn.execute('SELECT COUNT(*) FROM participation_import_batches').fetchone()[0]
    conn.execute('DELETE FROM participation_import_rows')
    conn.execute('DELETE FROM participation_import_batches')
    conn.commit()
    flash(f'Cleared {batches} import history record(s). Saved participants and attendance were not changed.','success')
    return redirect(url_for('participation_import'))

@app.get('/attendance/import/<batch_id>')
@login_required
def participation_import_review(batch_id):
    preview=participation_import_preview(batch_id)
    if not preview:
        flash('That import preview expired or was already completed. Upload the sheet again to start a new preview.','error')
        return redirect(url_for('participation_import'))
    return render_template('participation_import.html',preview=preview,recent=[])

@app.post('/attendance/import/<batch_id>/cancel')
@login_required
def participation_import_cancel(batch_id):
    conn=db()
    conn.execute('DELETE FROM participation_import_rows WHERE batch_id=? AND EXISTS (SELECT 1 FROM participation_import_batches WHERE id=? AND status="Preview")',(batch_id,batch_id))
    conn.execute('UPDATE participation_import_batches SET status="Cancelled" WHERE id=? AND status="Preview"',(batch_id,))
    conn.commit()
    flash('Import preview cancelled. Its staged student details were removed.','success')
    return redirect(url_for('participation_import'))

@app.post('/attendance/import/<batch_id>/confirm')
@login_required
def participation_import_confirm(batch_id):
    preview=participation_import_preview(batch_id)
    if not preview:
        flash('That import preview expired or was already completed.','error'); return redirect(url_for('participation_import'))
    if preview['ambiguous_count']:
        flash('Some activities match multiple existing events. Resolve duplicate event names before importing.','error')
        return redirect(url_for('participation_import_review',batch_id=batch_id))
    if preview['unmatched_count'] and request.form.get('skip_unmatched')!='1':
        flash('Review the unmatched students, or select the option to skip them explicitly.','error')
        return redirect(url_for('participation_import_review',batch_id=batch_id))
    dates={}
    for index,event in enumerate(preview['events']):
        if event['to_import'] and not event['event']:
            event_date=request.form.get(event['date_field'],'').strip()
            try: date.fromisoformat(event_date)
            except ValueError:
                flash('Choose an event date for each new event before importing. Sheet submission timestamps are not used as event dates.','error')
                return redirect(url_for('participation_import_review',batch_id=batch_id))
            dates[event['name']]=event_date
    conn=db()
    try:
        conn.commit()
        conn.execute('BEGIN IMMEDIATE')
        rows=conn.execute('SELECT * FROM participation_import_rows WHERE batch_id=? ORDER BY id',(batch_id,)).fetchall()
        active_events=conn.execute("SELECT * FROM events WHERE status<>'Cancelled'").fetchall()
        event_ids={}
        for event in preview['events']:
            if not event['to_import']: continue
            key=normalize_import_key(canonical_event_name(event['name']))
            matches=[candidate for candidate in active_events if normalize_import_key(canonical_event_name(candidate['name']))==key]
            if len(matches)>1: raise ValueError('Multiple matching events')
            existing=matches[0] if matches else None
            if existing:
                event_ids[event['name']]=existing['id']
                if existing['name']!=canonical_event_name(event['name']) and canonical_event_name(event['name'])=='Ethnic Day':
                    conn.execute("UPDATE events SET name='Ethnic Day' WHERE id=?",(existing['id'],))
            else:
                cur=conn.execute('INSERT INTO events(name,description,event_date,status) VALUES(?,?,?,?)',
                                 (canonical_event_name(event['name']),'Imported from YRC participation sheet',dates[event['name']],'Completed'))
                event_ids[event['name']]=cur.lastrowid
        skipped=0
        current_roster_by_reg={}; current_roster_by_name={}; current_roster_name_by_id={}
        for student_row in conn.execute('SELECT id,register_no,name FROM students').fetchall():
            current_roster_by_reg.setdefault(normalize_register_no(student_row['register_no']),[]).append(student_row['id'])
            current_roster_by_name.setdefault(normalize_student_name(student_row['name']),[]).append(student_row['id'])
            current_roster_name_by_id[student_row['id']]=student_row['name']
        event_id_values=list(set(event_ids.values()))
        existing_students_by_event={}
        existing_names_by_event={}
        if event_id_values:
            placeholders=','.join('?' for _ in event_id_values)
            existing_rows=conn.execute(f'''SELECT DISTINCT p.event_id,s.id,s.name
                FROM event_participants p JOIN students s ON s.id=p.student_id
                WHERE p.event_id IN ({placeholders})
                UNION
                SELECT DISTINCT a.event_id,s.id,s.name
                FROM attendance a JOIN students s ON s.id=a.student_id
                WHERE a.event_id IN ({placeholders})''',event_id_values+event_id_values).fetchall()
            for item in existing_rows:
                existing_students_by_event.setdefault(item['event_id'],set()).add(item['id'])
                name_key=normalize_student_name(item['name'])
                if name_key:
                    existing_names_by_event.setdefault(item['event_id'],set()).add(name_key)
        participants_to_add=[]; attendance_to_add=[]; queued_pairs=set(); queued_names=set()
        duplicate_skipped=0
        for row in rows:
            event_id=event_ids.get(row['event_name'])
            if not event_id:
                skipped+=1; continue
            student_id,match_method,error=resolve_import_student(row['register_no'],row['student_name'],current_roster_by_reg,current_roster_by_name)
            if not student_id:
                skipped+=1; continue
            pair=(event_id,student_id)
            name_key=normalize_student_name(current_roster_name_by_id.get(student_id,row['student_name']))
            event_name_key=(event_id,name_key)
            if (student_id in existing_students_by_event.get(event_id,set()) or
                    (name_key and name_key in existing_names_by_event.get(event_id,set())) or
                    pair in queued_pairs or (name_key and event_name_key in queued_names)):
                duplicate_skipped+=1
                skipped+=1; continue
            queued_pairs.add(pair)
            if name_key:
                queued_names.add(event_name_key)
            participants_to_add.append((event_id,student_id,session['admin_id'],'Selected'))
            attendance_to_add.append((event_id,student_id,'Present',session['admin_id']))
        if participants_to_add:
            conn.executemany('INSERT INTO event_participants(event_id,student_id,selected_by,selection_status) VALUES(?,?,?,?) ON CONFLICT(event_id,student_id) DO UPDATE SET selection_status=excluded.selection_status,selected_by=excluded.selected_by',participants_to_add)
            marked_at_expression='CURRENT_TIMESTAMP::text' if USING_POSTGRES else 'CURRENT_TIMESTAMP'
            conn.executemany(f'INSERT INTO attendance(event_id,student_id,status,marked_by) VALUES(?,?,?,?) ON CONFLICT(event_id,student_id) DO UPDATE SET status=excluded.status,marked_at={marked_at_expression},marked_by=excluded.marked_by',attendance_to_add)
        imported=len(attendance_to_add)
        conn.execute('UPDATE participation_import_batches SET status="Completed",imported_count=?,skipped_count=?,completed_at=CURRENT_TIMESTAMP WHERE id=?',
                     (imported,skipped,batch_id))
        conn.execute('DELETE FROM participation_import_rows WHERE batch_id=?',(batch_id,))
        audit('Participation sheet imported','participation_import',0,'',
              f'{imported} records; {duplicate_skipped} duplicate name(s) rejected; batch {batch_id}')
        conn.commit()
    except DB_IMPORT_ERROR:
        conn.rollback(); flash('The import could not be completed. No partial records were saved. Review the preview and try again.','error')
        return redirect(url_for('participation_import_review',batch_id=batch_id))
    flash(f'Import complete: {imported} participation record(s) added; {duplicate_skipped} duplicate name(s) rejected; {skipped-duplicate_skipped} unmatched record(s) skipped.','success')
    return redirect(url_for('attendance_page'))

@app.get('/selection')
@login_required
def fair_selection():
    conn=db()
    dept=g.admin_department_id if g.admin['role']=='department_admin' else None
    eligible_events=conn.execute("SELECT e.*,(SELECT COUNT(*) FROM event_participants p JOIN students s ON s.id=p.student_id WHERE p.event_id=e.id AND p.selection_status='Selected' AND (? IS NULL OR s.department_id=?)) selected FROM events e WHERE e.status IN ('Upcoming','Open') AND (? IS NULL OR EXISTS(SELECT 1 FROM event_departments x WHERE x.event_id=e.id AND x.department_id=?)) ORDER BY e.event_date,e.name",(dept,dept,dept,dept)).fetchall()
    students=conn.execute('''SELECT s.id,s.name,s.register_no,s.department,s.class_name,s.section,
        COUNT(DISTINCT CASE WHEN a.status='Present' THEN a.event_id END) events_attended,
        MAX(CASE WHEN a.status='Present' THEN e.event_date END) last_event
        FROM students s LEFT JOIN attendance a ON a.student_id=s.id
        LEFT JOIN events e ON e.id=a.event_id
        WHERE s.yrc_status='Active' AND (? IS NULL OR s.department_id=?)
        GROUP BY s.id
        ORDER BY events_attended ASC,
        CASE WHEN MAX(CASE WHEN a.status='Present' THEN e.event_date END) IS NULL THEN 0 ELSE 1 END ASC,
        last_event ASC,s.name ASC
        LIMIT 50''',(dept,dept)).fetchall()
    return render_template('fair_selection.html',events=eligible_events,students=students)

@app.get('/events/<int:eid>')
@login_required
def event_detail(eid):
    event=db().execute('SELECT * FROM events WHERE id=?',(eid,)).fetchone()
    if not event:return redirect(url_for('events'))
    dept_sql=f"s.department_id IN ({','.join('?' for _ in g.admin_department_ids)})" if g.admin['role']=='department_admin' else '1=1'
    dept_args=tuple(g.admin_department_ids) if g.admin['role']=='department_admin' else ()
    participants=db().execute('''SELECT s.*,p.selection_status,a.status attendance,a.marked_at,
        (SELECT COUNT(*) FROM attendance old WHERE old.student_id=s.id AND old.status="Present") past_events
        FROM event_participants p JOIN students s ON s.id=p.student_id
        LEFT JOIN attendance a ON a.event_id=p.event_id AND a.student_id=p.student_id
        WHERE p.event_id=? AND '''+dept_sql+''' ORDER BY s.name''',(eid,*dept_args)).fetchall()
    available=db().execute("SELECT s.id,s.name,s.register_no,s.department,s.class_name,s.section FROM students s WHERE s.yrc_status='Active' AND "+dept_sql+" AND s.id NOT IN (SELECT student_id FROM event_participants WHERE event_id=? AND selection_status='Selected') ORDER BY s.name",(*dept_args,eid)).fetchall()
    return render_template('event_detail.html',event=event,participants=participants,available=available)

@app.post('/events/<int:eid>/semester')
@login_required
def event_semester_update(eid):
    semester=request.form.get('semester','')
    if semester not in ('','1','2','3','4'):
        flash('Choose a valid semester.','error')
        return redirect(url_for('event_detail',eid=eid))
    conn=db()
    event=conn.execute('SELECT name,semester FROM events WHERE id=?',(eid,)).fetchone()
    if not event:
        flash('Event not found.','error')
        return redirect(url_for('events'))
    new_semester=int(semester) if semester else None
    conn.execute('UPDATE events SET semester=? WHERE id=?',(new_semester,eid))
    audit('Event semester changed','event',eid,event['semester'] or 'Unassigned',new_semester or 'Unassigned')
    conn.commit()
    flash('Event semester updated. Attendance and credit history were preserved.','success')
    return redirect(url_for('event_detail',eid=eid))

@app.post('/events/<int:eid>/eligibility')
@login_required
def event_eligibility_update(eid):
    value=1 if request.form.get('counts_for_eligibility')=='1' else 0
    conn=db(); event=conn.execute('SELECT name,counts_for_eligibility FROM events WHERE id=?',(eid,)).fetchone()
    if not event:
        flash('Event not found.','error'); return redirect(url_for('events'))
    conn.execute('UPDATE events SET counts_for_eligibility=? WHERE id=?',(value,eid))
    audit('Event eligibility participation setting changed','event',eid,event['counts_for_eligibility'],value)
    conn.commit(); flash('Activity eligibility setting saved.','success')
    return redirect(url_for('event_detail',eid=eid))

@app.post('/events/<int:eid>/delete')
@login_required
def event_delete(eid):
    conn=db(); event=conn.execute('SELECT name FROM events WHERE id=?',(eid,)).fetchone()
    if not event:
        flash('Event was already deleted.','error'); return redirect(url_for('events'))
    conn.execute("UPDATE events SET status='Cancelled' WHERE id=?",(eid,))
    audit('Event cancelled','event',eid,event['name'],'Cancelled')
    conn.commit(); flash('Event cancelled. Its participation and verification history was preserved.','success')
    return redirect(url_for('events'))

@app.post('/events/<int:eid>/permanent-delete')
@login_required
def event_permanent_delete(eid):
    conn=db(); event=conn.execute('SELECT id,name,status FROM events WHERE id=?',(eid,)).fetchone()
    if not event:
        flash('Event was not found.','error'); return redirect(url_for('events'))
    if event['status']!='Cancelled':
        flash('Only cancelled events can be permanently deleted. Cancel the event first.','error')
        return redirect(url_for('events'))
    audit('Cancelled event permanently deleted','event',eid,event['name'],'')
    try:
        conn.execute('DELETE FROM selection_history WHERE event_id=?',(eid,))
        conn.execute('DELETE FROM events WHERE id=?',(eid,))
        conn.commit(); flash('Cancelled event and its linked participation and attendance records were permanently deleted.','success')
    except DB_INTEGRITY_ERROR:
        conn.rollback(); flash('Event could not be deleted because linked records still reference it.','error')
    return redirect(url_for('events'))

@app.post('/events/<int:eid>/recommend')
@login_required
def recommend(eid):
    conn=db(); event=conn.execute('SELECT * FROM events WHERE id=?',(eid,)).fetchone()
    if not event: return redirect(url_for('events'))
    dscope=f"s.department_id IN ({','.join('?' for _ in g.admin_department_ids)})" if g.admin['role']=='department_admin' else '1=1'
    dargs=tuple(g.admin_department_ids) if g.admin['role']=='department_admin' else ()
    already=conn.execute("SELECT COUNT(*) FROM event_participants WHERE event_id=? AND selection_status='Selected'",(eid,)).fetchone()[0]
    capacity=event['max_participants'] if event['max_participants'] is not None else 100000
    limit=max(0,capacity-already)
    candidates=conn.execute('''SELECT id,attended,offers FROM (
      SELECT s.id,COUNT(DISTINCT CASE WHEN a.status='Present' THEN a.event_id END) attended,
        (SELECT COUNT(*) FROM selection_history h WHERE h.student_id=s.id AND h.decision='Selected' AND h.event_id<>?) offers
      FROM students s LEFT JOIN attendance a ON a.student_id=s.id
      WHERE s.yrc_status='Active' AND '''+dscope+''' AND s.id NOT IN (SELECT student_id FROM event_participants WHERE event_id=? AND selection_status='Selected')
      GROUP BY s.id
    ) AS candidate_scores ORDER BY (attended*100+offers), RANDOM() LIMIT ?''',(eid,*dargs,eid,limit)).fetchall()
    for rank,c in enumerate(candidates,1):
        score=c['attended']*100+c['offers']
        conn.execute('INSERT INTO event_participants(event_id,student_id,selected_by,selection_status) VALUES(?,?,? ,?) ON CONFLICT(event_id,student_id) DO UPDATE SET selection_status="Selected",selected_at=CURRENT_TIMESTAMP,selected_by=excluded.selected_by',(eid,c['id'],session['admin_id'],'Selected'))
        conn.execute('INSERT INTO selection_history(event_id,student_id,decision,attended_count,seat_offers,priority_score,tie_break,decided_by) VALUES(?,?,?,?,?,?,?,?)',(eid,c['id'],'Selected',c['attended'],c['offers'],score,'random tie break after priority score',session['admin_id']))
    audit('Fair recommendations generated','event',eid,'',len(candidates)); conn.commit(); flash(f'{len(candidates)} recommendations saved within the remaining capacity. Review the list before treating it as final.','success'); return redirect(url_for('event_detail',eid=eid))

@app.post('/events/<int:eid>/select')
@login_required
def select_student(eid):
    sid=int(request.form['student_id']); conn=db()
    if g.admin['role']=='department_admin' and not student_belongs_to_admin_department(sid): return 'Volunteer not found.',404
    event=conn.execute('SELECT max_participants FROM events WHERE id=?',(eid,)).fetchone()
    dscope=f"s.department_id IN ({','.join('?' for _ in g.admin_department_ids)})" if g.admin['role']=='department_admin' else '1=1'
    dargs=tuple(g.admin_department_ids) if g.admin['role']=='department_admin' else ()
    already=conn.execute("SELECT COUNT(*) FROM event_participants WHERE event_id=? AND selection_status='Selected'",(eid,)).fetchone()[0]
    if not event or (event['max_participants'] is not None and already>=event['max_participants']):
        flash('No seats remain for this event.','error'); return redirect(url_for('event_detail',eid=eid))
    student=conn.execute("SELECT yrc_status,name FROM students WHERE id=? AND department_id IN ("+','.join('?' for _ in g.admin_department_ids)+")",(sid,*g.admin_department_ids)).fetchone() if g.admin['role']=='department_admin' else conn.execute("SELECT yrc_status,name FROM students WHERE id=?",(sid,)).fetchone()
    if student and student['yrc_status']=='Active':
        recent=conn.execute("SELECT COUNT(*) FROM (SELECT e.id FROM events e JOIN attendance a ON a.event_id=e.id WHERE a.student_id=? AND a.status='Present' ORDER BY e.event_date DESC LIMIT 5)",(sid,)).fetchone()[0]
        conn.execute('INSERT INTO event_participants(event_id,student_id,selected_by) VALUES(?,?,?) ON CONFLICT(event_id,student_id) DO UPDATE SET selection_status="Selected",selected_at=CURRENT_TIMESTAMP,selected_by=excluded.selected_by',(eid,sid,session['admin_id'])); audit('Manual participant selection','event',eid,'',sid); conn.commit()
        if recent >= 4: flash(f'Participation note: {student["name"]} attended {recent} of the last 5 events.', 'error')
    return redirect(url_for('event_detail',eid=eid))

@app.post('/events/<int:eid>/participants/select-multiple')
@login_required
def select_multiple_students(eid):
    conn=db()
    event=conn.execute('SELECT id,max_participants,status FROM events WHERE id=?',(eid,)).fetchone()
    if not event:
        flash('Event not found.','error'); return redirect(url_for('events'))
    if event['status']=='Cancelled':
        flash('Participants cannot be added to a cancelled event.','error')
        return redirect(url_for('event_detail',eid=eid))
    try:
        requested=list(dict.fromkeys(int(value) for value in request.form.getlist('student_ids')))
    except (TypeError,ValueError):
        flash('Choose valid students. No participants were added.','error')
        return redirect(url_for('event_detail',eid=eid))
    if not requested:
        flash('Select at least one student.','error')
        return redirect(url_for('event_detail',eid=eid))
    placeholders=','.join('?' for _ in requested)
    if g.admin['role']=='department_admin':
        eligible=conn.execute(f"SELECT id FROM students WHERE yrc_status='Active' AND department_id IN ({','.join('?' for _ in g.admin_department_ids)}) AND id IN ({placeholders})",[*g.admin_department_ids,*requested]).fetchall()
    else:
        eligible=conn.execute(f"SELECT id FROM students WHERE yrc_status='Active' AND id IN ({placeholders})",requested).fetchall()
    if len(eligible)!=len(requested):
        flash('One or more selected students are no longer active. Reload the page and review the list; no one was added.','error')
        return redirect(url_for('event_detail',eid=eid))
    dscope=f"s.department_id IN ({','.join('?' for _ in g.admin_department_ids)})" if g.admin['role']=='department_admin' else '1=1'
    dargs=tuple(g.admin_department_ids) if g.admin['role']=='department_admin' else ()
    already=conn.execute("SELECT COUNT(*) FROM event_participants WHERE event_id=? AND selection_status='Selected'",(eid,)).fetchone()[0]
    remaining=(event['max_participants']-already) if event['max_participants'] is not None else None
    if remaining is not None and len(requested)>remaining:
        flash(f'Only {max(remaining,0)} seat(s) remain. No students were added; select fewer candidates.','error')
        return redirect(url_for('event_detail',eid=eid))
    conn.executemany('''INSERT INTO event_participants(event_id,student_id,selected_by,selection_status)
        VALUES(?,?,?,'Selected') ON CONFLICT(event_id,student_id) DO UPDATE SET
        selection_status='Selected',selected_at=CURRENT_TIMESTAMP,selected_by=excluded.selected_by''',
        [(eid,row['id'],session['admin_id']) for row in eligible])
    audit('Manual multi-select participants','event',eid,'',len(eligible))
    conn.commit()
    flash(f'Added {len(eligible)} participant(s). Attendance was not changed.','success')
    return redirect(url_for('event_detail',eid=eid))

@app.post('/events/<int:eid>/participants/import')
@login_required
def import_event_participants(eid):
    conn=db()
    event=conn.execute('SELECT id,name,max_participants,status FROM events WHERE id=?',(eid,)).fetchone()
    if not event:
        flash('Event not found.','error'); return redirect(url_for('events'))
    if event['status']=='Cancelled':
        flash('Participants cannot be imported into a cancelled event.','error')
        return redirect(url_for('event_detail',eid=eid))
    upload=request.files.get('file')
    if not upload or not upload.filename or not upload.filename.lower().endswith('.csv'):
        flash('Choose a CSV export of the participant sheet. No participants were added.','error')
        return redirect(url_for('event_detail',eid=eid))
    raw=upload.read()
    try: text=raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        try: text=raw.decode('cp1252')
        except UnicodeDecodeError:
            flash('The CSV encoding could not be read. Save the sheet as CSV and try again.','error')
            return redirect(url_for('event_detail',eid=eid))
    try: dialect=csv.Sniffer().sniff(text[:8192],delimiters=',;\t')
    except csv.Error: dialect=csv.excel
    reader=csv.DictReader(io.StringIO(text),dialect=dialect)
    headers=reader.fieldnames or []
    normalized={normalize_import_key(header):header for header in headers}
    name_col=next((header for key,header in normalized.items()
                   if key=='name' or ('name' in key.split() and
                   bool(set(key.split()) & {'full','student','participant','your'}))),None)
    register_col=next((header for key,header in normalized.items()
                       if (('register' in key or 'registration' in key) and ('number' in key or 'no' in key)) or
                       key in ('usn','usn no','usn number')),None)
    event_col=next((header for key,header in normalized.items()
                    if key in ('event','event name','name of event','activity name') or
                    ('event' in key.split() and 'name' in key.split())),None)
    if not event_col:
        found=', '.join(headers[:8]) or 'no headers found'
        flash(f'This event import needs an Event Name column so every row can be checked against "{event["name"]}". Headers found: {found}. No participants were added.','error')
        return redirect(url_for('event_detail',eid=eid))
    if not name_col and not register_col:
        found=', '.join(headers[:8]) or 'no headers found'
        flash(f'Could not identify a Name or Register Number column. Headers found: {found}. No participants were added.','error')
        return redirect(url_for('event_detail',eid=eid))

    rows=list(reader)
    expected_event=' '.join(str(event['name']).split()).casefold()
    for row_number,row in enumerate(rows,start=2):
        if not any(str(value or '').strip() for value in row.values()): continue
        sheet_event=' '.join(str(row.get(event_col) or '').split()).casefold()
        if sheet_event!=expected_event:
            supplied='(blank)' if not sheet_event else str(row.get(event_col)).strip()
            flash(f'No participants were added. Row {row_number} names event "{supplied}", but this page is for "{event["name"]}". Every row must use the same event name (case and extra spaces are ignored).','error')
            return redirect(url_for('event_detail',eid=eid))

    roster=conn.execute("SELECT id,register_no,name FROM students WHERE yrc_status='Active' AND department_id IN ("+','.join('?' for _ in g.admin_department_ids)+")",tuple(g.admin_department_ids)).fetchall() if g.admin['role']=='department_admin' else conn.execute("SELECT id,register_no,name FROM students WHERE yrc_status='Active'").fetchall()
    by_reg={}; by_name={}
    for student in roster:
        by_reg.setdefault(normalize_register_no(student['register_no']),[]).append(student['id'])
        by_name.setdefault(normalize_student_name(student['name']),[]).append(student['id'])
    selected={row['student_id'] for row in conn.execute(
        "SELECT student_id FROM event_participants WHERE event_id=? AND selection_status='Selected'",(eid,)).fetchall()}
    selected_count=len(selected)
    capacity=event['max_participants'] if event['max_participants'] is not None else 1000000000
    seats=max(0,capacity-selected_count)
    queued=set(); added=already=unmatched=over_capacity=name_matches=duplicates=0
    for row in rows:
        if not any(str(value or '').strip() for value in row.values()): continue
        register_no=' '.join(str(row.get(register_col) or '').split()) if register_col else ''
        student_name=' '.join(str(row.get(name_col) or '').split()) if name_col else ''
        if not register_no and not student_name:
            unmatched+=1; continue
        student_id,match_method,_error=resolve_import_student(register_no,student_name,by_reg,by_name)
        if not student_id:
            unmatched+=1; continue
        if student_id in selected:
            already+=1; continue
        if student_id in queued:
            duplicates+=1; continue
        if added>=seats:
            over_capacity+=1; continue
        queued.add(student_id)
        if match_method=='name': name_matches+=1
        added+=1
    if not added:
        flash(f'No new participants added. Already selected: {already}; unmatched or ambiguous: {unmatched}; duplicate sheet rows: {duplicates}; over capacity: {over_capacity}.','error')
        return redirect(url_for('event_detail',eid=eid))
    try:
        conn.executemany('''INSERT INTO event_participants(event_id,student_id,selected_by,selection_status)
            VALUES(?,?,?,'Selected') ON CONFLICT(event_id,student_id) DO UPDATE SET
            selection_status='Selected',selected_at=CURRENT_TIMESTAMP,selected_by=excluded.selected_by''',
            [(eid,student_id,session['admin_id']) for student_id in queued])
        audit('Participants imported from CSV','event',eid,event['name'],
              f'{added} added; {already} already selected; {unmatched} unmatched; {duplicates} duplicate rows')
        conn.commit()
    except DB_ERROR:
        conn.rollback()
        flash('The participant import failed. No participants were added.','error')
        return redirect(url_for('event_detail',eid=eid))
    flash(f'Added {added} participants to {event["name"]}. Matched by name: {name_matches}; already selected: {already}; unmatched or ambiguous: {unmatched}; duplicate sheet rows: {duplicates}; over capacity: {over_capacity}. Attendance was not changed.','success')
    return redirect(url_for('event_detail',eid=eid))

@app.post('/events/<int:eid>/participants/<int:sid>/remove')
@login_required
def remove_participant(eid,sid):
    conn=db(); conn.execute('UPDATE event_participants SET selection_status="Removed" WHERE event_id=? AND student_id=?',(eid,sid)); audit('Participant deselected','event',eid,'selected',sid); conn.commit(); return redirect(url_for('event_detail',eid=eid))

@app.post('/events/<int:eid>/attendance/<int:sid>')
@login_required
def mark_attendance(eid,sid):
    conn=db(); status=request.form['status']
    if status not in ('Present','Absent','Excused','Pending Verification','Undo'): return redirect(url_for('event_detail',eid=eid))
    enrolled=conn.execute("SELECT selection_status FROM event_participants WHERE event_id=? AND student_id=?",(eid,sid)).fetchone()
    if status!='Undo' and (not enrolled or enrolled['selection_status']!='Selected'):
        flash('Only selected volunteers may receive attendance records. Flag exceptions for manual review.','error'); return redirect(url_for('event_detail',eid=eid))
    if status=='Undo':
        conn.execute('DELETE FROM attendance WHERE event_id=? AND student_id=?',(eid,sid))
    else:
        conn.execute('INSERT INTO attendance(event_id,student_id,status,marked_by) VALUES(?,?,?,?) ON CONFLICT(event_id,student_id) DO UPDATE SET status=excluded.status,marked_at=CURRENT_TIMESTAMP,marked_by=excluded.marked_by',(eid,sid,status,session['admin_id']))
    audit('Attendance '+status,'attendance',eid,'',sid); conn.commit(); return redirect(url_for('event_detail',eid=eid))

@app.post('/events/<int:eid>/attendance/mark-all-present')
@login_required
def mark_all_present(eid):
    conn=db()
    event=conn.execute('SELECT id,name,status FROM events WHERE id=?',(eid,)).fetchone()
    if not event:
        flash('Event not found.','error')
        return redirect(url_for('events'))
    if event['status']=='Cancelled':
        flash('Attendance cannot be marked for a cancelled event.','error')
        return redirect(url_for('event_detail',eid=eid))
    students=conn.execute("SELECT p.student_id FROM event_participants p JOIN students s ON s.id=p.student_id WHERE p.event_id=? AND p.selection_status='Selected' AND s.department_id IN ("+','.join('?' for _ in g.admin_department_ids)+")",(eid,*g.admin_department_ids)).fetchall() if g.admin['role']=='department_admin' else conn.execute("SELECT student_id FROM event_participants WHERE event_id=? AND selection_status='Selected'",(eid,)).fetchall()
    if not students:
        flash('There are no selected participants to mark present.','error')
        return redirect(url_for('event_detail',eid=eid))
    conn.executemany('''INSERT INTO attendance(event_id,student_id,status,marked_by) VALUES(?,?,'Present',?)
        ON CONFLICT(event_id,student_id) DO UPDATE SET status='Present',marked_at=CURRENT_TIMESTAMP,marked_by=excluded.marked_by''',
        [(eid,row['student_id'],session['admin_id']) for row in students])
    audit('Attendance marked Present for all selected participants','attendance',eid,'',len(students))
    conn.commit()
    flash(f'Marked {len(students)} selected participant(s) present for {event["name"]}.','success')
    return redirect(url_for('event_detail',eid=eid))

@app.get('/reports')
@login_required
def reports():
    semester_filter=request.args.get('semester','All')
    cohort_filter=request.args.get('cohort','')
    cohort_clause=' AND s.cohort=?' if cohort_filter else ''
    cohort_args=[cohort_filter] if cohort_filter else []
    if semester_filter not in ('1','2','3','4','Unassigned'):
        semester_filter='All'
    semester_where=''
    semester_args=[]
    if semester_filter in ('1','2','3','4'):
        semester_where=' AND e.semester=?'
        semester_args.append(int(semester_filter))
    elif semester_filter=='Unassigned':
        semester_where=' AND e.semester IS NULL'
    conn=db(); rows=conn.execute('''SELECT s.register_no,s.name,s.department,s.class_name,s.section,s.email,s.phone,
        (SELECT COUNT(DISTINCT a.event_id) FROM attendance a JOIN events e ON e.id=a.event_id
         WHERE a.student_id=s.id AND a.status='Present' AND e.status<>'Cancelled' '''+semester_where+''') events,
        (SELECT MAX(e.event_date) FROM attendance a JOIN events e ON e.id=a.event_id
         WHERE a.student_id=s.id AND a.status='Present' AND e.status<>'Cancelled' '''+semester_where+''') last_event
        FROM students s WHERE s.yrc_status='Active' '''+cohort_clause+''' ORDER BY events,s.name''',semester_args*2+cohort_args).fetchall()
    attendance_rows=conn.execute('''SELECT s.id,s.register_no,s.name,s.department,s.class_name,s.section,
        e.name event_name,e.event_date
        FROM students s JOIN attendance a ON a.student_id=s.id AND a.status='Present'
        JOIN events e ON e.id=a.event_id
        WHERE s.yrc_status='Active' AND e.status<>'Cancelled' '''+semester_where+cohort_clause+'''
        ORDER BY s.name,e.event_date,e.name''',semester_args+cohort_args).fetchall()
    attended_by_student={}
    for attendance_row in attendance_rows:
        sid=attendance_row['id']
        student=attended_by_student.setdefault(sid,{
            'register_no':attendance_row['register_no'],'name':attendance_row['name'],
            'department':attendance_row['department'],'class_name':attendance_row['class_name'],
            'section':attendance_row['section'],'events':0,'event_names':[]})
        student['events']+=1
        student['event_names'].append(f"{attendance_row['event_name']} ({attendance_row['event_date']})")
    attended_rows=[dict(student, event_names=', '.join(student['event_names']))
                   for student in attended_by_student.values()]
    total_attendances=sum(student['events'] for student in attended_rows)
    below_event_threshold=[r for r in rows if r['events'] < ELIGIBILITY_EVENT_THRESHOLD]
    ineligible_by_department={}
    for student in below_event_threshold:
        department=(student['department'] or 'Unassigned').strip()
        ineligible_by_department[department]=ineligible_by_department.get(department,0)+1
    department_counts=[{'name':department,'count':ineligible_by_department.get(department,0)}
                       for department in sorted({(r['department'] or 'Unassigned').strip() for r in rows})]
    evrows=conn.execute('SELECT e.name,e.event_date,e.semester,(SELECT COUNT(*) FROM event_participants p WHERE p.event_id=e.id AND p.selection_status="Selected") selected,(SELECT COUNT(*) FROM attendance a WHERE a.event_id=e.id AND a.status="Present") present,(SELECT COUNT(*) FROM attendance a WHERE a.event_id=e.id AND a.status="Absent") absent FROM events e WHERE 1=1'+semester_where+' ORDER BY e.event_date DESC',semester_args).fetchall()
    credit_rows=conn.execute('''SELECT a.id attendance_id,a.credit_points,e.semester,e.name event_name,e.event_date,
        s.id student_id,s.register_no,s.name,s.department
        FROM attendance a JOIN events e ON e.id=a.event_id JOIN students s ON s.id=a.student_id
        WHERE a.status='Present' '''+semester_where+cohort_clause+''' ORDER BY e.semester,e.event_date,s.name''',semester_args+cohort_args).fetchall()
    credit_totals={}
    for credit_row in credit_rows:
        student=credit_totals.setdefault(credit_row['student_id'],{
            'register_no':credit_row['register_no'],'name':credit_row['name'],
            'department':credit_row['department'],'events':0,'credits':0})
        student['events']+=1
        student['credits']+=credit_row['credit_points']
    return render_template('reports.html',rows=rows,events=evrows,
                           below_event_threshold=below_event_threshold,
                           event_threshold_met=[r for r in attended_rows if r['events'] >= ELIGIBILITY_EVENT_THRESHOLD],
                           total_attendances=total_attendances,
                           department_counts=department_counts,semester_filter=semester_filter,
                           cohort_filter=cohort_filter,cohorts=[r[0] for r in conn.execute("SELECT name FROM cohorts UNION SELECT DISTINCT cohort FROM students WHERE cohort_id IS NULL AND cohort<>'' ORDER BY 1")],
                           credit_rows=credit_rows,credit_totals=sorted(credit_totals.values(),key=lambda item:item['name']))

@app.post('/reports/attendance/<int:attendance_id>/credit')
@login_required
def update_attendance_credit(attendance_id):
    cohort_filter=request.form.get('cohort','')
    cohort_args={'cohort':cohort_filter} if cohort_filter else {}
    try:
        credit_points=int(request.form.get('credit_points',''))
    except (TypeError,ValueError):
        flash('Enter a whole-number credit value of zero or more.','error')
        return redirect(url_for('reports',semester=request.form.get('semester','All'),**cohort_args))
    if credit_points<0:
        flash('Credit points cannot be negative.','error')
        return redirect(url_for('reports',semester=request.form.get('semester','All'),**cohort_args))
    conn=db()
    row=conn.execute('SELECT a.credit_points,a.status,e.semester,s.cohort FROM attendance a JOIN events e ON e.id=a.event_id JOIN students s ON s.id=a.student_id WHERE a.id=?',(attendance_id,)).fetchone()
    if not row or row['status']!='Present':
        flash('Credit can only be marked for a present attendance record.','error')
        return redirect(url_for('reports',semester=request.form.get('semester','All'),**cohort_args))
    if cohort_filter and row['cohort']!=cohort_filter:
        flash('That student is not part of the selected batch.','error')
        return redirect(url_for('reports',semester=request.form.get('semester','All'),**cohort_args))
    semester_filter=request.form.get('semester','All')
    if semester_filter in ('1','2','3','4') and row['semester']!=int(semester_filter):
        flash('That attendance record is not part of the selected semester.','error')
        return redirect(url_for('reports',semester=semester_filter,**cohort_args))
    if semester_filter=='Unassigned' and row['semester'] is not None:
        flash('That attendance record is not part of the selected semester.','error')
        return redirect(url_for('reports',semester=semester_filter,**cohort_args))
    conn.execute('UPDATE attendance SET credit_points=? WHERE id=?',(credit_points,attendance_id))
    audit('Attendance credit updated','attendance',attendance_id,row['credit_points'],credit_points)
    conn.commit()
    flash('Credit points saved. Previous semester records remain unchanged.','success')
    return redirect(url_for('reports',semester=request.form.get('semester','All'),**cohort_args))

@app.route('/eligibility', methods=['GET','POST'])
@login_required
def eligibility():
    return redirect(url_for('semester_list'))

def semester_eligibility_many(semester_id, student_ids):
    """Calculate a semester's results with a fixed number of database round trips."""
    conn=db()
    ids=list(dict.fromkeys(student_ids))
    if not ids:
        return {}
    semester=conn.execute('SELECT id FROM semesters WHERE id=?',(semester_id,)).fetchone()
    rules=conn.execute("SELECT * FROM semester_rules WHERE semester_id=? AND enabled=1 AND rule_type<>'certificate' ORDER BY id",(semester_id,)).fetchall() if semester else []
    min_rule=next((r for r in rules if r['rule_type']=='minimum_activities' and r['mandatory']),None)
    minimum=int(min_rule['value']) if min_rule else 0
    placeholders=','.join('?' for _ in ids)
    activity_rows=conn.execute("SELECT a.student_id,COUNT(DISTINCT a.event_id) AS activities FROM attendance a JOIN events e ON e.id=a.event_id WHERE a.status='Present' AND e.status<>'Cancelled' AND e.counts_for_eligibility=1 AND e.semester=? AND a.student_id IN ("+placeholders+") GROUP BY a.student_id",[semester_id,*ids]).fetchall() if semester else []
    activities={r['student_id']:r['activities'] for r in activity_rows}
    results={}; history=[]
    for student_id in ids:
        count=activities.get(student_id,0)
        if not semester or not rules:
            result={'status':'INCOMPLETE_RECORDS','activities':count,'minimum':minimum,'reasons':['Eligibility criteria have not been configured.' if semester else 'Semester configuration is missing.']}
        else:
            reasons=[]
            if min_rule and count<minimum:
                remaining=minimum-count
                reasons.append(f'Complete {remaining} more eligible activit{"y" if remaining==1 else "ies"}.')
            if not reasons:
                reasons=['You have met the activity requirement.']
            status='ELIGIBLE' if count>=minimum else 'NOT_ELIGIBLE'
            result={'status':status,'activities':count,'minimum':minimum,'reasons':reasons}
        results[student_id]=result
        history.append((semester_id,student_id,result['status'],'; '.join(result['reasons'])))
    conn.executemany('INSERT INTO semester_eligibility_history(semester_id,student_id,status,reasons) VALUES(?,?,?,?) ON CONFLICT(semester_id,student_id) DO UPDATE SET status=excluded.status,reasons=excluded.reasons,calculated_at=CURRENT_TIMESTAMP',history)
    return results

def semester_eligibility(semester_id, student_id):
    """Calculate one student's result through the shared batch calculation service."""
    return semester_eligibility_many(semester_id,[student_id])[student_id]

@app.get('/semesters')
@login_required
def semester_list():
    semesters=db().execute('SELECT * FROM semesters ORDER BY id').fetchall()
    return render_template('semesters.html',semesters=semesters)

@app.route('/semesters/<int:semester_id>', methods=['GET','POST'])
@login_required
def semester_detail(semester_id):
    conn=db(); semester=conn.execute('SELECT * FROM semesters WHERE id=?',(semester_id,)).fetchone()
    if not semester: return 'Semester not found',404
    if request.method=='POST':
        action=request.form.get('action','rules')
        if semester['locked'] and action not in ('semester','metadata'):
            flash('This semester is finalized. Reopen it before changing eligibility settings.','error')
            return redirect(url_for('semester_detail',semester_id=semester_id))
        if action=='rules':
            if semester['locked']:
                flash('Reopen this semester before changing its criteria.','error'); return redirect(url_for('semester_detail',semester_id=semester_id))
            try:
                minimum=int(request.form.get('minimum_activities','0'))
                if minimum<0 or minimum>100: raise ValueError
            except ValueError:
                flash('Activity minimum must be a whole number from 0 to 100.','error'); return redirect(url_for('semester_detail',semester_id=semester_id))
            old=[dict(r) for r in conn.execute('SELECT rule_type,value,mandatory,verification_required,enabled FROM semester_rules WHERE semester_id=?',(semester_id,)).fetchall()]
            conn.execute('DELETE FROM semester_rules WHERE semester_id=?',(semester_id,))
            conn.execute("INSERT INTO semester_rules(semester_id,rule_type,value,mandatory) VALUES(?,'minimum_activities',?,1)",(semester_id,str(minimum)))
            audit('Semester eligibility rules changed','semester',semester_id,old,{'minimum_activities':minimum})
            active_ids=[student['id'] for student in conn.execute("SELECT id FROM students WHERE yrc_status='Active'").fetchall()]
            semester_eligibility_many(semester_id,active_ids)
            conn.commit(); flash('Criteria saved and eligibility recalculated.','success')
        elif action=='semester':
            locked=1-int(semester['locked'])
            conn.execute('UPDATE semesters SET locked=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',(locked,semester_id))
            audit('Semester finalized' if locked else 'Semester reopened','semester',semester_id,semester['locked'],locked)
            conn.commit(); flash('Semester finalized.' if locked else 'Semester reopened.','success')
        elif action=='metadata':
            name=request.form.get('name','').strip(); year=request.form.get('academic_year','').strip(); active=1 if request.form.get('active')=='1' else 0
            if not name or len(name)>80 or len(year)>30:
                flash('Enter a semester name and a valid academic year.','error')
            else:
                conn.execute('UPDATE semesters SET name=?,academic_year=?,active=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',(name,year,active,semester_id))
                audit('Semester settings changed','semester',semester_id,{'name':semester['name'],'academic_year':semester['academic_year'],'active':semester['active']},{'name':name,'academic_year':year,'active':active})
                conn.commit(); flash('Semester settings saved.','success')
    rules=conn.execute('SELECT * FROM semester_rules WHERE semester_id=? AND enabled=1 ORDER BY id',(semester_id,)).fetchall()
    # Certificate criteria are no longer part of the semester eligibility calculation.
    conn.execute("DELETE FROM semester_rules WHERE semester_id=? AND rule_type='certificate'",(semester_id,))
    min_rule=next((r for r in rules if r['rule_type']=='minimum_activities'),None)
    students=conn.execute("SELECT id,register_no,name,department,class_name,section FROM students WHERE yrc_status='Active' AND department_id IN ("+','.join('?' for _ in g.admin_department_ids)+") ORDER BY name",tuple(g.admin_department_ids)).fetchall() if g.admin['role']=='department_admin' else conn.execute("SELECT id,register_no,name,department,class_name,section FROM students WHERE yrc_status='Active' ORDER BY name").fetchall()
    results=semester_eligibility_many(semester_id,[student['id'] for student in students])
    rows=[{'student':student,'result':results[student['id']]} for student in students]
    conn.commit()
    counts={key:sum(1 for row in rows if row['result']['status']==key) for key in ('ELIGIBLE','NOT_ELIGIBLE','INCOMPLETE_RECORDS')}
    eligible_rows=[row for row in rows if row['result']['status']=='ELIGIBLE']
    not_eligible_rows=[row for row in rows if row['result']['status']!='ELIGIBLE']
    return render_template('semester_detail.html',semester=semester,rows=rows,eligible_rows=eligible_rows,not_eligible_rows=not_eligible_rows,minimum=min_rule['value'] if min_rule else '',counts=counts,configured=bool(min_rule))

@app.get('/semesters/<int:semester_id>/report.csv')
@login_required
def semester_report_csv(semester_id):
    section=request.args.get('section','eligible')
    if section not in ('eligible','not-eligible'): return 'Invalid report section',400
    semester=db().execute('SELECT name FROM semesters WHERE id=?',(semester_id,)).fetchone()
    if not semester: return 'Semester not found',404
    output=io.StringIO(); writer=csv.writer(output)
    writer.writerow(['Semester','Register number','Student','Department','Class','Activities completed','Activities required','Status','Remaining requirements'])
    students=db().execute("SELECT id,register_no,name,department,class_name FROM students WHERE yrc_status='Active' AND department_id IN ("+','.join('?' for _ in g.admin_department_ids)+") ORDER BY name",tuple(g.admin_department_ids)).fetchall() if g.admin['role']=='department_admin' else db().execute("SELECT id,register_no,name,department,class_name FROM students WHERE yrc_status='Active' ORDER BY name").fetchall()
    results=semester_eligibility_many(semester_id,[student['id'] for student in students])
    for student in students:
        result=results[student['id']]
        is_eligible=result['status']=='ELIGIBLE'
        if (section=='eligible') != is_eligible: continue
        writer.writerow([semester['name'],student['register_no'],student['name'],student['department'],student['class_name'],result['activities'],result['minimum'],result['status'],'; '.join(result['reasons'])])
    db().commit()
    return Response('\ufeff'+output.getvalue(),mimetype='text/csv',headers={'Content-Disposition':f'attachment; filename="semester-{semester_id}-{section}.csv"'})

@app.get('/reports/attended-students.csv')
@login_required
def attended_students_csv():
    semester_filter=request.args.get('semester','All')
    semester_where=''
    semester_args=[]
    if semester_filter in ('1','2','3','4'):
        semester_where=' AND e.semester=?'
        semester_args.append(int(semester_filter))
    elif semester_filter=='Unassigned':
        semester_where=' AND e.semester IS NULL'
    rows=db().execute('''SELECT s.register_no,s.name,s.department,s.class_name,s.section,
        s.id student_id,e.name event_name,e.event_date
        FROM students s JOIN attendance a ON a.student_id=s.id AND a.status='Present'
        JOIN events e ON e.id=a.event_id
        WHERE s.yrc_status='Active' AND e.status<>'Cancelled' '''+semester_where+'''
        ORDER BY s.name,e.event_date,e.name''',semester_args).fetchall()
    attended_by_student={}
    for row in rows:
        student=attended_by_student.setdefault(row['student_id'],{
            'register_no':row['register_no'],'name':row['name'],'department':row['department'],
            'class_name':row['class_name'],'section':row['section'],'events':[]})
        student['events'].append(row['event_name'])
    buf=io.StringIO(newline=''); writer=csv.writer(buf)
    writer.writerow(['Register No','Student Name','Department','Class','Section','Events Attended','Total Events Attended'])
    for student in attended_by_student.values():
        if len(student['events']) < 3:
            continue
        values=[student['register_no'],student['name'],student['department'],student['class_name'],
                student['section'],'; '.join(student['events']),len(student['events'])]
        writer.writerow([("'"+str(value) if str(value or '').lstrip().startswith(('=','+','-','@')) else value) for value in values])
    filename=f'yrc-student-event-attendance-semester-{semester_filter}.csv' if semester_filter in ('1','2','3','4') else 'yrc-student-event-attendance.csv'
    return Response('\ufeff'+buf.getvalue(),mimetype='text/csv',headers={'Content-Disposition':f'attachment; filename={filename}'})

@app.get('/reports/students.csv')
@login_required
def students_csv():
    rows=db().execute('''SELECT s.register_no,s.name,s.department,s.class_name,s.section,s.year,s.email,s.phone,s.yrc_status,s.cohort,s.cohort_verification,
        (SELECT COUNT(DISTINCT a.event_id) FROM attendance a WHERE a.student_id=s.id AND a.status='Present') events
        FROM students s ORDER BY s.name''').fetchall()
    buf=io.StringIO(); writer=csv.writer(buf)
    writer.writerow(['Register No','Name','Department','Class','Section','Year','Email','Phone','YRC Status','Batch','Cohort Verification','Events Attended'])
    columns=('register_no','name','department','class_name','section','year','email','phone','yrc_status','cohort','cohort_verification','events')
    writer.writerows([[row[key] for key in columns] for row in rows])
    return Response('\ufeff'+buf.getvalue(),mimetype='text/csv',headers={'Content-Disposition':'attachment; filename=yrc-students.csv'})

@app.get('/reports/priority-students.csv')
@login_required
def priority_students_csv():
    import csv, io
    semester_filter=request.args.get('semester','All')
    semester_where=''
    semester_args=[]
    if semester_filter in ('1','2','3','4'):
        semester_where=' AND e.semester=?'
        semester_args.append(int(semester_filter))
    elif semester_filter=='Unassigned':
        semester_where=' AND e.semester IS NULL'
    rows=db().execute('''SELECT s.register_no,s.name,s.department,s.class_name,s.section,s.email,s.phone
        FROM students s WHERE s.yrc_status='Active'
        AND (SELECT COUNT(DISTINCT a.event_id) FROM attendance a JOIN events e ON e.id=a.event_id
             WHERE a.student_id=s.id AND a.status='Present' AND e.status<>'Cancelled' '''+semester_where+''') < 3
        ORDER BY s.department,s.class_name,s.name''',semester_args).fetchall()
    buf=io.StringIO(newline=''); writer=csv.writer(buf)
    writer.writerow(['Register No','Name','Department','Class','Section','Email','Phone','Priority'])
    for row in rows:
        values=[row['register_no'],row['name'],row['department'],row['class_name'],row['section'],row['email'],row['phone'],'Below 3-event attendance threshold']
        writer.writerow([("'"+str(value) if str(value or '').lstrip().startswith(('=','+','-','@')) else value) for value in values])
    filename=f'yrc-below-event-threshold-semester-{semester_filter}.csv' if semester_filter in ('1','2','3','4') else 'yrc-below-event-threshold.csv'
    return Response('\ufeff'+buf.getvalue(),mimetype='text/csv',headers={'Content-Disposition':f'attachment; filename={filename}'})

@app.get('/settings')
@login_required
def settings(): return render_template('settings.html')

@app.errorhandler(404)
def not_found(_): return render_template('404.html'),404

@app.get('/favicon.ico')
def favicon(): return redirect(url_for('static', filename='yrc-logo.jpg'))

if __name__=='__main__':
    init_db()
    app.run(debug=os.getenv('FLASK_DEBUG')=='1')
