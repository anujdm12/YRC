# KJU YRC Admin Management Portal

A Flask admin application for the existing YRC roster, event, attendance, and credit records. It currently uses SQLite so it runs locally without a database server. The application also includes public volunteer self-registration and a role-scoped approval workflow.

## Volunteer self-registration

Apply at `/volunteer/register` and track an application at `/volunteer/track`. New submissions go directly to **Pending approval**; no email verification is required. The additive migration runs through `init_db()`. Super Admins manage the registration window in **Registration Management**. Department Admins need the existing `can_manage_volunteers` assignment and are scoped to their assigned department. Optional SMTP settings (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, and `SMTP_FROM`) deliver approval or rejection notices. Set `COOKIE_SECURE=1` when serving the portal over HTTPS.

## Run locally

1. Install Python 3.10 or later.
2. Create and activate a virtual environment, then install dependencies:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

3. Set a persistent secret before launch. Do not use the generated per-process development secret for production:

   ```powershell
   $env:SECRET_KEY = [Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(48))
   ```

4. For a new database, create the first administrator interactively:

   ```powershell
   $env:DATABASE_PATH = "yrc.db"
   flask --app app create-admin
   ```

   For an existing administrator, use `flask --app app set-admin-password`. Administrator passwords must be at least 8 characters. The database file currently in this workspace may contain an account created by an older version; reset it before use.

5. Start the app with `flask --app app run` and open `http://127.0.0.1:5000/login`.

For HTTPS deployments set `COOKIE_SECURE=1`, use a stable randomly generated `SECRET_KEY`, and place Flask behind a production WSGI server and TLS reverse proxy. The development server is not a production server.

## Current behavior and data safety

- All write forms require a session CSRF token. Login attempts are rate limited per username over a 15 minute window.
- Attendance creates a pending credit review record. A super-admin can verify a nonnegative award amount; attendance no longer automatically grants the event's configured points.
- The recommendation route orders active volunteers by `(previous attended events * 100) + previous selected seat offers`, applies the remaining seat capacity, and records its priority values and a random tie break.
- Deactivating a student and cancelling an event preserve linked history. Only selected participants can receive attendance statuses.
- CSV roster import validates the entire file before inserting any rows. The current roster schema retains the legacy student naming and class fields.
- Attendance has a reviewed Google Sheets CSV import. It uses the latest submission per register number, matches by register number first and falls back to a unique exact full name, combines Ethnic Day labels, and skips attendance already marked Present. Duplicate names remain unmatched for review. New events require an administrator-entered date; response timestamps are not treated as event dates. Preview details are removed after confirmation, while an import summary is retained.
- The Eligibility Check requires at least 3 attended, non-cancelled events and at least 3 certificates. Upload the certificate-count CSV; the evaluator matches registration numbers and reads only the completed certificate count column. Certificate type is ignored, and blank count cells are treated as zero. Choose a semester to check that period's attendance or All semesters for cumulative attendance.
- Events can be assigned to Semester 1–4. The Reports semester selector scopes the attendance eligibility lists, event attendance, credit entry, and student credit totals to the selected semester; All semesters remains available for cumulative reports. CSV report links preserve the selected semester. Administrators can enter nonnegative credit points for each present student-event record. Older events and attendance remain intact after the schema update; their semester is shown as Historical / unassigned until an administrator assigns one from the event page. Credit changes are written to the audit log.
- Students keep a permanent registration number, stable student ID, cohort label, and academic year. The additive cohort migration preserves every existing label and links it to a cohort registry without recalculating or resetting attendance. Existing records are marked pending review because their legacy free-text labels cannot safely be inferred from registration numbers.
- Cohort management (`/cohorts`) supports active and archived intake labels, configurable two-to-four-digit registration prefix mappings, volunteer/participation counts, pending verification review, and manual cohort confirmation. New entries use a configured mapping only when the registration number matches a validated format. Unknown or malformed prefixes remain pending rather than being guessed. Student and Reports cohort filters continue to use the stored cohort label; events remain shared and attendance remains attached to permanent student IDs.
- Department registry (`/departments`) is an additive first migration step. Startup creates a department record for each exact, nonblank legacy student department label and adds `students.department_id` plus department verification status; it does not change registration numbers or student IDs. Central administrators can create departments, edit their codes and status, and record leader/admin assignments. New student creation and CSV imports require a registered active department.
- Department leadership appointments are managed from **Students → Manage Student Convenors**. The volunteer home loads only the current leader and secretary appointments for the authenticated volunteer's verified department. Appointment photos, contact details, role, active dates, and retirement history are stored in the database; volunteers see a neutral verification or vacancy message when applicable.
- Administrator access supports only `super_admin` and `department_admin`. Admin accounts have active/suspended/revoked states and revocable session tokens; Super Admins manage accounts at `/admins`. Department Admins require exactly one active department assignment and see only Students, Events, Semester Eligibility, Batch, and Attendance routes. Their student, cohort, event participant, attendance, CSV, and semester volunteer data is department-scoped on the backend. CA and Computational Studies Department Admins each share access to both departments; new records they create default to their assigned department. Shared event settings, reports, imports, admin management, and global settings are restricted.
- The app supports SQLite locally (`DATABASE_PATH`, default `yrc.db`) and PostgreSQL through `DATABASE_URL` (including Neon). PostgreSQL schema setup is automatic; existing SQLite records must be migrated separately.

