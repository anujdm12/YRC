import os
import sqlite3
import secrets
import time
import click
import csv
import io
from datetime import datetime, date
from functools import wraps
from flask import Flask, g, render_template, request, redirect, url_for, session, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY') or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
                  SESSION_COOKIE_SECURE=os.getenv('COOKIE_SECURE', '0') == '1')
DB_PATH = os.getenv('DATABASE_PATH', 'yrc.db')

@app.context_processor
def inject_now():
    today=date.today()
    return {'now': today.strftime('%d %b %Y'), 'today_long': today.strftime('%A, %d %B %Y').upper()}

SCHEMA = '''
CREATE TABLE IF NOT EXISTS admins(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS admin_roles(admin_id INTEGER PRIMARY KEY REFERENCES admins(id) ON DELETE CASCADE, role TEXT NOT NULL DEFAULT 'super_admin', department TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS students(id INTEGER PRIMARY KEY, register_no TEXT UNIQUE NOT NULL, name TEXT NOT NULL, department TEXT NOT NULL, class_name TEXT NOT NULL DEFAULT '', section TEXT NOT NULL DEFAULT '', year INTEGER NOT NULL, email TEXT, phone TEXT, yrc_status TEXT NOT NULL DEFAULT 'Active', created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT, event_date TEXT NOT NULL, start_time TEXT, end_time TEXT, venue TEXT, max_participants INTEGER, credit_points REAL NOT NULL DEFAULT 0, deadline TEXT, status TEXT NOT NULL DEFAULT 'Upcoming', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS event_participants(id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE, student_id INTEGER NOT NULL REFERENCES students(id), selection_status TEXT NOT NULL DEFAULT 'Selected', selected_at TEXT DEFAULT CURRENT_TIMESTAMP, selected_by INTEGER, UNIQUE(event_id,student_id));
CREATE TABLE IF NOT EXISTS selection_history(id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL, student_id INTEGER NOT NULL, decision TEXT NOT NULL, attended_count INTEGER NOT NULL DEFAULT 0, seat_offers INTEGER NOT NULL DEFAULT 0, priority_score INTEGER NOT NULL DEFAULT 0, tie_break TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '', decided_by INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS attendance(id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL, student_id INTEGER NOT NULL, status TEXT NOT NULL, marked_at TEXT DEFAULT CURRENT_TIMESTAMP, marked_by INTEGER, UNIQUE(event_id,student_id), FOREIGN KEY(event_id,student_id) REFERENCES event_participants(event_id,student_id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS credits(id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL, student_id INTEGER NOT NULL, credit_points REAL NOT NULL, status TEXT NOT NULL DEFAULT 'Awarded', awarded_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_by INTEGER, UNIQUE(event_id,student_id), FOREIGN KEY(event_id,student_id) REFERENCES event_participants(event_id,student_id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS login_attempts(id INTEGER PRIMARY KEY, username TEXT NOT NULL, attempted_at INTEGER NOT NULL, success INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS admin_audit_logs(id INTEGER PRIMARY KEY, admin_id INTEGER, action TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id INTEGER, old_value TEXT, new_value TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
'''

def db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA foreign_keys = ON')
    return g.db

