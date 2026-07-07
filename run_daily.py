"""
Hermes Daily Runner — The Waterfall Hybrid
=========================================
Executes the full 3-phase internship hunt using a hybrid ingestion strategy:
  Search: DDGS (Free/Fast) -> Tavily (Paid/Reliable)
  Scrape: Jina Reader (Free/Fast) -> Firecrawl (Paid/Heavy JS)

Usage:
  python run_daily.py              # Full run
  python run_daily.py --phase 1    # Only seeded sweep
  python run_daily.py --report     # Just print today's findings
"""

import sqlite3
import os
import json
import time
import argparse
import re
from pathlib import Path
from datetime import datetime
import requests
from dotenv import load_dotenv
import threading
import concurrent.futures

load_dotenv()

DB_PATH = Path("internships.db")
SKILL_PATH = Path("hermes_skill.md")

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY", "")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")

MIN_OPENINGS_THRESHOLD = 10

gemma_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def get_companies():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM companies ORDER BY tier ASC")
    companies = cursor.fetchall()
    conn.close()
    return companies

def update_last_checked(company_name):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE companies SET last_checked = ? WHERE name = ?",
        (datetime.now().isoformat(), company_name)
    )
    conn.commit()
    conn.close()

# ---------------------------------------------------------------------------
# The Waterfall: Search & Fetch
# ---------------------------------------------------------------------------

def smart_search(query, max_results=5):
    """
    Tier 1: DuckDuckGo Search (Free, Fast)
    Tier 2: Tavily (Fallback for complex queries)
    """
    try:
        from ddgs import DDGS
        print(f"  [Search] Trying DuckDuckGo for: '{query}'")
        results = DDGS().text(query, max_results=max_results)
        normalized = []
        for r in results:
            url = r.get("href") or r.get("url", "")
            title = r.get("title", "")
            content = r.get("body", "")
            if url:
                normalized.append({"url": url, "title": title, "content": content})
        if normalized:
            return normalized
    except Exception as e:
        print(f"  [Search] DuckDuckGo failed: {e}")

    # Fallback to Tavily
    if TAVILY_API_KEY:
        print(f"  [Search] Falling back to Tavily for: '{query}'")
        try:
            from tavily import TavilyClient
            client = TavilyClient(api_key=TAVILY_API_KEY)
            response = client.search(query=query, max_results=max_results)
            return response.get("results", [])
        except Exception as e:
            print(f"  [Search] Tavily failed: {e}")
    else:
        print("  [Search] Tavily skipped (no API key).")
        
    return []


def smart_fetch(url):
    """
    Tier 1: Jina Reader (Free, Fast, Zero local compute)
    Tier 2: Firecrawl (Paid, Fallback)
    """
    print(f"  [Fetch] Using Jina Reader for: {url}")
    try:
        response = requests.get(f"https://r.jina.ai/{url}", timeout=30)
        if response.status_code == 200:
            time.sleep(2)  # Respect Jina's free rate limits
            if len(response.text) > 100:  # Ensure it actually scraped something
                return response.text
            else:
                print(f"  [Fetch] Jina returned empty content. Falling back...")
        else:
            print(f"  [Fetch] Jina returned {response.status_code}. Falling back...")
    except Exception as e:
        print(f"  [Fetch] Jina failed: {e}. Falling back...")

    # Fallback -> Firecrawl
    if FIRECRAWL_API_KEY:
        print(f"  [Fetch] Using Firecrawl for: {url}")
        try:
            response = requests.post(
                "https://api.firecrawl.dev/v1/scrape",
                headers={"Authorization": f"Bearer {FIRECRAWL_API_KEY}", "Content-Type": "application/json"},
                json={"url": url, "formats": ["markdown"]},
                timeout=60
            )
            if response.status_code == 200:
                data = response.json().get("data", {})
                return data.get("markdown", "")
            else:
                print(f"  [Fetch] Firecrawl returned status {response.status_code}: {response.text}")
        except Exception as e:
            print(f"  [Fetch] Firecrawl failed for {url}: {e}")
    else:
        print("  [Fetch] Firecrawl skipped (no API key).")

    return None

