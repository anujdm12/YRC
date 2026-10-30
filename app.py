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
ELIGIBILITY_CERTIFICATE_THRESHOLD = 3

@app.context_processor
def inject_now():
    today=date.today()
    return {'now': today.strftime('%d %b %Y'), 'today_long': today.strftime('%A, %d %B %Y').upper()}

SCHEMA = '''
CREATE TABLE IF NOT EXISTS admins(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS admin_roles(admin_id INTEGER PRIMARY KEY REFERENCES admins(id) ON DELETE CASCADE, role TEXT NOT NULL DEFAULT 'super_admin', department TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS students(id INTEGER PRIMARY KEY, register_no TEXT UNIQUE NOT NULL, name TEXT NOT NULL, department TEXT NOT NULL, class_name TEXT NOT NULL DEFAULT '', section TEXT NOT NULL DEFAULT '', year INTEGER NOT NULL, email TEXT, phone TEXT, yrc_status TEXT NOT NULL DEFAULT 'Active', created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT, event_date TEXT NOT NULL, start_time TEXT, end_time TEXT, venue TEXT, max_participants INTEGER, deadline TEXT, status TEXT NOT NULL DEFAULT 'Upcoming', semester INTEGER, counts_for_eligibility INTEGER NOT NULL DEFAULT 1, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
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
'''

def db():
    if 'db' not in g:
        g.db = connect_db(DB_PATH)
    return g.db

@app.teardown_appcontext
def close_db(_):
    conn = g.pop('db', None)
    if conn: conn.close()

def init_db():
    conn = connect_db(DB_PATH)
    conn.executescript(SCHEMA)
    for semester_number in range(1, 5):
        conn.execute('INSERT OR IGNORE INTO semesters(id,name) VALUES(?,?)', (semester_number, f'Semester {semester_number}'))
    conn.execute("INSERT OR IGNORE INTO semester_rules(semester_id,rule_type,value,mandatory,verification_required) VALUES(1,'minimum_activities','2',1,0)")
    conn.execute("INSERT OR IGNORE INTO semester_rules(semester_id,rule_type,value,mandatory,verification_required) VALUES(1,'certificate','ICRC',1,1)")
    student_columns = {row[1] for row in conn.execute('PRAGMA table_info(students)')}
    if 'class_name' not in student_columns:
        conn.execute("ALTER TABLE students ADD COLUMN class_name TEXT NOT NULL DEFAULT ''")
    if 'section' not in student_columns:
        conn.execute("ALTER TABLE students ADD COLUMN section TEXT NOT NULL DEFAULT ''")
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
    bootstrap_username=os.getenv('INITIAL_ADMIN_USERNAME','').strip()
    bootstrap_password=os.getenv('INITIAL_ADMIN_PASSWORD','')
    if bool(bootstrap_username) != bool(bootstrap_password):
        conn.close()
        raise RuntimeError('Set both INITIAL_ADMIN_USERNAME and INITIAL_ADMIN_PASSWORD to bootstrap the first admin.')
    if bootstrap_username and len(bootstrap_password) < 3:
        conn.close()
        raise RuntimeError('INITIAL_ADMIN_PASSWORD must contain at least 3 characters.')
    if bootstrap_username and not conn.execute('SELECT 1 FROM admins LIMIT 1').fetchone():
        cur=conn.execute('INSERT INTO admins(username,password_hash) VALUES(?,?)',(bootstrap_username,generate_password_hash(bootstrap_password)))
        conn.execute("INSERT INTO admin_roles(admin_id,role,department) VALUES(?,'super_admin','')",(cur.lastrowid,))
    conn.commit(); conn.close()

# Initialize schema for both `python app.py` and Flask/WSGI launchers.
init_db()

@app.cli.command('create-admin')
@click.option('--username', prompt=True)
@click.option('--password', prompt=True, hide_input=True, confirmation_prompt=True)
def create_admin(username, password):
    """Create the initial administrator without a built-in credential."""
    if len(password) < 3:
        raise click.ClickException('Use a password with at least 3 characters.')
    conn=connect_db(DB_PATH)
    try:
        cur=conn.execute('INSERT INTO admins(username,password_hash) VALUES(?,?)',(username.strip(),generate_password_hash(password)))
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
    if len(password) < 3:
        raise click.ClickException('Use a password with at least 3 characters.')
    conn=connect_db(DB_PATH)
    cur=conn.execute('UPDATE admins SET password_hash=? WHERE username=?',(generate_password_hash(password),username.strip()))
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

