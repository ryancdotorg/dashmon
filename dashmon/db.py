__all__ = ['Database']

import logging
logger = logging.getLogger(__name__)

import os
import json
import sqlite3
import time
import datetime
import dateutil.parser

from contextlib import contextmanager
from collections.abc import Iterable

sqlite3.register_adapter('json', json.dumps)
sqlite3.register_converter('json', json.loads)

# iteratively flatten irregular list - works on iterables
def flatten(*args):
    paths, stack = [], [iter(args)]
    while len(stack):
        try:
            x = next(stack[-1])
            if isinstance(x, Iterable) and not isinstance(x, (str, bytes)):
                stack.append(iter(x))
            else:
                paths.append(x)
        except StopIteration:
            stack.pop()

    return paths

# order preserving unique, takes any iterable, returns a list
def uniq(itr):
    used = set()
    u = [x for x in itr if x not in used and (used.add(x) or True)]
    return u

def prog():
    import inspect
    for z in inspect.stack():
        prog = inspect.getmodule(z[0]).__file__ or prog

    return prog

SUBPATH = ['dashmon.sqlite']
DEFAULT_DB = lambda: uniq(map(lambda *p: os.path.abspath(os.path.join(*flatten(*p, SUBPATH))), [
    [os.path.dirname(os.path.realpath(prog())), 'db'],
    os.path.dirname(os.path.realpath(prog())),
    [os.getcwd(), 'db'],
    os.getcwd(),
    [os.sep, 'srv', 'db'],
]))

def parse_datetime(s):
    return int(dateutil.parser.parse(s).timestamp())

def map_factory(cursor, row):
    r = sqlite3.Row(cursor, row)
    return Map(dict(zip(r.keys(), tuple(r))))

def row_to_map(row):
    d = dict(zip(row.keys(), tuple(row)))
    return Map(d)

# https://stackoverflow.com/a/32107024
class Map(dict):
    def __init__(self, *args, **kwargs):
        super(Map, self).__init__(*args, **kwargs)
        for arg in args:
            if isinstance(arg, dict):
                for k, v in arg.items():
                    self[k] = v

        if kwargs:
            for k, v in kwargs.items():
                self[k] = v

    def __getattr__(self, attr):
        return self.get(attr)

    def __setattr__(self, key, value):
        self.__setitem__(key, value)

    def __setitem__(self, key, value):
        super(Map, self).__setitem__(key, value)
        self.__dict__.update({key: value})

    def __delattr__(self, item):
        self.__delitem__(item)

    def __delitem__(self, key):
        super(Map, self).__delitem__(key)
        del self.__dict__[key]

class Database:
    def __init__(self, logger=logger, filename=None):
        self._db_filename = None
        self._db_inode = None
        self.logger = logger
        self.open(filename)

    def open(self, filename=None):
        if filename is None and self._db_filename:
            filename = self._db_filename

        if filename is None:
            self.logger.info('No filename specified, searching defaults...')
            for loc in DEFAULT_DB():
                self.logger.debug(f'Looking for database at `{loc}`...')
                if os.path.isfile(loc):
                    if os.access(loc, os.R_OK|os.W_OK):
                        self.logger.info(f'Using database at `{loc}`.')
                        filename = loc
                        break
                    else:
                        self.logger.warning(f'Found database at `{loc}` but cannot access!')
        elif filename == self._db_filename:
            stat = os.stat(self._db_filename)
            if stat.st_ino == self._db_inode: return
            self.logger.info('Database replaced. Reopening.')

        if filename is None:
            self.logger.error('Could not find database!')

        self.con = sqlite3.connect(
            filename, isolation_level=None,
            detect_types=sqlite3.PARSE_DECLTYPES|sqlite3.PARSE_COLNAMES
        )
        cur = self.con.cursor()
        cur.execute('PRAGMA journal_mode=wal')
        stat = os.stat(filename)
        self._db_filename = filename
        self._db_inode = stat.st_ino

    def close(self):
        self._db_inode = None
        self.con.close()

    def _cursor(self):
        cur = self.con.cursor()
        cur.row_factory = map_factory
        return cur

    @contextmanager
    def trxn(self):
        self.con.execute('BEGIN')
        try: yield self.con
        except:
            self.con.rollback()
            raise
        else: self.con.commit()

    def log_press(self, pr):
        with self.trxn() as con:
            cur = self._cursor()
            row = cur.execute(
                'SELECT id FROM buttons WHERE mac=?',
                (pr.mac,)
            ).fetchone()

            if row is not None:
                button_id = row.id
            else:
                button_id = cur.execute(
                    'INSERT INTO buttons (mac) VALUES (?)',
                    (pr.mac,)
                ).lastrowid

            cur.execute(
                'INSERT INTO events (button_id, data) VALUES(?, JSON(?))',
                (button_id, pr.to_json())
            )

    def get_actions(self, pr):
        cur = self._cursor()
        row = cur.execute(
                'SELECT actions as "actions [json]" FROM buttons WHERE mac=?',
                (pr.mac,)
        ).fetchone()

        return row.actions if row is not None else None

    def get_recent(self):
        recent = {}

        cur = self._cursor()
        cur.execute('''
            SELECT
                ts as "ts [timestamp]",
                button_id,
                label,
                data as "data [json]"
            FROM events
            INNER JOIN buttons
                ON buttons.id = events.button_id
            WHERE ts > DATETIME(CURRENT_TIMESTAMP, "-7 days")
            ORDER BY ts DESC
        ''')

        for row in cur:
            data = Map(row.data)
            data.ts = row.ts
            label = row.label or data.ident
            recent.setdefault(label, []).append(data)

        return recent