def has_internship_signals(content):
    """Quick keyword check before sending to LLM."""
    signals = [
        "intern", "entry-level", "entry level", "fresher",
        "ai engineer", "machine learning", "llm", "generative ai",
        "genai", "rag", "agent", "nlp", "python", "software engineer"
    ]
    content_lower = content.lower()
    return any(s in content_lower for s in signals)

# ---------------------------------------------------------------------------
# Multi-portal URL discovery for a single company
# ---------------------------------------------------------------------------

def safe_get(row, key, default=""):
    try:
        val = row[key]
        return val if val is not None else default
    except (IndexError, KeyError):
        return default

def discover_urls_for_company(company):
    urls = []
    name = company["name"]

    career_url = safe_get(company, "career_url")
    if career_url.startswith("http"):
        urls.append(("careers page", career_url))

    portal = safe_get(company, "portal")
    if portal and "http" in portal:
        urls.append(("portal", portal))

    search_queries = [
        f'"{name}" intern AI engineer site:linkedin.com/jobs',
        f'"{name}" intern site:wellfound.com',
        f'"{name}" intern site:boards.greenhouse.io OR site:jobs.lever.co',
    ]

    for query in search_queries:
        results = smart_search(query, max_results=2)
        for r in results:
            url = r.get("url", "")
            if url and url not in [u[1] for u in urls]:
                source = "web search"
                if "linkedin.com" in url: source = "LinkedIn"
                elif "wellfound.com" in url: source = "Wellfound"
                elif "greenhouse.io" in url: source = "Greenhouse"
                elif "lever.co" in url: source = "Lever"
                elif "ashbyhq.com" in url: source = "Ashby"
                elif "internshala.com" in url: source = "Internshala"
                urls.append((source, url))

    return urls

# ---------------------------------------------------------------------------
# LLM Extraction via Gemma
# ---------------------------------------------------------------------------

def load_skill_prompt():
    with open(SKILL_PATH, "r", encoding="utf-8") as f:
        return f.read()

def extract_with_gemma(content, company, url, source, skill_prompt):
    name = company["name"] if type(company) != str else company
    print(f"  [{name}] Extracting via Gemma 3 ({source})...")

    company_context = "No prior intel available."
    if type(company) != str:
        ctx_parts = []
        for field, label in [
            ("what_they_looking_for", "What they look for"),
            ("why_good_match", "Why candidate is a good match"),
            ("known_hiring_pattern", "Hiring pattern"),
            ("useful_info", "Tactical tip"),
            ("location", "Location"),
            ("ppo_salary_est", "PPO salary estimate"),
            ("dsa_level", "DSA level"),
            ("machine_test", "Machine test"),
        ]:
            val = None
            try:
                val = company[field]
            except (IndexError, KeyError):
                pass
            if val:
                ctx_parts.append(f"- {label}: {val}")
        if ctx_parts:
            company_context = "\n".join(ctx_parts)

    prompt = f"""{skill_prompt}

### 🏢 Company Context: {name}
{company_context}

### 📄 Page Content (from {source}: {url})
---
{content[:55000]}
---

**Instructions:** Analyze this page for internship openings matching the Candidate Profile. 
Return ONLY a valid JSON object with these keys:
- "found": boolean
- "role_title": string
- "company_name": "{name}"
- "location": string
- "source": "{source}"
- "direct_apply_link": string
- "deadline": string or null
- "eligibility": string
- "stipend": string or "Not listed"
- "ppo_potential": string or null
- "match_reason": string
- "dsa_level": "None" | "Light" | "Medium" | "Heavy"
- "edge_notes": string

If no valid matching opening exists, set "found" to false and leave other fields empty.
"""

    payload = {
        "model": "gemma3:4b",
        "prompt": prompt,
        "stream": False,
        "format": "json",
    }

    try:
        with gemma_lock:
            response = requests.post(OLLAMA_URL, json=payload, timeout=120)
            response.raise_for_status()
            raw = response.json().get("response", "").strip()
        
        # Clean markdown codeblock formatting if Gemma hallucinated it
        if raw.startswith("```json"):
            raw = raw[7:]
        elif raw.startswith("```"):
            raw = raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()
        
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # Fallback robust regex extraction for {} blocks
            match = re.search(r'\{.*\}', raw, re.DOTALL)
            if match:
                data = json.loads(match.group(0))
            else:
                raise ValueError(f"Could not parse JSON: {raw[:100]}")
                
        return data
    except Exception as e:
        print(f"  [{name}] Gemma extraction failed: {e}")
        return None

