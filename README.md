# KJU YRC Admin Management Portal

A Flask admin application for the existing YRC roster, event, attendance, and credit records. It currently uses SQLite so it runs locally without a database server. The current implementation is a starter and does **not** yet meet the full specification for MySQL/SQLAlchemy, department scoped accounts, application intake, migrations, and complete reports.

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

   For an existing administrator, use `flask --app app set-admin-password`. Passwords must be at least 3 characters. The database file currently in this workspace may contain an account created by an older version; reset it before use.

5. Start the app with `flask --app app run` and open `http://127.0.0.1:5000/login`.

For HTTPS deployments set `COOKIE_SECURE=1`, use a stable randomly generated `SECRET_KEY`, and place Flask behind a production WSGI server and TLS reverse proxy. The development server is not a production server.

## Current behavior and data safety

- All write forms require a session CSRF token. Login attempts are rate limited per username over a 15 minute window.
- Attendance creates a pending credit review record. A super-admin can verify a nonnegative award amount; attendance no longer automatically grants the event's configured points.
- The recommendation route orders active volunteers by `(previous attended events * 100) + previous selected seat offers`, applies the remaining seat capacity, and records its priority values and a random tie break.
- Deactivating a student and cancelling an event preserve linked history. Only selected participants can receive attendance statuses.
- CSV roster import validates the entire file before inserting any rows. The current roster schema retains the legacy student naming and class fields.
- Attendance has a reviewed Google Sheets CSV import. It uses the latest submission per register number, matches by register number first and falls back to a unique exact full name, combines Ethnic Day labels, and skips attendance already marked Present. Duplicate names remain unmatched for review. New events require an administrator-entered date; response timestamps are not treated as event dates. Preview details are removed after confirmation, while an import summary is retained.
- The Eligibility Check requires at least 3 attended, non-cancelled events and at least 3 certificate PDFs. Choose a semester to check only that semester, or All semesters for cumulative results; use a matching public Drive folder for semester-specific checks. PDF filenames must contain the student's exact roll number as a separate filename token (for example, `26BCAC63_First_Aid.pdf`). The app reads Drive file names only, does not download or OCR certificates, and does not retain the folder link or scan results. Configure a Google Drive API key in `GOOGLE_DRIVE_API_KEY` on the server, enable the Drive API, and restrict the key to the Drive API before use.
- Events can be assigned to Semester 1–4. The Reports semester selector scopes the attendance eligibility lists, event attendance, credit entry, and student credit totals to the selected semester; All semesters remains available for cumulative reports. CSV report links preserve the selected semester. Administrators can enter nonnegative credit points for each present student-event record. Older events and attendance remain intact after the schema update; their semester is shown as Historical / unassigned until an administrator assigns one from the event page. Credit changes are written to the audit log.
- The app supports SQLite locally (`DATABASE_PATH`, default `yrc.db`) and PostgreSQL through `DATABASE_URL` (including Neon). PostgreSQL schema setup is automatic; existing SQLite records must be migrated separately.

## Important scope gaps

The portal still needs the full production build described in the project brief: MySQL with SQLAlchemy and versioned migrations; Flask-WTF forms; department/leader administration and backend department isolation; department-linked events and quotas; an application and ranked waitlist workflow with finalization/decline promotion; audited attendance amendments and credit disputes; event editing and full event-specific credit evidence; remaining CSV exports, pagination and filters; and automated tests. The existing account roles currently support the initial `super_admin` path only. Do not treat this build as production-ready for real university records until those controls are implemented and reviewed.

## First administrator on Render

For a new deployment, set `INITIAL_ADMIN_USERNAME` and `INITIAL_ADMIN_PASSWORD` in the Render service environment before the first start. The password must contain at least 3 characters. The app creates this account only when the database has no administrator. Remove both bootstrap variables after the account is created; never put the password in this repository.

For a persistent hosted database, create a Neon PostgreSQL project, copy its pooled connection string, and add it to Render as the `DATABASE_URL` environment variable. Keep the URL secret and include `sslmode=require` if it is not already present. The app creates its tables on first start. `DATABASE_URL` takes precedence over SQLite's `DATABASE_PATH`.

Switching to Neon does not copy records automatically. With a local SQLite backup, install requirements, set `DATABASE_URL` in your shell, make sure `INITIAL_ADMIN_USERNAME` and `INITIAL_ADMIN_PASSWORD` are unset, then run `py migrate_sqlite_to_postgres.py --source yrc.db`. The migration stops if any destination table already has rows. For data still on a live Render instance, back it up before changing the database setting; the local migration script needs a copy of that SQLite file.

For PostgreSQL, create the first administrator with `INITIAL_ADMIN_USERNAME` and `INITIAL_ADMIN_PASSWORD`; remove those bootstrap values after the first successful start. To create or reset an account later, run `flask --app app create-admin` or `flask --app app set-admin-password` with the same `DATABASE_URL`.

## Four-semester eligibility

The admin portal now includes /semesters for independent Semester 1–4 eligibility configuration and progress. Semester 1 is initialized with at least two present attendances for non-cancelled, eligibility-enabled events plus a verified ICRC certificate. Semesters 2–4 start without criteria. Semester settings and rule changes are audited; changing criteria recalculates that semester while retaining participation and certificate records. Certificates recorded through the semester page remain pending until an administrator verifies or rejects them. Student progress is visible within the existing admin-protected student profile. An eligibility CSV is available on each semester page. Eligibility is a requirements result, not an official credit award. init_db() applies additive, idempotent schema updates for existing SQLite and PostgreSQL databases; it does not reset student, event, or attendance data.

The existing Drive-folder Eligibility Check remains the legacy three-event/three-PDF filename scan. The semester rule engine uses attendance and certificate submissions recorded in the semester workflow; the legacy scan does not create verified semester certificate records. This app has no student sign-in, so student progress is currently staff-visible in the protected admin portal rather than a self-service student account. Semester configuration currently covers minimum activity count and mandatory verified certificate types; optional requirements, category-specific rule conditions, and student authentication are not yet implemented.

Semester reports now filter by academic year, department, class, eligibility, certificate state, event and student search. Lists are paginated and CSV exports use the same server-side calculation and filters. The optional consolidated view keeps each semester independent. New events require a semester and academic year. Reassignment of events with participation requires confirmation, records an audit entry, and recalculates affected results. Eligibility counts only present attendance at completed events marked eligible. Legacy certificate verification records are copied repeatably to year-scoped certificate storage, with existing status retained.

The attached local SQLite database was inspected read-only before these changes: it contains seven events dated October 8–14, 2026, with no semester or academic-year assignments; 343 event-participant links; 320 attendance records, all marked Present; 185 prior eligibility-history rows; and no semester certificate records. Because no academic-year or term data identifies which period those seven events belong to, they are deliberately left unassigned and appear in the Events review queue. Administrators must confirm each event's semester and academic year; no existing participation or attendance rows are changed by this update.