def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get('admin_id'): return redirect(url_for('login'))
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
        user = db().execute('SELECT * FROM admins WHERE username=?', (username,)).fetchone()
        if user and check_password_hash(user['password_hash'], request.form.get('password','')):
            db().execute('INSERT INTO login_attempts(username,attempted_at,success) VALUES(?,?,1)',(username,int(time.time())))
            db().commit()
            session.clear(); session['admin_id'] = user['id']; session['username'] = user['username']; session['_csrf_token']=secrets.token_urlsafe(32); return redirect(url_for('dashboard'))
        db().execute('INSERT INTO login_attempts(username,attempted_at,success) VALUES(?,?,0)',(username,int(time.time())))
        db().commit()
        flash('Those credentials did not match an admin account.', 'error')
    return render_template('login.html')

@app.post('/logout')
def logout(): session.clear(); return redirect(url_for('login'))

@app.get('/')
@login_required
def dashboard():
    conn=db()
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
        if not f.get('register_no','').strip() or not f.get('name','').strip() or not f.get('department','').strip() or not f.get('class_name','').strip() or year not in range(1,9):
            flash('Enter the register number, name, department, programme, and a year from 1 to 8.','error'); return redirect(url_for('students'))
        if f.get('yrc_status','Active') not in ('Active','Inactive','Removed'):
            flash('Choose a valid YRC status.','error'); return redirect(url_for('students'))
        try:
            cur=conn.execute('INSERT INTO students(register_no,name,department,class_name,section,year,email,phone,yrc_status) VALUES(?,?,?,?,?,?,?,?,?)',(f['register_no'].strip(),f['name'].strip(),f['department'].strip(),f['class_name'].strip(),f['section'].strip().upper(),year,f.get('email'),f.get('phone'),f.get('yrc_status','Active')))
            audit('Student added','student',cur.lastrowid,'',f['register_no']); conn.commit(); flash('Student added.','success')
        except DB_INTEGRITY_ERROR: conn.rollback(); flash('Register number already exists.','error')
        return redirect(url_for('students'))
    q=request.args.get('q','').strip(); status=request.args.get('status',''); dept=request.args.get('department',''); section=request.args.get('section','').upper()
    where=[]; args=[]
    if q: where.append('(name LIKE ? OR register_no LIKE ?)'); args += [f'%{q}%',f'%{q}%']
    if status: where.append('yrc_status=?'); args.append(status)
    else: where.append("yrc_status<>'Removed'")
    if dept: where.append('department=?'); args.append(dept)
    if section=='UNASSIGNED': where.append("section=''")
    elif section: where.append('section=?'); args.append(section)
    sql='SELECT s.*, (SELECT COUNT(DISTINCT a.event_id) FROM attendance a WHERE a.student_id=s.id AND a.status="Present") events FROM students s'+(' WHERE '+' AND '.join(where) if where else '')+' ORDER BY s.name'
    rows=conn.execute(sql,args).fetchall(); departments=[r[0] for r in conn.execute('SELECT DISTINCT department FROM students ORDER BY department')]
    return render_template('students.html', students=rows,q=q,status=status,department=dept,departments=departments,section=section)

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
        if section and section not in tuple('ABCDEFG'): errors.append(f'Row {row_num}: section must be a letter from A through G.'); continue
        if status not in ('Active','Inactive','Removed'): errors.append(f'Row {row_num}: invalid YRC status {status!r}.'); continue
        if reg.casefold() in known: errors.append(f'Row {row_num}: duplicate register number {reg}.'); continue
        known.add(reg.casefold())
        records.append((reg,name,department,class_name,section,year,(row.get('email') or '').strip(),(row.get('phone') or '').strip(),status))
    if errors:
        preview=' '.join(errors[:5]); more=f' (+{len(errors)-5} more)' if len(errors)>5 else ''
        flash(f'No students imported. Fix {len(errors)} issue(s): {preview}{more}','error'); return redirect(url_for('students'))
    if not records:
        flash('The CSV has no student rows. No students were imported.','error'); return redirect(url_for('students'))
    conn=db()
    try:
        conn.executemany('INSERT INTO students(register_no,name,department,class_name,section,year,email,phone,yrc_status) VALUES(?,?,?,?,?,?,?,?,?)',records)
        audit('Student CSV imported','student',0,'',f'{len(records)} added'); conn.commit()
    except DB_INTEGRITY_ERROR:
        conn.rollback(); flash('The import was rejected by a database constraint. No students were imported; check register numbers and required fields.','error'); return redirect(url_for('students'))
    flash(f'Imported all {len(records)} students successfully.','success'); return redirect(url_for('students'))

