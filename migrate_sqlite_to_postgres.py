"""Copy an existing local YRC SQLite database into an empty PostgreSQL database."""
import argparse
import os
import sqlite3

from app import SCHEMA  # Initializes/creates the PostgreSQL schema using DATABASE_URL.
from database import connect_db, USING_POSTGRES


TABLES = (
    'admins', 'students', 'events', 'admin_roles', 'event_participants',
    'selection_history', 'attendance', 'credits', 'login_attempts',
    'admin_audit_logs', 'participation_import_batches', 'participation_import_rows',
)
SEQUENCED_TABLES = (
    'admins', 'students', 'events', 'event_participants', 'selection_history',
    'attendance', 'credits', 'login_attempts', 'admin_audit_logs',
    'participation_import_rows',
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', default=os.getenv('DATABASE_PATH', 'yrc.db'),
                        help='SQLite database file (default: DATABASE_PATH or yrc.db)')
    args = parser.parse_args()
    if not USING_POSTGRES:
        parser.error('Set DATABASE_URL to the destination PostgreSQL connection string first.')
    if not os.path.isfile(args.source):
        parser.error(f'SQLite source file not found: {args.source}')

    source = sqlite3.connect(args.source)
    source.row_factory = sqlite3.Row
    target = connect_db()
    try:
        for table in TABLES:
            if target.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]:
                raise RuntimeError(f'Target table {table} is not empty; migration stopped without copying data.')

        source_tables = {row[0] for row in source.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        summaries = []
        for table in TABLES:
            if table not in source_tables:
                continue
            source_columns = [row[1] for row in source.execute(f'PRAGMA table_info({table})')]
            target_columns = {row[0] for row in target.execute(
                'SELECT column_name FROM information_schema.columns '
                'WHERE table_schema=current_schema() AND table_name=?', (table,)).fetchall()}
            columns = [column for column in source_columns if column in target_columns]
            if not columns:
                continue
            quoted = ','.join(columns)
            rows = source.execute(f'SELECT {quoted} FROM {table}').fetchall()
            if rows:
                marks = ','.join('?' for _ in columns)
                target.executemany(f'INSERT INTO {table} ({quoted}) VALUES ({marks})',
                                   [tuple(row[column] for column in columns) for row in rows])
            summaries.append((table, len(rows)))

        for table in SEQUENCED_TABLES:
            target.execute(
                f"SELECT setval(pg_get_serial_sequence(?, 'id'), COALESCE(MAX(id),1), MAX(id) IS NOT NULL) FROM {table}",
                (table,))
        target.commit()
        for table, count in summaries:
            print(f'{table}: {count} row(s) copied')
        print('Migration completed. Verify the Neon-backed site before removing the SQLite backup.')
    except Exception:
        target.rollback()
        raise
    finally:
        source.close()
        target.close()


if __name__ == '__main__':
    main()