@app.teardown_appcontext
def close_db(_):
    conn = g.pop('db', None)
    if conn: conn.close()

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    student_columns = {row[1] for row in conn.execute('PRAGMA table_info(students)')}
    if 'class_name' not in student_columns:
        conn.execute("ALTER TABLE students ADD COLUMN class_name TEXT NOT NULL DEFAULT ''")
    if 'section' not in student_columns:
        conn.execute("ALTER TABLE students ADD COLUMN section TEXT NOT NULL DEFAULT ''")
    # Older records stored department and program together, separated by an em dash.
    conn.execute("UPDATE students SET class_name=TRIM(SUBSTR(department, INSTR(department, '—') + 1)), department=TRIM(SUBSTR(department, 1, INSTR(department, '—') - 1)) WHERE class_name='' AND INSTR(department, '—') > 0")
    conn.execute("INSERT OR IGNORE INTO admin_roles(admin_id,role,department) SELECT id,'super_admin','' FROM admins")
    bootstrap_username=os.getenv('INITIAL_ADMIN_USERNAME','').strip()
    bootstrap_password=os.getenv('INITIAL_ADMIN_PASSWORD','')
    if bool(bootstrap_username) != bool(bootstrap_password):
        conn.close()
        raise RuntimeError('Set both INITIAL_ADMIN_USERNAME and INITIAL_ADMIN_PASSWORD to bootstrap the first admin.')
    if bootstrap_username and len(bootstrap_password) < 12:
        conn.close()
        raise RuntimeError('INITIAL_ADMIN_PASSWORD must contain at least 12 characters.')
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
    if len(password) < 12:
        raise click.ClickException('Use a password with at least 12 characters.')
    conn=sqlite3.connect(DB_PATH)
    try:
        cur=conn.execute('INSERT INTO admins(username,password_hash) VALUES(?,?)',(username.strip(),generate_password_hash(password)))
        conn.execute("INSERT INTO admin_roles(admin_id,role,department) VALUES(?,'super_admin','')",(cur.lastrowid,))
        conn.commit()
    except sqlite3.IntegrityError as exc:
        raise click.ClickException('That username already exists.') from exc
    finally: conn.close()

@app.cli.command('set-admin-password')
@click.option('--username', prompt=True)
@click.option('--password', prompt=True, hide_input=True, confirmation_prompt=True)
def set_admin_password(username, password):
    """Reset an existing administrator password."""
    if len(password) < 12:
        raise click.ClickException('Use a password with at least 12 characters.')
    conn=sqlite3.connect(DB_PATH)
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
      'credits':conn.execute("SELECT COALESCE(SUM(credit_points),0) FROM credits WHERE status='Awarded'").fetchone()[0],
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
        except sqlite3.IntegrityError: conn.rollback(); flash('Register number already exists.','error')
        return redirect(url_for('students'))
    q=request.args.get('q','').strip(); status=request.args.get('status',''); dept=request.args.get('department',''); section=request.args.get('section','').upper()
    where=[]; args=[]
    if q: where.append('(name LIKE ? OR register_no LIKE ?)'); args += [f'%{q}%',f'%{q}%']
    if status: where.append('yrc_status=?'); args.append(status)
    if dept: where.append('department=?'); args.append(dept)
    if section=='UNASSIGNED': where.append("section=''")
    elif section: where.append('section=?'); args.append(section)
    sql='SELECT s.*, (SELECT COUNT(DISTINCT a.event_id) FROM attendance a WHERE a.student_id=s.id AND a.status="Present") events, (SELECT COALESCE(SUM(c.credit_points),0) FROM credits c WHERE c.student_id=s.id AND c.status="Awarded") credits FROM students s'+(' WHERE '+' AND '.join(where) if where else '')+' ORDER BY s.name'
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
    except sqlite3.IntegrityError:
        conn.rollback(); flash('The import was rejected by a database constraint. No students were imported; check register numbers and required fields.','error'); return redirect(url_for('students'))
    flash(f'Imported all {len(records)} students successfully.','success'); return redirect(url_for('students'))