@app.get('/students/<int:sid>')
@login_required
def student_detail(sid):
    s=db().execute('SELECT s.*,(SELECT COUNT(DISTINCT a.event_id) FROM attendance a WHERE a.student_id=s.id AND a.status="Present") events,(SELECT MAX(e.event_date) FROM attendance a JOIN events e ON e.id=a.event_id WHERE a.student_id=s.id AND a.status="Present") last_event,(SELECT COUNT(*) FROM events e WHERE e.status="Completed") total_events FROM students s WHERE s.id=?',(sid,)).fetchone()
    if not s: return redirect(url_for('students'))
    history=db().execute('SELECT e.name,e.event_date,e.semester,a.status,a.credit_points FROM attendance a JOIN events e ON e.id=a.event_id WHERE a.student_id=? ORDER BY e.event_date DESC',(sid,)).fetchall()
    semester_progress=[{'semester':db().execute('SELECT * FROM semesters WHERE id=?',(n,)).fetchone(), 'result':semester_eligibility(n,sid)} for n in range(1,5)]
    db().commit()
    return render_template('student_detail.html',student=s,history=history,semester_progress=semester_progress)

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
    if year not in range(1,9) or any(not f.get(k,'').strip() for k in ('register_no','name','department','class_name')):
        flash('Enter all required student details and choose a year from 1 to 8.','error'); return redirect(url_for('student_detail',sid=sid))
    try:
        conn.execute('UPDATE students SET register_no=?,name=?,department=?,class_name=?,section=?,year=?,email=?,phone=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',(f['register_no'].strip(),f['name'].strip(),f['department'].strip(),f['class_name'].strip(),f['section'].strip().upper(),year,f.get('email'),f.get('phone'),sid))
        audit('Student edited','student',sid,'','profile updated'); conn.commit(); flash('Student profile updated.','success')
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
        audit('Event created','event',cur.lastrowid,'',f['name']); conn.commit(); flash('Event created.','success'); return redirect(url_for('events'))
    q=request.args.get('q','').strip(); status=request.args.get('status','All')
    semester=request.args.get('semester','All')
    where=[]; args=[]
    if q: where.append('(e.name LIKE ? OR e.venue LIKE ?)'); args.extend((f'%{q}%',f'%{q}%'))
    if status in ('Draft','Upcoming','Open','Closed','Completed','Cancelled'): where.append('e.status=?'); args.append(status)
    if semester in ('1','2','3','4'): where.append('e.semester=?'); args.append(int(semester))
    elif semester=='Unassigned': where.append('e.semester IS NULL')
    else: semester='All'
    rows=conn.execute('SELECT e.*,(SELECT COUNT(*) FROM event_participants p WHERE p.event_id=e.id AND p.selection_status="Selected") selected,(SELECT COUNT(*) FROM attendance a WHERE a.event_id=e.id AND a.status="Present") present FROM events e'+(' WHERE '+' AND '.join(where) if where else '')+' ORDER BY e.event_date DESC',args).fetchall()
    summary=conn.execute("SELECT COUNT(*) total, SUM(CASE WHEN status IN ('Upcoming','Open') THEN 1 ELSE 0 END) active, SUM(CASE WHEN status='Cancelled' THEN 1 ELSE 0 END) cancelled FROM events").fetchone()
    return render_template('events.html',events=rows,q=q,status=status,semester=semester,summary=summary)

@app.get('/attendance')
@login_required
def attendance_page():
    rows=db().execute('''SELECT e.*,
        (SELECT COUNT(*) FROM event_participants p WHERE p.event_id=e.id AND p.selection_status='Selected') selected,
        (SELECT COUNT(*) FROM attendance a JOIN event_participants p ON p.event_id=a.event_id AND p.student_id=a.student_id
          WHERE a.event_id=e.id AND p.selection_status='Selected') marked,
        (SELECT COUNT(*) FROM attendance a JOIN event_participants p ON p.event_id=a.event_id AND p.student_id=a.student_id
          WHERE a.event_id=e.id AND a.status='Present' AND p.selection_status='Selected') present
        FROM events e WHERE e.status<>'Cancelled' ORDER BY e.event_date DESC,e.name''').fetchall()
    pending=sum(max(0,r['selected']-r['marked']) for r in rows)
    summary=db().execute('''SELECT COUNT(DISTINCT a.student_id) unique_students, COUNT(*) attendance_records
        FROM attendance a JOIN events e ON e.id=a.event_id
        WHERE a.status='Present' AND e.status<>'Cancelled' ''').fetchone()
    return render_template('attendance.html',events=rows,pending=pending,summary=summary)

