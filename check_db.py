import sqlite3

conn = sqlite3.connect("internships.db")
c = conn.cursor()

print("=== COMPANIES TABLE SCHEMA ===")
for r in c.execute("PRAGMA table_info(companies)").fetchall():
    print(f"  {r}")

count = c.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
print(f"\nTotal companies: {count}")

print("\n=== SAMPLE COMPANIES (with PPO/machine test data) ===")
conn.row_factory = sqlite3.Row
c2 = conn.cursor()
rows = c2.execute("SELECT name, tier, ppo_salary_est, machine_test, dsa_level, location FROM companies LIMIT 8").fetchall()
for r in rows:
    print(f"  {dict(r)}")
