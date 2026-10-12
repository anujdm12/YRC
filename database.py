"""Small SQLite/PostgreSQL compatibility layer for the YRC app."""
import os
import re
import sqlite3


DATABASE_URL = os.getenv('DATABASE_URL', '').strip()
USING_POSTGRES = bool(DATABASE_URL)

try:
    import psycopg
except ImportError:  # PostgreSQL is optional for local SQLite development.
    psycopg = None


DB_INTEGRITY_ERROR = (sqlite3.IntegrityError,)
DB_ERROR = (sqlite3.Error,)
if psycopg:
    DB_INTEGRITY_ERROR += (psycopg.IntegrityError,)
    DB_ERROR += (psycopg.Error,)
DB_IMPORT_ERROR = DB_ERROR + (KeyError, ValueError)


class HybridRow(dict):
    """Dict-like PostgreSQL row that also supports SQLite's integer indexing."""
    def __init__(self, values, columns):
        super().__init__(zip(columns, values))
        self._columns = columns

    def __getitem__(self, key):
        if isinstance(key, int):
            key = self._columns[key]
        return super().__getitem__(key)


class CursorProxy:
    def __init__(self, cursor, lastrowid=None):
        self._cursor = cursor
        self.lastrowid = lastrowid

    @property
    def rowcount(self):
        return self._cursor.rowcount

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def __iter__(self):
        return iter(self._cursor)


class PostgresConnection:
    _RETURNING_TABLES = {'admins', 'students', 'events'}

    def __init__(self, connection):
        self._connection = connection

    @staticmethod
    def _postgres_schema(schema):
        schema = re.sub(r'\bid INTEGER PRIMARY KEY\b', 'id BIGSERIAL PRIMARY KEY', schema)
        schema = re.sub(r'\bINTEGER\b', 'BIGINT', schema)
        schema = re.sub(r'\bREAL\b', 'DOUBLE PRECISION', schema)
        return schema

    def execute(self, sql, parameters=()):
        raw_sql = sql.strip()
        pragma = re.fullmatch(r'PRAGMA\s+table_info\((\w+)\)', raw_sql, re.IGNORECASE)
        if pragma:
            table = pragma.group(1)
            cursor = self._connection.execute(
                "SELECT (ordinal_position - 1) AS cid, column_name AS name, data_type AS type, "
                '0 AS "notnull", column_default AS dflt_value, 0 AS pk '
                "FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=%s "
                "ORDER BY ordinal_position", (table,))
            return CursorProxy(cursor)

        sql = raw_sql
        if sql.upper() == 'BEGIN IMMEDIATE':
            sql = 'BEGIN'
        if 'datetime("now","-1 day")' in sql:
            sql = sql.replace('datetime("now","-1 day")', "(CURRENT_TIMESTAMP - INTERVAL '1 day')")
        sql = re.sub(r'"([A-Za-z][A-Za-z0-9 _-]*)"', r"'\1'", sql)
        sql = re.sub(r'\bLIKE\b', 'ILIKE', sql, flags=re.IGNORECASE)
        sql = re.sub(r'\bINSERT\s+OR\s+IGNORE\s+INTO\b', 'INSERT INTO', sql, flags=re.IGNORECASE)
        if raw_sql.upper().startswith('INSERT OR IGNORE') and 'ON CONFLICT' not in sql.upper():
            sql += ' ON CONFLICT DO NOTHING'
        sql = sql.replace('?', '%s')

        returning_id = False
        insert = re.match(r'INSERT\s+INTO\s+(\w+)', sql, re.IGNORECASE)
        if insert and insert.group(1).lower() in self._RETURNING_TABLES and 'RETURNING' not in sql.upper():
            sql += ' RETURNING id'
            returning_id = True
        cursor = self._connection.execute(sql, parameters or ())
        lastrowid = None
        if returning_id:
            returned = cursor.fetchone()
            lastrowid = returned['id'] if returned else None
        return CursorProxy(cursor, lastrowid)

    def executemany(self, sql, parameter_rows):
        sql = sql.strip().replace('?', '%s')
        return CursorProxy(self._connection.cursor().executemany(sql, parameter_rows))

    def executescript(self, schema):
        for statement in self._postgres_schema(schema).split(';'):
            if statement.strip():
                self.execute(statement)

    def commit(self):
        self._connection.commit()

    def rollback(self):
        self._connection.rollback()

    def close(self):
        self._connection.close()


def connect_db(path=None):
    if DATABASE_URL:
        if psycopg is None:
            raise RuntimeError('Install psycopg[binary] to use DATABASE_URL with PostgreSQL.')
        dsn = DATABASE_URL
        if dsn.startswith('postgres://'):
            dsn = 'postgresql://' + dsn[len('postgres://'):]
        connection = psycopg.connect(dsn, row_factory=_hybrid_row_factory)
        return PostgresConnection(connection)
    connection = sqlite3.connect(path or os.getenv('DATABASE_PATH', 'yrc.db'))
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys = ON')
    return connection


def _hybrid_row_factory(cursor):
    columns = [column.name for column in cursor.description] if cursor.description else []
    return lambda values: HybridRow(values, columns)