# ---------------------------------------------------------------------------
# Phase 1: Seeded Company Sweep
# ---------------------------------------------------------------------------

def phase_1(skill_prompt):
    print("\n" + "=" * 60)
    print("  PHASE 1: Seeded Company Sweep")
    print("=" * 60)

    companies = get_companies()
    openings_found = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(_process_company, c, skill_prompt) for c in companies]
        for future in concurrent.futures.as_completed(futures):
            try:
                openings_found += future.result()
            except Exception as e:
                print(f"  ❌ Error processing company: {e}")

    return openings_found

def _process_company(c, skill_prompt):
    name = c["name"]
    print(f"\n🔍 [{name}] (Tier: {c['tier']})")
    
    openings_found = 0
    urls = discover_urls_for_company(c)
    if not urls:
        print(f"  No URLs found for {name}.")
        update_last_checked(name)
        return openings_found

    seen_roles = set()

    for source, url in urls:
        content = smart_fetch(url)
        if not content:
            continue
        if not has_internship_signals(content):
            print(f"  No internship signals on {source}.")
            continue

        data = extract_with_gemma(content, c, url, source, skill_prompt)
        if data and data.get("found"):
            role = data.get("role_title", "Unknown Role")
            dedup_key = f"{name}|{role}"
            if dedup_key in seen_roles:
                print(f"  ♻️  Duplicate: {role}")
                continue
            seen_roles.add(dedup_key)

            print(f"  ✅ FOUND: {role}")
            _log_via_cli(data)
            openings_found += 1
        else:
            print(f"  No matching openings on {source}.")

    update_last_checked(name)
    return openings_found

# ---------------------------------------------------------------------------
# Phase 2: Autonomous Discovery (Wild Hunt)
# ---------------------------------------------------------------------------

DISCOVERY_QUERIES = [
    '"AI engineering intern" India 2026 hiring now',
    '"agentic AI" intern Mumbai OR Pune OR Remote',
    '"LLM" OR "RAG" intern India',
    '"AI intern" startup India 2026',
]

def phase_2(skill_prompt):
    print("\n" + "=" * 60)
    print("  PHASE 2: Autonomous Discovery (Wild Hunt)")
    print("=" * 60)

    existing = {c["name"].lower() for c in get_companies()}
    discovered = 0

    for query in DISCOVERY_QUERIES:
        print(f"\n🌐 Searching: {query}")
        results = smart_search(query, max_results=5)

        for r in results:
            url = r.get("url", "")
            title = r.get("title", "")
            snippet = r.get("content", "")

            if not any(kw in (title + snippet).lower() for kw in ["intern", "ai", "llm", "engineer"]):
                continue

            pseudo_name = title.split(" - ")[0].split(" | ")[0].strip()[:50]
            if pseudo_name.lower() in existing:
                continue

            print(f"  🔎 Evaluating: {title[:80]}...")
            content = smart_fetch(url)
            if not content or not has_internship_signals(content):
                continue

            source = "web search"
            data = _extract_discovery(content, url, source, skill_prompt)

            if data and data.get("found"):
                company_name = data.get("company_name", pseudo_name)
                print(f"  ✅ DISCOVERED: {data.get('role_title')} @ {company_name}")
                os.system(f'python db_tool.py add-company --name "{company_name}" --location "{data.get("location", "Unknown")}" --domain "AI Engineering" --career-url "{url}"')
                _log_via_cli(data)
                existing.add(company_name.lower())
                discovered += 1

    return discovered

