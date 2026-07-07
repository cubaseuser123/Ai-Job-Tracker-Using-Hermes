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
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

MIN_OPENINGS_THRESHOLD = 10

gemma_lock = threading.Lock()
error_log_lock = threading.Lock()

def log_error(company_name, raw_output, error_msg):
    with error_log_lock:
        try:
            with open("errors.json", "r", encoding="utf-8") as f:
                errors = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            errors = []
        errors.append({
            "company": company_name,
            "raw_output": raw_output,
            "error": str(error_msg),
            "timestamp": datetime.now().isoformat()
        })
        with open("errors.json", "w", encoding="utf-8") as f:
            json.dump(errors, f, indent=2)

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
        f'"{name}" intern site:workatastartup.com OR site:instahyre.com OR site:cutshort.io',
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
                elif "workatastartup.com" in url: source = "YC WorkAtAStartup"
                elif "instahyre.com" in url: source = "Instahyre"
                elif "cutshort.io" in url: source = "Cutshort"
                urls.append((source, url))

    return urls

# ---------------------------------------------------------------------------
# LLM Extraction via Gemma
# ---------------------------------------------------------------------------

def load_skill_prompt():
    with open(SKILL_PATH, "r", encoding="utf-8") as f:
        skill = f.read()
    try:
        with open("memory.md", "r", encoding="utf-8") as f:
            memory = f.read()
            if memory.strip():
                skill += "\n\n### PERSISTENT MEMORY / RULES\n" + memory
    except FileNotFoundError:
        pass
    return skill

def extract_with_gemma(content, company, url, source, skill_prompt):
    name = company["name"] if type(company) != str else company
    print(f"  [{name}] Extracting via Qwen 2.5 7B ({source})...")

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
{content[:15000]}
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
- "hiring_manager": string or null

If no valid matching opening exists, set "found" to false and leave other fields empty.
"""

    payload = {
        "model": "qwen2.5:7b",
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
            return data
        except json.JSONDecodeError as e:
            # Fallback robust regex extraction for {} blocks
            match = re.search(r'\{.*\}', raw, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group(0))
                    return data
                except Exception as ex:
                    log_error(name, raw, f"Regex fallback failed: {ex}")
                    raise ValueError(f"Could not parse JSON: {raw[:100]}")
            else:
                log_error(name, raw, f"JSON decode error: {e}")
                raise ValueError(f"Could not parse JSON: {raw[:100]}")
                
    except Exception as e:
        print(f"  [{name}] Qwen extraction failed: {e}")
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
            
            manager = data.get("hiring_manager")
            if not manager:
                print(f"  🕵️ Launching Insider Recon for {name}...")
                recon_query = f'"{name}" (engineering manager OR technical recruiter) site:linkedin.com/in'
                recon_results = smart_search(recon_query, max_results=2)
                if recon_results:
                    insiders = [rr.get('title', '').split(' - ')[0] for rr in recon_results]
                    if insiders:
                        added_note = "Insider Targets: " + ", ".join(insiders)
                        existing_notes = data.get("edge_notes", "")
                        data["edge_notes"] = f"{existing_notes} | {added_note}".strip(" |")
                        print(f"  🎯 Found Insiders: {', '.join(insiders)}")

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

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(_process_discovery_query, q, existing, skill_prompt) for q in DISCOVERY_QUERIES]
        for future in concurrent.futures.as_completed(futures):
            try:
                discovered += future.result()
            except Exception as e:
                print(f"  ❌ Error in Wild Hunt query: {e}")

    return discovered

def _process_discovery_query(query, existing, skill_prompt):
    print(f"\n🌐 Searching: {query}")
    results = smart_search(query, max_results=5)
    found_count = 0

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
            
            manager = data.get("hiring_manager")
            if not manager:
                print(f"  🕵️ Launching Insider Recon for {company_name}...")
                recon_query = f'"{company_name}" (engineering manager OR technical recruiter) site:linkedin.com/in'
                recon_results = smart_search(recon_query, max_results=2)
                if recon_results:
                    insiders = [rr.get('title', '').split(' - ')[0] for rr in recon_results]
                    if insiders:
                        added_note = "Insider Targets: " + ", ".join(insiders)
                        existing_notes = data.get("edge_notes", "")
                        data["edge_notes"] = f"{existing_notes} | {added_note}".strip(" |")
                        print(f"  🎯 Found Insiders: {', '.join(insiders)}")

            print(f"  ✅ DISCOVERED: {data.get('role_title')} @ {company_name}")
            os.system(f'python db_tool.py add-company --name "{company_name}" --location "{data.get("location", "Unknown")}" --domain "AI Engineering" --career-url "{url}"')
            _log_via_cli(data)
            existing.add(company_name.lower())
            found_count += 1

    return found_count

def _extract_discovery(content, url, source, skill_prompt):
    prompt = f"""{skill_prompt}
