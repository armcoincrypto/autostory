import os, sys, sqlite3
_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.abspath(os.path.join(_here, ".."))
sys.path.insert(0, _root)
from config.settings import settings
DB_PATH = settings.database.url.replace("sqlite:///", "")
if not os.path.isabs(DB_PATH):
    DB_PATH = os.path.join(_root, DB_PATH)
def column_exists(cur, table, column):
    cur.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())
MIGRATIONS = [
    ("scheduled_jobs", "lease_until", "DATETIME"),
    ("scheduled_jobs", "lease_owner", "VARCHAR(128)"),
    ("accounts", "health_status",             "TEXT"),
    ("accounts", "health_reason",             "TEXT"),
    ("accounts", "health_checked_at",         "DATETIME"),
    ("accounts", "purpose",                   "TEXT DEFAULT 'both'"),
    ("accounts", "story_precheck_status",     "TEXT"),
    ("accounts", "story_precheck_checked_at", "DATETIME"),
]
con = sqlite3.connect(DB_PATH)
cur = con.cursor()
print(f"DB: {DB_PATH}")
for table, col, defn in MIGRATIONS:
    if column_exists(cur, table, col):
        print(f"  [skip]  {table}.{col}")
    else:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {defn}")
        print(f"  [added] {table}.{col}")
con.commit()
con.close()
print("Done.")
