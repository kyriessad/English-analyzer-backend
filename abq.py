import sqlite3

conn = sqlite3.connect("english_analyzer.db")
cur = conn.cursor()

for table in ["users", "cards", "review_records"]:
    print(f"\n[{table} indexes]")
    cur.execute(f"PRAGMA index_list({table});")
    indexes = cur.fetchall()

    for index in indexes:
        print(index)

        index_name = index[1]
        cur.execute(f"PRAGMA index_info({index_name});")
        print("  columns:", cur.fetchall())

conn.close()