@app.get('/attendance/export/<int:eid>.csv')
@login_required
def attendance_event_csv(eid):
    conn=db()
    event=conn.execute("SELECT id,name,event_date FROM events WHERE id=? AND status<>'Cancelled'",(eid,)).fetchone()
    if not event:
        flash('That event is unavailable for attendance export.','error')
        return redirect(url_for('attendance_page'))
    students=conn.execute('''SELECT s.register_no,s.name,s.department,s.class_name,s.section,a.status,a.credit_points,e.semester
        FROM attendance a JOIN students s ON s.id=a.student_id
        JOIN events e ON e.id=a.event_id
        JOIN event_participants p ON p.event_id=a.event_id AND p.student_id=a.student_id AND p.selection_status='Selected'
        WHERE a.event_id=? AND a.status='Present'
        ORDER BY s.name,s.register_no''',(eid,)).fetchall()
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
    eligible_events=conn.execute("SELECT e.*,(SELECT COUNT(*) FROM event_participants p WHERE p.event_id=e.id AND p.selection_status='Selected') selected FROM events e WHERE e.status IN ('Upcoming','Open') ORDER BY e.event_date,e.name").fetchall()
    students=conn.execute('''SELECT s.id,s.name,s.register_no,s.department,s.class_name,s.section,
        COUNT(DISTINCT CASE WHEN a.status='Present' THEN a.event_id END) events_attended,
        MAX(CASE WHEN a.status='Present' THEN e.event_date END) last_event
        FROM students s LEFT JOIN attendance a ON a.student_id=s.id
        LEFT JOIN events e ON e.id=a.event_id
        WHERE s.yrc_status='Active'
        GROUP BY s.id
        ORDER BY events_attended ASC,
        CASE WHEN MAX(CASE WHEN a.status='Present' THEN e.event_date END) IS NULL THEN 0 ELSE 1 END ASC,
        last_event ASC,s.name ASC
        LIMIT 50''').fetchall()
    return render_template('fair_selection.html',events=eligible_events,students=students)

@app.get('/events/<int:eid>')
@login_required
def event_detail(eid):
    event=db().execute('SELECT * FROM events WHERE id=?',(eid,)).fetchone()
    if not event:return redirect(url_for('events'))
    participants=db().execute('''SELECT s.*,p.selection_status,a.status attendance,a.marked_at,
        (SELECT COUNT(*) FROM attendance old WHERE old.student_id=s.id AND old.status="Present") past_events
        FROM event_participants p JOIN students s ON s.id=p.student_id
        LEFT JOIN attendance a ON a.event_id=p.event_id AND a.student_id=p.student_id
        WHERE p.event_id=? ORDER BY s.name''',(eid,)).fetchall()
    available=db().execute("SELECT id,name,register_no,department,class_name,section FROM students WHERE yrc_status='Active' AND id NOT IN (SELECT student_id FROM event_participants WHERE event_id=? AND selection_status='Selected') ORDER BY name",(eid,)).fetchall()
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
    already=conn.execute("SELECT COUNT(*) FROM event_participants WHERE event_id=? AND selection_status='Selected'",(eid,)).fetchone()[0]
    capacity=event['max_participants'] if event['max_participants'] is not None else 100000
    limit=max(0,capacity-already)
    candidates=conn.execute('''SELECT id,attended,offers FROM (
      SELECT s.id,COUNT(DISTINCT CASE WHEN a.status='Present' THEN a.event_id END) attended,
        (SELECT COUNT(*) FROM selection_history h WHERE h.student_id=s.id AND h.decision='Selected' AND h.event_id<>?) offers
      FROM students s LEFT JOIN attendance a ON a.student_id=s.id
      WHERE s.yrc_status='Active' AND s.id NOT IN (SELECT student_id FROM event_participants WHERE event_id=? AND selection_status='Selected')
      GROUP BY s.id
    ) AS candidate_scores ORDER BY (attended*100+offers), RANDOM() LIMIT ?''',(eid,eid,limit)).fetchall()
    for rank,c in enumerate(candidates,1):
        score=c['attended']*100+c['offers']
        conn.execute('INSERT INTO event_participants(event_id,student_id,selected_by,selection_status) VALUES(?,?,? ,?) ON CONFLICT(event_id,student_id) DO UPDATE SET selection_status="Selected",selected_at=CURRENT_TIMESTAMP,selected_by=excluded.selected_by',(eid,c['id'],session['admin_id'],'Selected'))
        conn.execute('INSERT INTO selection_history(event_id,student_id,decision,attended_count,seat_offers,priority_score,tie_break,decided_by) VALUES(?,?,?,?,?,?,?,?)',(eid,c['id'],'Selected',c['attended'],c['offers'],score,'random tie break after priority score',session['admin_id']))
    audit('Fair recommendations generated','event',eid,'',len(candidates)); conn.commit(); flash(f'{len(candidates)} recommendations saved within the remaining capacity. Review the list before treating it as final.','success'); return redirect(url_for('event_detail',eid=eid))

