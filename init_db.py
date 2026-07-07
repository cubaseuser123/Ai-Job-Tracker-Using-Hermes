import sqlite3
import csv
from pathlib import Path
import os

CSV_PATH = Path(r"C:\Users\Aaryan\.gemini\antigravity-ide\brain\1651860b-da35-48cb-a6cd-fad24873953d\ai_internships.csv")
DB_PATH = Path("internships.db")

def parse_csv():
    companies = []
    if not CSV_PATH.exists():
        print(f"Error: Could not find {CSV_PATH}")
        return companies

    with open(CSV_PATH, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not row.get("Company Name"):
                continue
            companies.append(row)
    return companies

def setup_db():
    if DB_PATH.exists():
        os.remove(DB_PATH) # Drop old DB to recreate with new schema

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Create richer companies table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            tier TEXT NOT NULL,
            fit_score TEXT,
            location TEXT,
            primary_ai_domain TEXT,
            portfolio_importance TEXT,
            dsa_level TEXT,
            min_stipend TEXT,
            hiring_status TEXT,
            what_they_looking_for TEXT,
            why_good_match TEXT,
            known_hiring_pattern TEXT,
            useful_info TEXT,
            career_url TEXT,
            last_checked DATETIME
        )
    """)
    
    # Create richer opportunities table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS opportunities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER,
            role_title TEXT,
            apply_link TEXT UNIQUE,
            eligibility TEXT,
            match_reason TEXT,
            deadline TEXT,
            verified_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'pending_review',
            FOREIGN KEY(company_id) REFERENCES companies(id)
        )
    """)
    
    companies = parse_csv()
    if not companies:
        print("No companies found to seed.")
        return

    added = 0
    for c in companies:
        try:
            cursor.execute("""
                INSERT INTO companies (
                    name, tier, fit_score, location, primary_ai_domain, portfolio_importance, 
                    dsa_level, min_stipend, hiring_status, what_they_looking_for, why_good_match, 
                    known_hiring_pattern, useful_info, career_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                c.get('Company Name', '').strip(), 
                c.get('Tier', '').strip(),
                c.get('Candidate Fit Score', ''),
                c.get('Location', ''),
                c.get('Primary AI Domain', ''),
                c.get('Portfolio Importance (1-5)', ''),
                c.get('DSA Level', ''),
                c.get('Min Stipend (Estimated ₹)', ''),
                c.get('Hiring Status (2025/2026)', ''),
                c.get("What They're Looking For", ''),
                c.get("Why I'm a Good Match", ''),
                c.get("Known Hiring Pattern", ''),
                c.get("Anything Useful Before Applying", ''),
                c.get('Career URL', '')
            ))
            added += 1
        except sqlite3.IntegrityError:
            pass
            
    conn.commit()
    conn.close()
    
    print(f"Database initialized. Seeded {added} companies with full context from CSV.")

if __name__ == "__main__":
    setup_db()
