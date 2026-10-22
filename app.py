import os
import secrets
import time
import click
import csv
import io
import re
import unicodedata
from datetime import datetime, date
from functools import wraps
from flask import Flask, g, render_template, request, redirect, url_for, session, flash, jsonify, Response
from werkzeug.security import generate_password_hash, check_password_hash
from database import connect_db, DB_ERROR, DB_INTEGRITY_ERROR, DB_IMPORT_ERROR, USING_POSTGRES

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY') or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
                  SESSION_COOKIE_SECURE=os.getenv('COOKIE_SECURE', '0') == '1')
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024
DB_PATH = os.getenv('DATABASE_PATH', 'yrc.db')

@app.context_processor
def inject_now():
    today=date.today()
    return {'now': today.strftime('%d %b %Y'), 'today_long': today.strftime('%A, %d %B %Y').upper()}

SCHEMA = '''
CREATE TABLE IF NOT EXISTS admins(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS admin_roles(admin_id INTEGER PRIMARY KEY REFERENCES admins(id) ON DELETE CASCADE, role TEXT NOT NULL DEFAULT 'super_admin', department TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS students(id INTEGER PRIMARY KEY, register_no TEXT UNIQUE NOT NULL, name TEXT NOT NULL, department TEXT NOT NULL, class_name TEXT NOT NULL DEFAULT '', section TEXT NOT NULL DEFAULT '', year INTEGER NOT NULL, email TEXT, phone TEXT, yrc_status TEXT NOT NULL DEFAULT 'Active', created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT, event_date TEXT NOT NULL, start_time TEXT, end_time TEXT, venue TEXT, max_participants INTEGER, deadline TEXT, status TEXT NOT NULL DEFAULT 'Upcoming', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS event_participants(id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE, student_id INTEGER NOT NULL REFERENCES students(id), selection_status TEXT NOT NULL DEFAULT 'Selected', selected_at TEXT DEFAULT CURRENT_TIMESTAMP, selected_by INTEGER, UNIQUE(event_id,student_id));
CREATE TABLE IF NOT EXISTS selection_history(id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL, student_id INTEGER NOT NULL, decision TEXT NOT NULL, attended_count INTEGER NOT NULL DEFAULT 0, seat_offers INTEGER NOT NULL DEFAULT 0, priority_score INTEGER NOT NULL DEFAULT 0, tie_break TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '', decided_by INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS attendance(id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL, student_id INTEGER NOT NULL, status TEXT NOT NULL, marked_at TEXT DEFAULT CURRENT_TIMESTAMP, marked_by INTEGER, UNIQUE(event_id,student_id), FOREIGN KEY(event_id,student_id) REFERENCES event_participants(event_id,student_id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS login_attempts(id INTEGER PRIMARY KEY, username TEXT NOT NULL, attempted_at INTEGER NOT NULL, success INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS admin_audit_logs(id INTEGER PRIMARY KEY, admin_id INTEGER, action TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id INTEGER, old_value TEXT, new_value TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS participation_import_batches(id TEXT PRIMARY KEY, admin_id INTEGER NOT NULL, filename TEXT NOT NULL, response_count INTEGER NOT NULL DEFAULT 0, unique_respondents INTEGER NOT NULL DEFAULT 0, repeated_respondents INTEGER NOT NULL DEFAULT 0, missing_register_count INTEGER NOT NULL DEFAULT 0, record_count INTEGER NOT NULL DEFAULT 0, imported_count INTEGER NOT NULL DEFAULT 0, skipped_count INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'Preview', created_at TEXT DEFAULT CURRENT_TIMESTAMP, completed_at TEXT);
CREATE TABLE IF NOT EXISTS participation_import_rows(id INTEGER PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES participation_import_batches(id) ON DELETE CASCADE, source_row INTEGER NOT NULL, register_no TEXT NOT NULL, student_name TEXT NOT NULL, event_name TEXT NOT NULL, student_id INTEGER, error TEXT NOT NULL DEFAULT '', match_method TEXT NOT NULL DEFAULT 'register_no', UNIQUE(batch_id,source_row,event_name));
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
    student_columns = {row[1] for row in conn.execute('PRAGMA table_info(students)')}
    if 'class_name' not in student_columns:
        conn.execute("ALTER TABLE students ADD COLUMN class_name TEXT NOT NULL DEFAULT ''")
    if 'section' not in student_columns:
        conn.execute("ALTER TABLE students ADD COLUMN section TEXT NOT NULL DEFAULT ''")
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
    history=db().execute('SELECT e.name,e.event_date,a.status FROM attendance a JOIN events e ON e.id=a.event_id WHERE a.student_id=? ORDER BY e.event_date DESC',(sid,)).fetchall()
    return render_template('student_detail.html',student=s,history=history)

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
        try:
            date.fromisoformat(f.get('event_date',''))
            capacity=int(f.get('max_participants') or 0) or None
        except (TypeError,ValueError):
            flash('Enter a valid date and seat capacity.','error'); return redirect(url_for('events'))
        status=f.get('status','Upcoming')
        if (not name or (capacity is not None and capacity<1) or
            status not in ('Draft','Upcoming','Open','Closed','Completed','Cancelled')):
            flash('Check the event details.','error'); return redirect(url_for('events'))
        cur=conn.execute('''INSERT INTO events(name,description,event_date,start_time,end_time,venue,max_participants,deadline,status)
            VALUES(?,?,?,?,?,?,?,?,?)''',(name,f.get('description'),f['event_date'],f.get('start_time'),f.get('end_time'),f.get('venue'),capacity,f.get('deadline'),status))
        audit('Event created','event',cur.lastrowid,'',f['name']); conn.commit(); flash('Event created.','success'); return redirect(url_for('events'))
    q=request.args.get('q','').strip(); status=request.args.get('status','All')
    where=[]; args=[]
    if q: where.append('(e.name LIKE ? OR e.venue LIKE ?)'); args.extend((f'%{q}%',f'%{q}%'))
    if status in ('Draft','Upcoming','Open','Closed','Completed','Cancelled'): where.append('e.status=?'); args.append(status)
    rows=conn.execute('SELECT e.*,(SELECT COUNT(*) FROM event_participants p WHERE p.event_id=e.id AND p.selection_status="Selected") selected,(SELECT COUNT(*) FROM attendance a WHERE a.event_id=e.id AND a.status="Present") present FROM events e'+(' WHERE '+' AND '.join(where) if where else '')+' ORDER BY e.event_date DESC',args).fetchall()
    summary=conn.execute("SELECT COUNT(*) total, SUM(CASE WHEN status IN ('Upcoming','Open') THEN 1 ELSE 0 END) active, SUM(CASE WHEN status='Cancelled' THEN 1 ELSE 0 END) cancelled FROM events").fetchone()
    return render_template('events.html',events=rows,q=q,status=status,summary=summary)

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
    students=conn.execute('''SELECT s.register_no,s.name,s.department,s.class_name,s.section,a.status
        FROM attendance a JOIN students s ON s.id=a.student_id
        JOIN event_participants p ON p.event_id=a.event_id AND p.student_id=a.student_id AND p.selection_status='Selected'
        WHERE a.event_id=? AND a.status='Present'
        ORDER BY s.name,s.register_no''',(eid,)).fetchall()
    output=io.StringIO(newline='')
    writer=csv.writer(output)
    writer.writerow(['Event','Event Date','Register Number','Student Name','Department','Class','Section','Attendance'])
    for student in students:
        values=[event['name'],event['event_date'],student['register_no'],student['name'],
                student['department'],student['class_name'],student['section'],student['status']]
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
        current_roster_by_reg={}; current_roster_by_name={}
        current_roster_name_by_id={}
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
    conn=db(); rows=conn.execute('SELECT s.register_no,s.name,s.department,s.class_name,s.section,s.email,s.phone,(SELECT COUNT(DISTINCT a.event_id) FROM attendance a WHERE a.student_id=s.id AND a.status="Present") events,(SELECT MAX(e.event_date) FROM attendance a JOIN events e ON e.id=a.event_id WHERE a.student_id=s.id AND a.status="Present") last_event FROM students s WHERE s.yrc_status="Active" ORDER BY events,s.name').fetchall()
    attendance_rows=conn.execute('''SELECT s.id,s.register_no,s.name,s.department,s.class_name,s.section,
        e.name event_name,e.event_date
        FROM students s JOIN attendance a ON a.student_id=s.id AND a.status='Present'
        JOIN events e ON e.id=a.event_id
        WHERE s.yrc_status='Active'
        ORDER BY s.name,e.event_date,e.name''').fetchall()
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
    not_credit_eligible=[r for r in rows if r['events'] < 3]
    ineligible_by_department={}
    for student in not_credit_eligible:
        department=(student['department'] or 'Unassigned').strip()
        ineligible_by_department[department]=ineligible_by_department.get(department,0)+1
    department_counts=[{'name':department,'count':ineligible_by_department.get(department,0)}
                       for department in sorted({(r['department'] or 'Unassigned').strip() for r in rows})]
    evrows=conn.execute('SELECT e.name,e.event_date,(SELECT COUNT(*) FROM event_participants p WHERE p.event_id=e.id AND p.selection_status="Selected") selected,(SELECT COUNT(*) FROM attendance a WHERE a.event_id=e.id AND a.status="Present") present,(SELECT COUNT(*) FROM attendance a WHERE a.event_id=e.id AND a.status="Absent") absent FROM events e ORDER BY e.event_date DESC').fetchall()
    return render_template('reports.html',rows=rows,events=evrows,
                           not_credit_eligible=not_credit_eligible,
                           credit_eligible=[r for r in attended_rows if r['events'] >= 3],
                           total_attendances=total_attendances,
                           department_counts=department_counts)

@app.get('/reports/attended-students.csv')
@login_required
def attended_students_csv():
    rows=db().execute('''SELECT s.register_no,s.name,s.department,s.class_name,s.section,
        s.id student_id,e.name event_name,e.event_date
        FROM students s JOIN attendance a ON a.student_id=s.id AND a.status='Present'
        JOIN events e ON e.id=a.event_id
        WHERE s.yrc_status='Active'
        ORDER BY s.name,e.event_date,e.name''').fetchall()
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
    return Response('\ufeff'+buf.getvalue(),mimetype='text/csv',headers={'Content-Disposition':'attachment; filename=students-eligible-for-credits.csv'})

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
    rows=db().execute('''SELECT s.register_no,s.name,s.department,s.class_name,s.section,s.email,s.phone
        FROM students s WHERE s.yrc_status='Active'
        AND (SELECT COUNT(DISTINCT a.event_id) FROM attendance a WHERE a.student_id=s.id AND a.status='Present') < 3
        ORDER BY s.department,s.class_name,s.name''').fetchall()
    buf=io.StringIO(newline=''); writer=csv.writer(buf)
    writer.writerow(['Register No','Name','Department','Class','Section','Email','Phone','Priority'])
    for row in rows:
        values=[row['register_no'],row['name'],row['department'],row['class_name'],row['section'],row['email'],row['phone'],'Not eligible - fewer than 3 events attended']
        writer.writerow([("'"+str(value) if str(value or '').lstrip().startswith(('=','+','-','@')) else value) for value in values])
    return Response('\ufeff'+buf.getvalue(),mimetype='text/csv',headers={'Content-Disposition':'attachment; filename=students-not-eligible-for-credits.csv'})

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