@app.post('/events/<int:eid>/select')
@login_required
def select_student(eid):
    sid=int(request.form['student_id']); conn=db()
    event=conn.execute('SELECT max_participants FROM events WHERE id=?',(eid,)).fetchone()
    already=conn.execute("SELECT COUNT(*) FROM event_participants WHERE event_id=? AND selection_status='Selected'",(eid,)).fetchone()[0]
    if not event or (event['max_participants'] is not None and already>=event['max_participants']):
        flash('No seats remain for this event.','error'); return redirect(url_for('event_detail',eid=eid))
    student=conn.execute("SELECT yrc_status,name FROM students WHERE id=?",(sid,)).fetchone()
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
    eligible=conn.execute(f"SELECT id FROM students WHERE yrc_status='Active' AND id IN ({placeholders})",requested).fetchall()
    if len(eligible)!=len(requested):
        flash('One or more selected students are no longer active. Reload the page and review the list; no one was added.','error')
        return redirect(url_for('event_detail',eid=eid))
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

    roster=conn.execute("SELECT id,register_no,name FROM students WHERE yrc_status='Active'").fetchall()
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
    students=conn.execute("SELECT student_id FROM event_participants WHERE event_id=? AND selection_status='Selected'",(eid,)).fetchall()
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
        FROM students s WHERE s.yrc_status='Active' ORDER BY events,s.name''',semester_args*2).fetchall()
    attendance_rows=conn.execute('''SELECT s.id,s.register_no,s.name,s.department,s.class_name,s.section,
        e.name event_name,e.event_date
        FROM students s JOIN attendance a ON a.student_id=s.id AND a.status='Present'
        JOIN events e ON e.id=a.event_id
        WHERE s.yrc_status='Active' AND e.status<>'Cancelled' '''+semester_where+'''
        ORDER BY s.name,e.event_date,e.name''',semester_args).fetchall()
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
        WHERE a.status='Present' '''+semester_where+''' ORDER BY e.semester,e.event_date,s.name''',semester_args).fetchall()
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
                           credit_rows=credit_rows,credit_totals=sorted(credit_totals.values(),key=lambda item:item['name']))

@app.post('/reports/attendance/<int:attendance_id>/credit')
@login_required
def update_attendance_credit(attendance_id):
    try:
        credit_points=int(request.form.get('credit_points',''))
    except (TypeError,ValueError):
        flash('Enter a whole-number credit value of zero or more.','error')
        return redirect(url_for('reports',semester=request.form.get('semester','All')))
    if credit_points<0:
        flash('Credit points cannot be negative.','error')
        return redirect(url_for('reports',semester=request.form.get('semester','All')))
    conn=db()
    row=conn.execute('SELECT a.credit_points,a.status,e.semester FROM attendance a JOIN events e ON e.id=a.event_id WHERE a.id=?',(attendance_id,)).fetchone()
    if not row or row['status']!='Present':
        flash('Credit can only be marked for a present attendance record.','error')
        return redirect(url_for('reports',semester=request.form.get('semester','All')))
    semester_filter=request.form.get('semester','All')
    if semester_filter in ('1','2','3','4') and row['semester']!=int(semester_filter):
        flash('That attendance record is not part of the selected semester.','error')
        return redirect(url_for('reports',semester=semester_filter))
    if semester_filter=='Unassigned' and row['semester'] is not None:
        flash('That attendance record is not part of the selected semester.','error')
        return redirect(url_for('reports',semester=semester_filter))
    conn.execute('UPDATE attendance SET credit_points=? WHERE id=?',(credit_points,attendance_id))
    audit('Attendance credit updated','attendance',attendance_id,row['credit_points'],credit_points)
    conn.commit()
    flash('Credit points saved. Previous semester records remain unchanged.','success')
    return redirect(url_for('reports',semester=request.form.get('semester','All')))