### 📄 Page Content (from {source}: {url})
---
{content[:15000]}
---
**Instructions:** Analyze this page for AI Engineering internship openings matching the Candidate Profile.
Return ONLY a valid JSON object with the expected keys (found, role_title, company_name, edge_notes, hiring_manager, etc).
If no valid matching opening exists, set "found" to false.
"""
    payload = {"model": "qwen2.5:7b", "prompt": prompt, "stream": False, "format": "json"}
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
        except json.JSONDecodeError as e:
            match = re.search(r'\{.*\}', raw, re.DOTALL)
            if match: 
                try:
                    return json.loads(match.group(0))
                except:
                    log_error(source, raw, f"Regex fallback failed in discovery")
            else:
                log_error(source, raw, f"JSON decode error in discovery: {e}")
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
# Phase 3: Claude Supervisor Reflection
# ---------------------------------------------------------------------------

def phase_3_reflection():
    print("\n" + "=" * 60)
    print("  PHASE 3: Claude Supervisor Reflection")
    print("=" * 60)
    
    if not ANTHROPIC_API_KEY:
        print("  ❌ No Anthropic API Key found. Skipping reflection.")
        return
        
    try:
        with open("errors.json", "r", encoding="utf-8") as f:
            errors = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        print("  ✅ No extraction errors logged today. Memory is stable.")
        return
        
    if not errors:
        print("  ✅ No extraction errors logged today. Memory is stable.")
        return

    print(f"  🧠 Found {len(errors)} errors. Consulting Claude 3.5 Haiku...")
    
    prompt = f"""You are the Supervisor for an autonomous AI web scraper. 
Your local LLM agent made the following JSON extraction errors today:
{json.dumps(errors, indent=2)}

Analyze WHY the local model failed to output valid JSON. Did it hallucinate conversational text? Did it truncate? Did it include markdown wrappers?

Write a strict, 1-2 sentence rule in markdown format (starting with "- CRITICAL: ") that will be appended to the local model's system prompt to ensure it NEVER makes this specific formatting mistake again. Do not output anything other than the new rules."""

    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-3-5-haiku-latest",
            max_tokens=200,
            temperature=0.0,
            messages=[{"role": "user", "content": prompt}]
        )
        new_rules = response.content[0].text.strip()
        
        with open("memory.md", "a", encoding="utf-8") as f:
            f.write("\n" + new_rules + "\n")
            
        print(f"  📝 New Rules Added to Memory:\n{new_rules}")
        
        # Clear errors for next run
        with open("errors.json", "w", encoding="utf-8") as f:
            json.dump([], f)
            
    except Exception as e:
        print(f"  ❌ Reflection failed: {e}")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_daily(phase_filter=None):
    print(f"\n{'🌅' * 20}")
    print(f"  HERMES DAILY RUN (V2.1 Concurrent Swarm) — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'🌅' * 20}\n")

    skill_prompt = load_skill_prompt()
    total = 0

    if phase_filter is None:
        print("\n🚀 Activating Concurrent Swarm (Phase 1 & Phase 2 simultaneously)...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            future_p1 = executor.submit(phase_1, skill_prompt)
            future_p2 = executor.submit(phase_2, skill_prompt)
            
            try:
                p1_count = future_p1.result()
                p2_count = future_p2.result()
                total += (p1_count + p2_count)
            except Exception as e:
                print(f"  ❌ Error in Concurrent Swarm: {e}")
    else:
        if phase_filter == 1:
            total += phase_1(skill_prompt)
        elif phase_filter == 2:
            total += phase_2(skill_prompt)
            
    if phase_filter is None or phase_filter == 3:
        phase_3_reflection()

    print(f"\n{'=' * 60}")
    print(f"  TOTAL: {total} openings logged today.")
    print(f"  Run `python db_tool.py report` for the full briefing.")
    print(f"{'=' * 60}\n")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Hermes Daily Runner")
    parser.add_argument("--phase", type=int, choices=[1, 2, 3], help="Run only a specific phase")
    parser.add_argument("--report", action="store_true", help="Just show today's report")
    args = parser.parse_args()

    if args.report:
        os.system("python db_tool.py report")
    else:
        run_daily(phase_filter=args.phase)
