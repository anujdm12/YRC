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
- The Eligibility Check requires at least 3 attended, non-cancelled events and at least 3 certificates. Upload the certificate-count CSV; the evaluator matches registration numbers and reads only the completed certificate count column. Certificate type is ignored, and blank count cells are treated as zero. Choose a semester to check that period's attendance or All semesters for cumulative attendance.
- Events can be assigned to Semester 1–4. The Reports semester selector scopes the attendance eligibility lists, event attendance, credit entry, and student credit totals to the selected semester; All semesters remains available for cumulative reports. CSV report links preserve the selected semester. Administrators can enter nonnegative credit points for each present student-event record. Older events and attendance remain intact after the schema update; their semester is shown as Historical / unassigned until an administrator assigns one from the event page. Credit changes are written to the audit log.
- The app supports SQLite locally (`DATABASE_PATH`, default `yrc.db`) and PostgreSQL through `DATABASE_URL` (including Neon). PostgreSQL schema setup is automatic; existing SQLite records must be migrated separately.

## Important scope gaps

The portal remains an incremental Flask application using its existing SQLite/PostgreSQL adapter. Broader department/leader administration and backend department isolation, department-linked quotas, application and ranked waitlist finalization, audited attendance amendments and credit disputes, and student sign-in are outside this enhancement. Administrative actions currently use the existing `super_admin` role. Review these controls against the university's full production requirements before expanding access.

## First administrator on Render

For a new deployment, set `INITIAL_ADMIN_USERNAME` and `INITIAL_ADMIN_PASSWORD` in the Render service environment before the first start. The password must contain at least 3 characters. The app creates this account only when the database has no administrator. Remove both bootstrap variables after the account is created; never put the password in this repository.

For a persistent hosted database, create a Neon PostgreSQL project, copy its pooled connection string, and add it to Render as the `DATABASE_URL` environment variable. Keep the URL secret and include `sslmode=require` if it is not already present. The app creates its tables on first start. `DATABASE_URL` takes precedence over SQLite's `DATABASE_PATH`.

Switching to Neon does not copy records automatically. With a local SQLite backup, install requirements, set `DATABASE_URL` in your shell, make sure `INITIAL_ADMIN_USERNAME` and `INITIAL_ADMIN_PASSWORD` are unset, then run `py migrate_sqlite_to_postgres.py --source yrc.db`. The migration stops if any destination table already has rows. For data still on a live Render instance, back it up before changing the database setting; the local migration script needs a copy of that SQLite file.

For PostgreSQL, create the first administrator with `INITIAL_ADMIN_USERNAME` and `INITIAL_ADMIN_PASSWORD`; remove those bootstrap values after the first successful start. To create or reset an account later, run `flask --app app create-admin` or `flask --app app set-admin-password` with the same `DATABASE_URL`.

## Four-semester eligibility

The admin portal now includes /semesters for independent Semester 1–4 eligibility configuration and progress. Semester 1 is initialized with at least two present attendances for non-cancelled, eligibility-enabled events plus a verified ICRC certificate. Semesters 2–4 start without criteria. Semester settings and rule changes are audited; changing criteria recalculates that semester while retaining participation and certificate records. Certificates recorded through the semester page remain pending until an administrator verifies or rejects them. Student progress is visible within the existing admin-protected student profile. An eligibility CSV is available on each semester page. Eligibility is a requirements result, not an official credit award. init_db() applies additive, idempotent schema updates for existing SQLite and PostgreSQL databases; it does not reset student, event, or attendance data.

The Eligibility Check reads certificate totals from the uploaded CSV and does not create verified semester certificate records. The semester rule engine continues to use attendance and certificate submissions recorded in the semester workflow. This app has no student sign-in, so student progress is staff-visible in the protected admin portal. Semester configuration currently supports a minimum activity count, required verified certificate types, and an AND/OR operator; arbitrary criterion types and category-specific rule conditions are not yet implemented.

Semester reports now filter by academic year, department, class, eligibility, certificate state, event and student search. Lists are paginated and CSV exports use the same server-side calculation and filters. The consolidated view keeps each semester independent. New events require a semester and academic year. Reassignment of events with participation requires confirmation, records an audit entry, and recalculates affected results. Eligibility counts only present attendance at completed events marked eligible. Events without a reliable assignment remain available for administrator review; when unresolved attendance could change a result, the engine reports incomplete records. Legacy certificate verification records are copied repeatably to year-scoped certificate storage, with existing status retained.

The attached local SQLite database was inspected read-only before these changes: it contains seven events dated October 8–14, 2026, with no semester or academic-year assignments; 343 event-participant links; 320 attendance records, all marked Present; 185 prior eligibility-history rows; and no semester certificate records. Because no academic-year or term data identifies which period those seven events belong to, they are deliberately left unassigned and appear in the Events review queue. Administrators must confirm each event's semester and academic year; no existing participation or attendance rows are changed by this update.

## Certificate Evaluator and Report Maker

The protected Certificate Evaluator offers add-only submissions, reference-only updates, and explicit verification imports. CSV headers are mapped and validated before a row preview; unmatched students, duplicates, conflicts, and invalid rows are retained in import history. Applying a preview requires confirmation and runs in one transaction. Ordinary imports cannot reset verification decisions, and no student is created from an unmatched CSV row. Verification decisions are restricted to super administrators and audited.

The Report Maker includes semester eligibility, consolidated, student, event, participation, certificate, credit, defaulter, missing-record, department, class, comparison, and custom reports. Previewing and filtering use read-only eligibility calculations. CSV and Excel exports use the selected filters; print-ready HTML can be saved as PDF through the browser. Export activity is recorded separately from source records.

Semester criteria support an activity minimum, required certificate types, and an AND/OR operator. Semester 1 starts with two eligible activities AND verified ICRC. Rule changes update or disable existing rule rows instead of deleting their history. Calculated status changes are recorded in an additive audit table.
