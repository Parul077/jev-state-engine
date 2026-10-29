import sqlite3

db_path = r"C:\Users\parul\.jev\repos\f55a8c6b79e66e9c\ledger.db"
conn = sqlite3.connect(db_path)
c = conn.cursor()

c.execute("SELECT name FROM sqlite_master WHERE type='table'")
print("Tables:", [r[0] for r in c.fetchall()])

c.execute("SELECT run_id, attempt_no, gate_status, probability, diff FROM attempts ORDER BY rowid DESC LIMIT 5")
rows = c.fetchall()
print(f"\nRetrieved {len(rows)} attempts:")
for run_id, att, status, prob, diff in rows:
    print(f"\n--- Run: {run_id} | Attempt: {att} | Status: {status} | Prob: {prob} ---")
    print("Diff (first 600 chars):")
    print(diff[:600] if diff else "[NO DIFF]")