def _extract_discovery(content, url, source, skill_prompt):
    prompt = f"""{skill_prompt}
### 📄 Page Content (from {source}: {url})
---
{content[:55000]}
---
**Instructions:** Analyze this page for AI Engineering internship openings matching the Candidate Profile.
Return ONLY a valid JSON object with the expected keys (found, role_title, company_name, etc).
If no valid matching opening exists, set "found" to false.
"""
    payload = {"model": "gemma3:4b", "prompt": prompt, "stream": False, "format": "json"}
    try:
        with gemma_lock:
            response = requests.post(OLLAMA_URL, json=payload, timeout=120)
            response.raise_for_status()
            raw = response.json().get("response", "").strip()
        
        if raw.startswith("```json"): raw = raw[7:]
        elif raw.startswith("```"): raw = raw[3:]
        if raw.endswith("```"): raw = raw[:-3]
        raw = raw.strip()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            match = re.search(r'\{.*\}', raw, re.DOTALL)
            if match: return json.loads(match.group(0))
            return None
    except:
        return None

# ---------------------------------------------------------------------------
# Logging helper
# ---------------------------------------------------------------------------

def _log_via_cli(data):
    def esc(val):
        return str(val or "").replace('"', '\\"').replace("\n", " ")

    parts = [
        f'--company "{esc(data.get("company_name", ""))}"',
        f'--role "{esc(data.get("role_title", ""))}"',
        f'--link "{esc(data.get("direct_apply_link", ""))}"',
        f'--source "{esc(data.get("source", "careers page"))}"',
        f'--eligibility "{esc(data.get("eligibility", ""))}"',
        f'--location "{esc(data.get("location", ""))}"',
        f'--stipend "{esc(data.get("stipend", ""))}"',
        f'--ppo-potential "{esc(data.get("ppo_potential", ""))}"',
        f'--match-reason "{esc(data.get("match_reason", ""))}"',
        f'--dsa-level "{esc(data.get("dsa_level", ""))}"',
        f'--edge-notes "{esc(data.get("edge_notes", ""))}"',
    ]
    cmd = f'python db_tool.py log-opening {" ".join(parts)}'
    os.system(cmd)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_daily(phase_filter=None):
    print(f"\n{'🌅' * 20}")
    print(f"  HERMES DAILY RUN (Hybrid Waterfall) — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'🌅' * 20}\n")

    skill_prompt = load_skill_prompt()
    total = 0

    if phase_filter is None or phase_filter == 1:
        p1_count = phase_1(skill_prompt)
        total += p1_count
        print(f"\n📊 Phase 1 complete: {p1_count} openings found.")

    if phase_filter is None or phase_filter == 2:
        if total < MIN_OPENINGS_THRESHOLD:
            print(f"\n⚡ Phase 1 yield ({total}) below threshold ({MIN_OPENINGS_THRESHOLD}). Activating Wild Hunt...")
            p2_count = phase_2(skill_prompt)
            total += p2_count
            print(f"\n📊 Phase 2 complete: {p2_count} new discoveries.")
        else:
            print(f"\n✅ Phase 1 yield ({total}) meets threshold. Skipping Phase 2.")

    print(f"\n{'=' * 60}")
    print(f"  TOTAL: {total} openings logged today.")
    print(f"  Run `python db_tool.py report` for the full briefing.")
    print(f"{'=' * 60}\n")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Hermes Daily Runner")
    parser.add_argument("--phase", type=int, choices=[1, 2], help="Run only a specific phase")
    parser.add_argument("--report", action="store_true", help="Just show today's report")
    args = parser.parse_args()

    if args.report:
        os.system("python db_tool.py report")
    else:
        run_daily(phase_filter=args.phase)