@app.route('/eligibility', methods=['GET','POST'])
@login_required
def eligibility():
    folder_url=''
    results=[]
    summary=None
    error=''
    semester_filter=request.values.get('semester','All')
    if semester_filter not in ('1','2','3','4','Unassigned'):
        semester_filter='All'
    if request.method=='POST':
        folder_url=request.form.get('folder_url','').strip()
        try:
            folder_id=extract_drive_folder_id(folder_url)
            pdf_names=fetch_drive_pdf_names(folder_id,os.getenv('GOOGLE_DRIVE_API_KEY','').strip())
        except DriveFolderError as exc:
            error=str(exc)
        else:
            semester_condition=''
            semester_args=[]
            if semester_filter in ('1','2','3','4'):
                semester_condition=' AND e.semester=?'
                semester_args.append(int(semester_filter))
            elif semester_filter=='Unassigned':
                semester_condition=' AND e.semester IS NULL'
            students=db().execute('''SELECT s.id,s.register_no,s.name,s.department,s.class_name,s.section,
                (SELECT COUNT(DISTINCT a.event_id) FROM attendance a JOIN events e ON e.id=a.event_id
                 WHERE a.student_id=s.id AND a.status='Present' AND e.status<>'Cancelled' '''+semester_condition+''') events
                FROM students s WHERE s.yrc_status='Active' ORDER BY s.name''',semester_args).fetchall()
            pdf_matches={student['id']:0 for student in students}
            unmatched_files=0
            for filename in pdf_names:
                matched=False
                for student in students:
                    roll_number=student['register_no'].strip()
                    if roll_number and re.search(rf'(?<![A-Za-z0-9]){re.escape(roll_number)}(?![A-Za-z0-9])',filename,re.IGNORECASE):
                        pdf_matches[student['id']]+=1
                        matched=True
                if not matched:
                    unmatched_files+=1
            for student in students:
                certificates=pdf_matches[student['id']]
                events=student['events']
                results.append({
                    'register_no':student['register_no'],
                    'name':student['name'],
                    'department':student['department'],
                    'class_name':student['class_name'],
                    'section':student['section'],
                    'events':events,
                    'certificates':certificates,
                    'eligible':events>=ELIGIBILITY_EVENT_THRESHOLD and certificates>=ELIGIBILITY_CERTIFICATE_THRESHOLD,
                })
            eligible_count=sum(1 for student in results if student['eligible'])
            summary={
                'pdf_count':len(pdf_names),
                'unmatched_files':unmatched_files,
                'eligible_count':eligible_count,
                'not_eligible_count':len(results)-eligible_count,
            }
    return render_template('eligibility.html',folder_url=folder_url,results=results,summary=summary,error=error,
                           semester_filter=semester_filter,
                           drive_api_configured=bool(os.getenv('GOOGLE_DRIVE_API_KEY','').strip()))

