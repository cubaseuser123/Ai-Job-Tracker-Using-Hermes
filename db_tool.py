import sqlite3
import argparse
import sys
from pathlib import Path
from datetime import datetime

DB_PATH = Path("internships.db")

def ensure_columns(cursor):
    """Idempotently add any new columns to existing tables."""
    new_opportunity_cols = {
        "location": "TEXT",
        "ppo_potential": "TEXT",
        "source": "TEXT",
        "stipend": "TEXT",
        "dsa_level": "TEXT",
        "edge_notes": "TEXT",
    }
    for col, col_type in new_opportunity_cols.items():
        try:
            cursor.execute(f"ALTER TABLE opportunities ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass  # Column already exists


def log_opening(args):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    ensure_columns(cursor)

    # Find company by exact name or fuzzy match
    cursor.execute("SELECT id FROM companies WHERE name = ?", (args.company,))
    row = cursor.fetchone()
    if not row:
        # Try fuzzy match
        cursor.execute("SELECT id, name FROM companies WHERE name LIKE ?", (f"%{args.company}%",))
        row = cursor.fetchone()
        if row:
            print(f"  Fuzzy-matched '{args.company}' to '{row[1]}'")
        else:
            print(f"  Company '{args.company}' not in DB. Adding as newly discovered.")
            cursor.execute(
                "INSERT INTO companies (name, tier, location, career_url) VALUES (?, 'Discovered', ?, ?)",
                (args.company, args.location or "Unknown", args.link or ""),
            )
            conn.commit()
            row = (cursor.lastrowid,)

    company_id = row[0]

    try:
        cursor.execute("""
            INSERT INTO opportunities 
                (company_id, role_title, apply_link, eligibility, match_reason, 
                 location, ppo_potential, source, stipend, dsa_level, edge_notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            company_id,
            args.role,
            args.link,
            args.eligibility,
            args.match_reason,
            args.location,
            args.ppo_potential,
            args.source,
            args.stipend,
            args.dsa_level,
            args.edge_notes,
        ))
        conn.commit()
        print(f"✅ Logged: {args.company} — {args.role} ({args.location}) via {args.source}")
    except sqlite3.IntegrityError:
        print(f"⚠️  Duplicate: {args.link} already logged.")
    finally:
        # Update last_checked timestamp on the company
        cursor.execute(
            "UPDATE companies SET last_checked = ? WHERE id = ?",
            (datetime.now().isoformat(), company_id),
        )
        conn.commit()
        conn.close()


def add_company(args):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    try:
        cursor.execute("""
            INSERT INTO companies (name, tier, location, primary_ai_domain, career_url)
            VALUES (?, 'Discovered', ?, ?, ?)
        """, (args.name, args.location, args.domain, args.career_url))
        conn.commit()
        print(f"✅ Added new company: {args.name} ({args.location})")
    except sqlite3.IntegrityError:
        print(f"⚠️  Company '{args.name}' already exists in database.")
    finally:
        conn.close()


def show_report(args):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT o.role_title, c.name as company, o.location, o.source, 
               o.apply_link, o.ppo_potential, o.dsa_level, o.edge_notes, o.verified_at
        FROM opportunities o
        JOIN companies c ON o.company_id = c.id
        ORDER BY o.verified_at DESC
    """)
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        print("No openings logged yet.")
        return

    print(f"\n{'='*80}")
    print(f"  HERMES DAILY REPORT — {len(rows)} Opening(s) Found")
    print(f"{'='*80}\n")

    for i, r in enumerate(rows, 1):
        print(f"  [{i}] {r['role_title']} @ {r['company']}")
        print(f"      📍 {r['location'] or 'N/A'}  |  🔗 {r['source'] or 'careers page'}")
        print(f"      Apply: {r['apply_link']}")
        if r['ppo_potential']:
            print(f"      💰 PPO: {r['ppo_potential']}")
        if r['dsa_level']:
            print(f"      📊 DSA: {r['dsa_level']}")
        if r['edge_notes']:
            print(f"      🤝 Edge: {r['edge_notes']}")
        print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Hermes Database Tool")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- log-opening ---
    log_parser = subparsers.add_parser("log-opening", help="Log a verified internship opening")
    log_parser.add_argument("--company", required=True)
    log_parser.add_argument("--role", required=True)
    log_parser.add_argument("--link", required=True)
    log_parser.add_argument("--source", default="careers page", help="Where the listing was found")
    log_parser.add_argument("--eligibility", default="")
    log_parser.add_argument("--match-reason", default="")
    log_parser.add_argument("--location", default="")
    log_parser.add_argument("--stipend", default="")
    log_parser.add_argument("--ppo-potential", default="")
    log_parser.add_argument("--dsa-level", default="")
    log_parser.add_argument("--edge-notes", default="", help="Insider intel: hiring manager, referral path, email, timing")

    # --- add-company ---
    add_parser = subparsers.add_parser("add-company", help="Add a newly discovered company")
    add_parser.add_argument("--name", required=True)
    add_parser.add_argument("--location", default="Unknown")
    add_parser.add_argument("--domain", default="AI Engineering")
    add_parser.add_argument("--career-url", default="")

    # --- report ---
    subparsers.add_parser("report", help="Print a formatted report of all findings")

    args = parser.parse_args()

    if args.command == "log-opening":
        log_opening(args)
    elif args.command == "add-company":
        add_company(args)
    elif args.command == "report":
        show_report(args)
