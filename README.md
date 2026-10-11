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

   For an existing administrator, use `flask --app app set-admin-password`. Passwords must be at least 12 characters. The database file currently in this workspace may contain an account created by an older version; reset it before use.

5. Start the app with `flask --app app run` and open `http://127.0.0.1:5000/login`.

For HTTPS deployments set `COOKIE_SECURE=1`, use a stable randomly generated `SECRET_KEY`, and place Flask behind a production WSGI server and TLS reverse proxy. The development server is not a production server.

## Current behavior and data safety

- All write forms require a session CSRF token. Login attempts are rate limited per username over a 15 minute window.
- Attendance creates a pending credit review record. A super-admin can verify a nonnegative award amount; attendance no longer automatically grants the event's configured points.
- The recommendation route orders active volunteers by `(previous attended events * 100) + previous selected seat offers`, applies the remaining seat capacity, and records its priority values and a random tie break.
- Deactivating a student and cancelling an event preserve linked history. Only selected participants can receive attendance statuses.
- CSV roster import validates the entire file before inserting any rows. The current roster schema retains the legacy student naming and class fields.
- The database is SQLite (`DATABASE_PATH`, default `yrc.db`). `init_db` performs basic additive setup only; it is not a versioned migration system.

## Important scope gaps

The portal still needs the full production build described in the project brief: MySQL with SQLAlchemy and versioned migrations; Flask-WTF forms; department/leader administration and backend department isolation; department-linked events and quotas; an application and ranked waitlist workflow with finalization/decline promotion; audited attendance amendments and credit disputes; event editing and full event-specific credit evidence; remaining CSV exports, pagination and filters; and automated tests. The existing account roles currently support the initial `super_admin` path only. Do not treat this build as production-ready for real university records until those controls are implemented and reviewed.

## First administrator on Render

For a new deployment, set `INITIAL_ADMIN_USERNAME` and `INITIAL_ADMIN_PASSWORD` in the Render service environment before the first start. The password must contain at least 12 characters. The app creates this account only when the database has no administrator. Remove both bootstrap variables after the account is created; never put the password in this repository.

Use a persistent Render disk for SQLite and set `DATABASE_PATH` to a path on that disk (for example, `/var/data/yrc.db`) so the account survives restarts and redeploys. To create or reset an account later, run `flask --app app create-admin` or `flask --app app set-admin-password` in the service shell.