def semester_eligibility(semester_id, student_id):
    """Calculate eligibility only from verified attendance and verified required certificates."""
    conn=db()
    semester=conn.execute('SELECT * FROM semesters WHERE id=?',(semester_id,)).fetchone()
    if not semester:
        return {'status':'INCOMPLETE_RECORDS','activities':0,'minimum':0,'certificates':[],'reasons':['Semester configuration is missing.']}
    rules=conn.execute('SELECT * FROM semester_rules WHERE semester_id=? AND enabled=1 ORDER BY id',(semester_id,)).fetchall()
    min_rule=next((r for r in rules if r['rule_type']=='minimum_activities' and r['mandatory']),None)
    cert_rules=[name.strip() for r in rules if r['rule_type']=='certificate' and r['mandatory'] for name in r['value'].split(',') if name.strip()]
    if not rules:
        return {'status':'INCOMPLETE_RECORDS','activities':0,'minimum':0,'certificates':[],'reasons':['Eligibility criteria have not been configured.']}
    semester_filter=' AND e.semester=?'
    activities=conn.execute("SELECT COUNT(DISTINCT a.event_id) FROM attendance a JOIN events e ON e.id=a.event_id WHERE a.student_id=? AND a.status='Present' AND e.status<>'Cancelled' AND e.counts_for_eligibility=1"+semester_filter,(student_id,semester_id)).fetchone()[0]
    minimum=int(min_rule['value']) if min_rule else 0
    reasons=[]
    if min_rule and activities<minimum:
        remaining=minimum-activities
        reasons.append(f'Complete {remaining} more eligible activit{"y" if remaining==1 else "ies"}.')
    cert_results=[]
    pending=False
    for cert_type in cert_rules:
        cert=conn.execute('SELECT status FROM semester_certificates WHERE semester_id=? AND student_id=? AND certificate_type=?',(semester_id,student_id,cert_type)).fetchone()
        state=cert['status'] if cert else 'Missing'
        cert_results.append({'type':cert_type,'status':state})
        if state!='Verified':
            if state=='Submitted':
                pending=True; reasons.append(f'{cert_type} certificate is awaiting verification.')
            elif state=='Rejected': reasons.append(f'{cert_type} certificate was rejected; resubmission is required.')
            else: reasons.append(f'{cert_type} certificate submission is pending.')
    if not reasons:
        reasons=['You have met all eligibility requirements.']
    status='ELIGIBLE' if activities>=minimum and all(c['status']=='Verified' for c in cert_results) else ('PENDING_VERIFICATION' if pending else 'NOT_ELIGIBLE')
    reason_text='; '.join(reasons)
    conn.execute('INSERT INTO semester_eligibility_history(semester_id,student_id,status,reasons) VALUES(?,?,?,?) ON CONFLICT(semester_id,student_id) DO UPDATE SET status=excluded.status,reasons=excluded.reasons,calculated_at=CURRENT_TIMESTAMP',(semester_id,student_id,status,reason_text))
    return {'status':status,'activities':activities,'minimum':minimum,'certificates':cert_results,'reasons':reasons}

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
            flash('This semester is finalized. Reopen it before changing eligibility or certificate records.','error')
            return redirect(url_for('semester_detail',semester_id=semester_id))
        if action=='rules':
            if semester['locked']:
                flash('Reopen this semester before changing its criteria.','error'); return redirect(url_for('semester_detail',semester_id=semester_id))
            try:
                minimum=int(request.form.get('minimum_activities','0'))
                if minimum<0 or minimum>100: raise ValueError
            except ValueError:
                flash('Activity minimum must be a whole number from 0 to 100.','error'); return redirect(url_for('semester_detail',semester_id=semester_id))
            cert_types=[]
            for cert in request.form.get('certificates','').split(','):
                cert=cert.strip()
                if cert and cert not in cert_types: cert_types.append(cert)
            old=[dict(r) for r in conn.execute('SELECT rule_type,value,mandatory,verification_required,enabled FROM semester_rules WHERE semester_id=?',(semester_id,)).fetchall()]
            conn.execute('DELETE FROM semester_rules WHERE semester_id=?',(semester_id,))
            if minimum>0: conn.execute("INSERT INTO semester_rules(semester_id,rule_type,value,mandatory) VALUES(?,'minimum_activities',?,1)",(semester_id,str(minimum)))
            if cert_types: conn.execute("INSERT INTO semester_rules(semester_id,rule_type,value,mandatory,verification_required) VALUES(?,'certificate',?,1,1)",(semester_id,', '.join(cert_types)))
            audit('Semester eligibility rules changed','semester',semester_id,old,{'minimum_activities':minimum,'certificates':cert_types})
            for student in conn.execute("SELECT id FROM students WHERE yrc_status='Active'").fetchall(): semester_eligibility(semester_id,student['id'])
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
        elif action=='certificate':
            sid=request.form.get('student_id',''); cert_type=request.form.get('certificate_type','').strip(); reference=request.form.get('reference','').strip()
            student=conn.execute('SELECT id FROM students WHERE id=?',(sid,)).fetchone()
            if not student or not cert_type:
                flash('Choose a student and enter a certificate type.','error')
            else:
                conn.execute("INSERT INTO semester_certificates(semester_id,student_id,certificate_type,reference,status) VALUES(?,?,?,?,'Submitted') ON CONFLICT(semester_id,student_id,certificate_type) DO UPDATE SET reference=excluded.reference,status='Submitted',verified_by=NULL,verified_at=NULL",(semester_id,sid,cert_type,reference))
                audit('Certificate submission recorded','semester_certificate',sid,'','Submitted '+cert_type)
                semester_eligibility(semester_id,sid); conn.commit(); flash('Certificate submission recorded as awaiting verification.','success')
        elif action=='verify':
            cert_id=request.form.get('certificate_id'); decision=request.form.get('decision')
            if decision not in ('Verified','Rejected'):
                flash('Choose a valid verification decision.','error')
            else:
                cert=conn.execute('SELECT * FROM semester_certificates WHERE id=? AND semester_id=?',(cert_id,semester_id)).fetchone()
                if cert:
                    conn.execute('UPDATE semester_certificates SET status=?,verified_by=?,verified_at=CURRENT_TIMESTAMP WHERE id=?',(decision,session.get('admin_id'),cert_id))
                    audit('Certificate verification decision','semester_certificate',cert_id,cert['status'],decision)
                    semester_eligibility(semester_id,cert['student_id']); conn.commit(); flash('Certificate decision saved.','success')
    rules=conn.execute('SELECT * FROM semester_rules WHERE semester_id=? AND enabled=1 ORDER BY id',(semester_id,)).fetchall()
    cert_types=', '.join(r['value'] for r in rules if r['rule_type']=='certificate')
    min_rule=next((r for r in rules if r['rule_type']=='minimum_activities'),None)
    students=conn.execute("SELECT id,register_no,name,department,class_name,section FROM students WHERE yrc_status='Active' ORDER BY name").fetchall()
    rows=[]
    for student in students:
        result=semester_eligibility(semester_id,student['id'])
        rows.append({'student':student,'result':result})
    conn.commit()
    certificates=conn.execute('SELECT c.*,s.name,s.register_no FROM semester_certificates c JOIN students s ON s.id=c.student_id WHERE c.semester_id=? ORDER BY c.created_at DESC',(semester_id,)).fetchall()
    counts={key:sum(1 for row in rows if row['result']['status']==key) for key in ('ELIGIBLE','NOT_ELIGIBLE','PENDING_VERIFICATION','INCOMPLETE_RECORDS')}
    return render_template('semester_detail.html',semester=semester,rows=rows,students=students,certificates=certificates,minimum=min_rule['value'] if min_rule else '',cert_types=cert_types,counts=counts,configured=bool(rules))

