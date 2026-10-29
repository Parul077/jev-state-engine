import sqlite3

db_path = r"C:\Users\parul\.jev\repos\f55a8c6b79e66e9c\ledger.db"
conn = sqlite3.connect(db_path)
c = conn.cursor()

c.execute("SELECT run_id, subgoal_id, attempt_no, outcome, jev_valid, jev_probability FROM attempts WHERE subgoal_id = 'sg-1' ORDER BY rowid DESC LIMIT 3")
rows = c.fetchall()
print(f"Retrieved {len(rows)} attempts for sg-1:")
for r in rows:
    print(r)
