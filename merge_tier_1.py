import sqlite3
import re
from pathlib import Path

DB_PATH = Path("internships.db")
MD_PATH = Path(r"C:\Users\Aaryan\.gemini\antigravity-ide\brain\b70ea13d-3d59-4514-aa94-f40fe06f6d75\tier_1_targets.md")

def add_tier_1_targets():
    if not MD_PATH.exists():
        print("Tier 1 MD not found.")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Add new columns to companies if they don't exist
    try:
        cursor.execute("ALTER TABLE companies ADD COLUMN ppo_salary_est TEXT")
        cursor.execute("ALTER TABLE companies ADD COLUMN machine_test TEXT")
        cursor.execute("ALTER TABLE companies ADD COLUMN apply_window TEXT")
        cursor.execute("ALTER TABLE companies ADD COLUMN portal TEXT")
    except sqlite3.OperationalError:
        pass # Columns already exist

    with open(MD_PATH, "r", encoding="utf-8") as f:
        content = f.read()

    # Find the table rows
    in_table = False
    added = 0
    updated = 0
    for line in content.split("\n"):
        if line.startswith("|---"):
            in_table = True
            continue
        if in_table and line.startswith("|"):
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 10 and parts[1].isdigit():
                company_name = parts[2]
                location = parts[3]
                domain = parts[4]
                portal = parts[5]
                apply_window = parts[6]
                ppo_est = parts[7]
                dsa_bar = parts[8]
                machine_test = parts[9]
                
                # Check if company exists
                cursor.execute("SELECT id FROM companies WHERE name LIKE ?", (f"%{company_name}%",))
                row = cursor.fetchone()
                if row:
                    cursor.execute("""
                        UPDATE companies SET 
                        ppo_salary_est = ?, machine_test = ?, apply_window = ?, portal = ?
                        WHERE id = ?
                    """, (ppo_est, machine_test, apply_window, portal, row[0]))
                    updated += 1
                else:
                    cursor.execute("""
                        INSERT INTO companies (name, tier, location, primary_ai_domain, portal, apply_window, ppo_salary_est, dsa_level, machine_test)
                        VALUES (?, 'Tier 1', ?, ?, ?, ?, ?, ?, ?)
                    """, (company_name, location, domain, portal, apply_window, ppo_est, dsa_bar, machine_test))
                    added += 1
        elif in_table and not line.strip():
            in_table = False
            
    conn.commit()
    conn.close()
    print(f"Tier 1 Targets parsed: Added {added} new companies, updated {updated} existing companies.")

if __name__ == "__main__":
    add_tier_1_targets()
