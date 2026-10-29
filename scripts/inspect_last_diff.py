import sqlite3
import json

conn = sqlite3.connect('checkpoints.db')
cursor = conn.cursor()
cursor.execute('SELECT thread_id, checkpoint FROM checkpoints ORDER BY rowid DESC LIMIT 10')
rows = cursor.fetchall()

print(f"Total rows retrieved: {len(rows)}")
for thread_id, cp_blob in rows:
    try:
        # Checkpoint might be JSON or msgpack/pickle
        if isinstance(cp_blob, bytes):
            data = json.loads(cp_blob.decode('utf-8', errors='ignore'))
        else:
            data = json.loads(cp_blob)
        channel_values = data.get('channel_values', {})
        staged_diff = channel_values.get('staged_diff') or channel_values.get('last_diff')
        if staged_diff:
            print(f"=== Thread: {thread_id} ===")
            print("Diff:")
            print(staged_diff[:800])
            break
    except Exception as e:
        continue