@app.get('/students/<int:sid>')
@login_required
def student_detail(sid):
    s=db().execute('SELECT s.*,(SELECT COUNT(DISTINCT a.event_id) FROM attendance a WHERE a.student_id=s.id AND a.status="Present") events,(SELECT COALESCE(SUM(c.credit_points),0) FROM credits c WHERE c.student_id=s.id AND c.status="Awarded") credits,(SELECT MAX(e.event_date) FROM attendance a JOIN events e ON e.id=a.event_id WHERE a.student_id=s.id AND a.status="Present") last_event,(SELECT COUNT(*) FROM events e WHERE e.status="Completed") total_events FROM students s WHERE s.id=?',(sid,)).fetchone()
    if not s: return redirect(url_for('students'))
    history=db().execute('SELECT e.name,e.event_date,a.status,COALESCE(c.credit_points,0) credits FROM attendance a JOIN events e ON e.id=a.event_id LEFT JOIN credits c ON c.event_id=a.event_id AND c.student_id=a.student_id WHERE a.student_id=? ORDER BY e.event_date DESC',(sid,)).fetchall()
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
    except sqlite3.IntegrityError: conn.rollback(); flash('Register number already belongs to another student.','error')
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
            points=float(f.get('credit_points') or 0)
        except (TypeError,ValueError):
            flash('Enter a valid event date, seat capacity, and credit reference amount.','error'); return redirect(url_for('events'))
        status=f.get('status','Upcoming')
        if not name or (capacity is not None and capacity<1) or points<0 or status not in ('Draft','Upcoming','Open','Closed','Completed','Cancelled'):
            flash('Check the event name, positive seat capacity, nonnegative credit amount, and status.','error'); return redirect(url_for('events'))
        cur=conn.execute('INSERT INTO events(name,description,event_date,start_time,end_time,venue,max_participants,credit_points,deadline,status) VALUES(?,?,?,?,?,?,?,?,?,?)',(name,f.get('description'),f['event_date'],f.get('start_time'),f.get('end_time'),f.get('venue'),capacity,points,f.get('deadline'),status))
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
        ORDER BY events_attended ASC, CASE WHEN last_event IS NULL THEN 0 ELSE 1 END ASC,last_event ASC,s.name ASC
        LIMIT 50''').fetchall()
    return render_template('fair_selection.html',events=eligible_events,students=students)

@app.get('/events/<int:eid>')
@login_required
def event_detail(eid):
    event=db().execute('SELECT * FROM events WHERE id=?',(eid,)).fetchone()
    if not event:return redirect(url_for('events'))
    participants=db().execute('SELECT s.*,p.selection_status,a.status attendance,a.marked_at,c.credit_points,(SELECT COUNT(*) FROM attendance old WHERE old.student_id=s.id AND old.status="Present") past_events FROM event_participants p JOIN students s ON s.id=p.student_id LEFT JOIN attendance a ON a.event_id=p.event_id AND a.student_id=p.student_id LEFT JOIN credits c ON c.event_id=p.event_id AND c.student_id=p.student_id WHERE p.event_id=? ORDER BY s.name',(eid,)).fetchall()
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
        conn.commit(); flash('Cancelled event and its linked participation, attendance, and credit records were permanently deleted.','success')
    except sqlite3.IntegrityError:
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
    candidates=conn.execute('''SELECT s.id,COUNT(DISTINCT CASE WHEN a.status='Present' THEN a.event_id END) attended,
      (SELECT COUNT(*) FROM selection_history h WHERE h.student_id=s.id AND h.decision='Selected' AND h.event_id<>?) offers
      FROM students s LEFT JOIN attendance a ON a.student_id=s.id
      WHERE s.yrc_status='Active' AND s.id NOT IN (SELECT student_id FROM event_participants WHERE event_id=? AND selection_status='Selected')
      GROUP BY s.id ORDER BY (attended*100+offers), RANDOM() LIMIT ?''',(eid,eid,limit)).fetchall()
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
        credit=conn.execute("SELECT status FROM credits WHERE event_id=? AND student_id=?",(eid,sid)).fetchone()
        if credit and credit['status']=='Awarded':
            flash('A verified credit requires an audited amendment; attendance cannot be undone here.','error')
            return redirect(url_for('event_detail',eid=eid))
        conn.execute('DELETE FROM attendance WHERE event_id=? AND student_id=?',(eid,sid)); conn.execute('DELETE FROM credits WHERE event_id=? AND student_id=?',(eid,sid))
    else:
        conn.execute('INSERT INTO attendance(event_id,student_id,status,marked_by) VALUES(?,?,?,?) ON CONFLICT(event_id,student_id) DO UPDATE SET status=excluded.status,marked_at=CURRENT_TIMESTAMP,marked_by=excluded.marked_by',(eid,sid,status,session['admin_id']))
        if status=='Present':
            conn.execute('INSERT INTO credits(event_id,student_id,credit_points,status,updated_by) VALUES(?,?,0,"Pending",?) ON CONFLICT(event_id,student_id) DO NOTHING',(eid,sid,session['admin_id']))
        elif status in ('Absent','Excused'):
            conn.execute("UPDATE credits SET status='Not eligible',credit_points=0,updated_by=? WHERE event_id=? AND student_id=? AND status<>'Awarded'",(session['admin_id'],eid,sid))
    audit('Attendance '+status,'attendance',eid,'',sid); conn.commit(); return redirect(url_for('event_detail',eid=eid))

@app.get('/reports')
@login_required
def reports():
    conn=db(); rows=conn.execute('SELECT s.register_no,s.name,s.department,s.class_name,s.section,(SELECT COUNT(DISTINCT a.event_id) FROM attendance a WHERE a.student_id=s.id AND a.status="Present") events,(SELECT COALESCE(SUM(c.credit_points),0) FROM credits c WHERE c.student_id=s.id AND c.status="Awarded") credits,(SELECT MAX(e.event_date) FROM attendance a JOIN events e ON e.id=a.event_id WHERE a.student_id=s.id AND a.status="Present") last_event FROM students s ORDER BY events,credits').fetchall()
    evrows=conn.execute('SELECT e.name,e.event_date,(SELECT COUNT(*) FROM event_participants p WHERE p.event_id=e.id AND p.selection_status="Selected") selected,(SELECT COUNT(*) FROM attendance a WHERE a.event_id=e.id AND a.status="Present") present,(SELECT COUNT(*) FROM attendance a WHERE a.event_id=e.id AND a.status="Absent") absent FROM events e ORDER BY e.event_date DESC').fetchall()
    return render_template('reports.html',rows=rows,events=evrows)

@app.get('/credits')
@login_required
def credits_page():
    conn=db()
    rows=conn.execute('SELECT c.event_id,c.student_id,c.awarded_at,c.credit_points,c.status,e.name event_name,e.event_date,s.register_no,s.name student_name,s.department,s.class_name,s.section FROM credits c JOIN events e ON e.id=c.event_id JOIN students s ON s.id=c.student_id ORDER BY c.awarded_at DESC').fetchall()
    totals={'points':conn.execute("SELECT COALESCE(SUM(credit_points),0) FROM credits WHERE status='Awarded'").fetchone()[0], 'records':len(rows), 'students':conn.execute("SELECT COUNT(DISTINCT student_id) FROM credits WHERE status='Awarded'").fetchone()[0]}
    return render_template('credits.html',rows=rows,totals=totals)

@app.post('/credits/<int:eid>/<int:sid>/verify')
@login_required
def verify_credit(eid,sid):
    conn=db()
    role=conn.execute('SELECT role FROM admin_roles WHERE admin_id=?',(session['admin_id'],)).fetchone()
    if not role or role['role']!='super_admin':
        flash('Only a university coordinator can verify official credit awards.','error')
        return redirect(url_for('credits_page'))
    row=conn.execute("SELECT status,credit_points FROM credits WHERE event_id=? AND student_id=?",(eid,sid)).fetchone()
    try: amount=float(request.form.get('credit_points',''))
    except ValueError: amount=-1
    if not row or row['status'] not in ('Pending','Disputed') or amount < 0:
        flash('This record cannot be verified. Check its status and enter a valid nonnegative credit amount.','error')
        return redirect(url_for('credits_page'))
    conn.execute("UPDATE credits SET credit_points=?,status='Awarded',awarded_at=CURRENT_TIMESTAMP,updated_by=? WHERE event_id=? AND student_id=?",(amount,session['admin_id'],eid,sid))
    audit('Credit verified','credit',eid,f"{row['status']}:{row['credit_points']}",f'Awarded:{amount}; student {sid}')
    conn.commit(); flash('Credit decision verified and recorded.','success')
    return redirect(url_for('credits_page'))

@app.get('/reports/students.csv')
@login_required
def students_csv():
    import csv, io
    rows=db().execute('SELECT s.register_no,s.name,s.department,s.class_name,s.section,(SELECT COUNT(DISTINCT a.event_id) FROM attendance a WHERE a.student_id=s.id AND a.status="Present") events,(SELECT COALESCE(SUM(c.credit_points),0) FROM credits c WHERE c.student_id=s.id AND c.status="Awarded") credits FROM students s ORDER BY s.name').fetchall()
    buf=io.StringIO(); writer=csv.writer(buf); writer.writerow(['Register No','Name','Department','Class','Section','Events','Credits']); writer.writerows([tuple(r) for r in rows])
    from flask import Response
    return Response('\ufeff'+buf.getvalue(),mimetype='text/csv',headers={'Content-Disposition':'attachment; filename=yrc-student-credits.csv'})

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