## Important scope gaps

Reports, global analytics, convenor management, and system settings remain Super Admin only. Department Admins use a department-scoped dashboard and the five permitted operational sections. Automated backup retention, a tested restore procedure, and a full deployment/recovery runbook remain operational follow-up work; review deployment configuration before using this app with production records.

## First administrator on Render

For a new deployment, set `INITIAL_ADMIN_USERNAME` and `INITIAL_ADMIN_PASSWORD` in the Render service environment before the first start. The password must contain at least 8 characters. The app creates this account only when the database has no administrator. Remove both bootstrap variables after the account is created; never put the password in this repository.

For a persistent hosted database, create a Neon PostgreSQL project, copy its pooled connection string, and add it to Render as the `DATABASE_URL` environment variable. Keep the URL secret and include `sslmode=require` if it is not already present. The app creates its tables on first start. `DATABASE_URL` takes precedence over SQLite's `DATABASE_PATH`.

Switching to Neon does not copy records automatically. With a local SQLite backup, install requirements, set `DATABASE_URL` in your shell, make sure `INITIAL_ADMIN_USERNAME` and `INITIAL_ADMIN_PASSWORD` are unset, then run `py migrate_sqlite_to_postgres.py --source yrc.db`. The migration stops if any destination table already has rows. For data still on a live Render instance, back it up before changing the database setting; the local migration script needs a copy of that SQLite file.

For PostgreSQL, create the first administrator with `INITIAL_ADMIN_USERNAME` and `INITIAL_ADMIN_PASSWORD`; remove those bootstrap values after the first successful start. To create or reset an account later, run `flask --app app create-admin` or `flask --app app set-admin-password` with the same `DATABASE_URL`.

## Four-semester eligibility

The admin portal now includes /semesters for independent Semester 1–4 eligibility configuration and progress. Semester 1 is initialized with at least two present attendances for non-cancelled, eligibility-enabled events plus a verified ICRC certificate. Semesters 2–4 start without criteria. Semester settings and rule changes are audited; changing criteria recalculates that semester while retaining participation and certificate records. Certificates recorded through the semester page remain pending until an administrator verifies or rejects them. Student progress is visible within the existing admin-protected student profile. An eligibility CSV is available on each semester page. Eligibility is a requirements result, not an official credit award. init_db() applies additive, idempotent schema updates for existing SQLite and PostgreSQL databases; it does not reset student, event, or attendance data.

The Eligibility Check reads certificate totals from the uploaded CSV and does not create verified semester certificate records. The semester rule engine uses attendance recorded for each semester. Student self-service sign-in is available at `/volunteer` and uses the same responsive sign-in layout as the administrator portal; student accounts are separate from administrator accounts, and each student can only view their own attendance and semester eligibility. Coordinators can issue or reset an account from a student's profile, or use **Issue all portal passwords** on the Students page to set every roster account to the shared password `KJU`. The bulk action includes inactive and removed records and revokes existing sessions. Passwords are stored as hashes; students can change their password after signing in. Failed student sign-ins are rate limited. Department leadership appointments, profiles, dates, contacts, and photos are managed from **Students → Manage Student Convenors**; uploaded photos are stored in the database and limited to verified JPG/PNG/WebP images under 5 MB. Existing legacy contact records remain available in that page's photo library. Semester configuration currently covers minimum activity count; optional requirements, category-specific activities, and department/class report filters are not yet implemented.