@app.get('/semesters/<int:semester_id>/report.csv')
@login_required
def semester_report_csv(semester_id):
    semester=db().execute('SELECT name FROM semesters WHERE id=?',(semester_id,)).fetchone()
    if not semester: return 'Semester not found',404
    output=io.StringIO(); writer=csv.writer(output)
    writer.writerow(['Semester','Register number','Student','Department','Class','Activities completed','Activities required','Certificates','Status','Remaining requirements'])
    students=db().execute("SELECT id,register_no,name,department,class_name FROM students WHERE yrc_status='Active' ORDER BY name").fetchall()
    for student in students:
        result=semester_eligibility(semester_id,student['id'])
        certs='; '.join(f"{c['type']}: {c['status']}" for c in result['certificates'])
        writer.writerow([semester['name'],student['register_no'],student['name'],student['department'],student['class_name'],result['activities'],result['minimum'],certs,result['status'],'; '.join(result['reasons'])])
    db().commit()
    return Response('\ufeff'+output.getvalue(),mimetype='text/csv',headers={'Content-Disposition':f'attachment; filename="semester-{semester_id}-eligibility.csv"'})

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
    rows=db().execute('''SELECT s.register_no,s.name,s.department,s.class_name,s.section,s.year,s.email,s.phone,s.yrc_status,
        (SELECT COUNT(DISTINCT a.event_id) FROM attendance a WHERE a.student_id=s.id AND a.status='Present') events
        FROM students s ORDER BY s.name''').fetchall()
    buf=io.StringIO(); writer=csv.writer(buf)
    writer.writerow(['Register No','Name','Department','Class','Section','Year','Email','Phone','YRC Status','Events Attended'])
    columns=('register_no','name','department','class_name','section','year','email','phone','yrc_status','events')
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
