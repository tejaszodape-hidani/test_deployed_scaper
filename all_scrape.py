import sys
import time
import gc
import re
import json
import random
import os
import hashlib
import threading
import requests as _requests
from requests.adapters import HTTPAdapter
from urllib.parse import urlparse, parse_qs, unquote, urlencode, quote
# curl_cffi: impersonates Chrome TLS fingerprint — bypasses Cloudflare on SimplyHired
try:
    from curl_cffi import requests as cffi_requests
    _CURL_CFFI_AVAILABLE = True
except ImportError:
    _CURL_CFFI_AVAILABLE = False
    print("[SimplyHired] curl_cffi not installed. Run: pip install curl_cffi")
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium_stealth import stealth
from push_to_db import push_data, push_jobs_list  # centralised DB helpers
# meta_extract removed — AI metadata enrichment is disabled (useapi=False)

# ── Persistence flags ────────────────────────────────────────────────────────────────────────────
# Controlled via .env  (set to 'true' or 'false'):
#   SAVE_TO_JSON=true   → write JSON backup files after each scrape cycle
#   SAVE_TO_DB=true     → push jobs to MySQL/PostgreSQL database
#   USE_API=false       → enable AI metadata enrichment (requires meta_extract)
# ────────────────────────────────────────────────────────────────────────────────
SAVE_TO_JSON = os.getenv("SAVE_TO_JSON", "true").strip().lower() == "true"
useapi       = os.getenv("USE_API",       "false").strip().lower() == "true"
SAVE_TO_DB   = os.getenv("SAVE_TO_DATABASE",   "true").strip().lower()  == "true"


LINKEDIN_JOBS_PER_URL = 100      # Guest API allows 400+ jobs per URL (vs 60 with browser)
LINKEDIN_PAGE_SIZE    = 25   # LinkedIn shows exactly 25 results per page
SIMPLYHIRED_JOBS_PER_URL =80    # Reduced from 40 to speed up cycles
GLASSDOOR_JOBS_PER_URL = 20      # Reduced from 40
NAUKRI_JOBS_PER_URL = 20         # Reduced from 40
HIRINGCAFE_JOBS_PER_URL = 40     # Reduced from 40
INDEED_JOBS_PER_URL = 40       # Reduced from 40
REED_JOBS_PER_URL = 50

CURRENT_REGION = None

PROGRESS_FILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "job_progress.json")

# ── Pooled HTTP session (Fix 3) ───────────────────────────────────────────────
# Reused across all requests_get_with_retry calls to avoid per-call TCP setup.
_http_session = _requests.Session()
_http_session.mount("https://", HTTPAdapter(pool_connections=16, pool_maxsize=16))
_http_session.mount("http://",  HTTPAdapter(pool_connections=16, pool_maxsize=16))

# ── Progress-file throttle (Fix 1a) ──────────────────────────────────────────
# Avoid writing job_progress.json on every single job; flush at most every 2 s.
_PROGRESS_LAST_FLUSH: float = 0.0
_PROGRESS_MIN_INTERVAL: float = 2.0

print("Initializing Master Multi-Category Deep-Scraper...")
print("Mode: Deep Link Scraping (Visiting each job page for 100% accurate skills and descriptions)")

def get_company_logo(company_name, driver, company_url=None):
    """Try to extract a company logo from the current page, then fall back to Clearbit or initials."""
    if not company_name:
        return None

    # 1. Look for a logo <img> on the current page
    selectors = [
        'img[class*="logo"]',
        'img[alt*="logo"]',
        'img[src*="logo"]',
        'img[src*="company"]',
        '.company-logo img',
        '.companyLogo img',
        'img[data-testid="company-logo"]',
    ]
    for sel in selectors:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                for attr in ('src', 'data-src', 'data-lazy-src', 'data-original'):
                    src = el.get_attribute(attr)
                    if src and src.startswith('http'):
                        return src
                srcset = el.get_attribute('srcset')
                if srcset:
                    first = srcset.split(',')[0].strip().split(' ')[0]
                    if first.startswith('http'):
                        return first
        except Exception:
            continue

    return None


def decode_apply_url(url):
    """Extract the real destination URL from LinkedIn's safety redirect wrapper.
    e.g. https://www.linkedin.com/safety/go/?url=https%3A%2F%2Fcompany.com/job
    becomes https://company.com/job
    """
    if not url:
        return None
    try:
        if 'linkedin.com/safety/go' in url:
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            real_url = params.get('url', [None])[0]
            if real_url:
                return unquote(real_url)
        return url
    except:
        return url

def get_final_job_url(driver, listing_url):
    """Try to find an external apply/career-page URL on the current page.
    Returns the decoded external URL, or None to fall back to listing_url.
    Works for LinkedIn /safety/go wrappers and generic 'Apply' buttons.
    """
    try:
        # 1. Directly grab the visible external-apply <a> href.
        #    LinkedIn wraps these in /safety/go/?url=...; other sites link directly.
        apply_url = driver.execute_script("""
            let a = Array.from(document.querySelectorAll('a[href]')).find(a => {
                let t = (a.textContent || '').toLowerCase().trim();
                let aria = (a.getAttribute('aria-label') || '').toLowerCase();
                let data = (a.getAttribute('data-control-name') || '').toLowerCase();
                let h = a.href.toLowerCase();
                let isExternal = h.includes('linkedin.com/safety/go') || !h.includes(window.location.hostname);
                let looksApply = t.includes('apply') && !t.includes('easy');
                let looksExternal = t.includes('external') || t.includes('off-site') || t.includes('offsite');
                let ariaApply = aria.includes('apply') && !aria.includes('easy');
                let isApply = looksApply || looksExternal || ariaApply ||
                              data.includes('external_apply') || data.includes('offsite');
                return a.href.startsWith('http') && isExternal && isApply;
            });
            return a ? a.href : null;
        """)
        if apply_url:
            return decode_apply_url(apply_url)

        # 2. LinkedIn offsite Apply: the real URL is only available at click time.
        #    We override window.open so we can capture it without leaving the page.
        apply_btn = driver.execute_script("""
            let svg = document.querySelector('svg.apply-button__offsite-apply-icon-svg');
            if (svg) return svg.closest('button');
            return Array.from(document.querySelectorAll('button, a, [role="button"]')).find(b => {
                let t = (b.textContent || '').toLowerCase().trim();
                let aria = (b.getAttribute('aria-label') || '').toLowerCase();
                let data = (b.getAttribute('data-control-name') || '').toLowerCase();
                let looksApply = t.includes('apply') && !t.includes('easy');
                let looksExternal = t.includes('external') || t.includes('off-site') || t.includes('offsite');
                let ariaApply = aria.includes('apply') && !aria.includes('easy');
                return looksApply || looksExternal || ariaApply ||
                       data.includes('external_apply') || data.includes('offsite');
            });
        """)
        if apply_btn:
            try:
                driver.execute_script("""
                    window.__capturedApplyUrl = null;
                    window.__originalOpen = window.open;
                    window.open = function(url, target) {
                        window.__capturedApplyUrl = url;
                        return null;
                    };
                """)
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", apply_btn)
                driver.execute_script("arguments[0].click();", apply_btn)
                time.sleep(1)
                driver.execute_script("""
                    var e = new MouseEvent('click', {bubbles: true, cancelable: true, view: window});
                    arguments[0].dispatchEvent(e);
                """, apply_btn)
                time.sleep(1.5)

                captured = driver.execute_script("return window.__capturedApplyUrl;")
                try:
                    driver.execute_script("window.open = window.__originalOpen; delete window.__capturedApplyUrl; delete window.__originalOpen;")
                except Exception:
                    pass

                if captured:
                    return decode_apply_url(captured)

                # If window.open wasn't used, check for a new tab / navigation.
                new_handles = [h for h in driver.window_handles if h != driver.current_window_handle]
                if new_handles:
                    original_handle = driver.current_window_handle
                    driver.switch_to.window(new_handles[-1])
                    time.sleep(2)
                    final_url = driver.current_url
                    driver.switch_to.window(original_handle)
                    return decode_apply_url(final_url)

                final_url = driver.current_url
                if final_url and final_url != listing_url:
                    return decode_apply_url(final_url)
            except Exception as click_e:
                print(f'[Apply URL] click/redirect failed: {click_e}')
    except Exception as e:
        print(f'[Apply URL] extraction failed for {listing_url[:80]}: {e}')
    return None

def clean_html(html_str):
    if not html_str: return None
    cleaned = re.sub(r'<(button|a)[^>]*>(?i:show more|show less|read more)</\1>', '', html_str, flags=re.IGNORECASE)
    cleaned = re.sub(r'(?i)show more\s*show less', '', cleaned)
    cleaned = re.sub(r'(?i)show more', '', cleaned)
    cleaned = re.sub(r'(?i)show less', '', cleaned)
    cleaned = re.sub(r'(?i)<!------>', '', cleaned)
    cleaned = re.sub(r'<script.*?</script>', '', cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r'<style.*?</style>', '', cleaned, flags=re.DOTALL | re.IGNORECASE)
    return cleaned.strip()

def clean_text(text):
    """Extract clean plain text from HTML, prioritizing the actual job description body.
    For LinkedIn HTML, targets the description section specifically to skip boilerplate."""
    if not text:
        return None
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(text, 'html.parser')

        # 1. Try to grab LinkedIn's description section directly (most specific)
        desc_el = (
            soup.find('div', class_=lambda c: c and 'description__text' in c) or
            soup.find('div', {'class': 'show-more-less-html__markup'}) or
            soup.find('section', class_=lambda c: c and 'description' in c) or
            soup.find('div', class_=lambda c: c and 'job-description' in c)
        )
        if desc_el:
            return re.sub(r'\s+', ' ', desc_el.get_text(separator=' ', strip=True)).strip()

        # 2. Fallback: strip all tags and whitespace
        plain = re.sub(r'<[^>]+>', ' ', text)
    except Exception:
        plain = re.sub(r'<[^>]+>', ' ', text)

    cleaned = re.sub(r'\s+', ' ', plain)
    cleaned = re.sub(r'(?i)show more\s*show less', '', cleaned)
    cleaned = re.sub(r'(?i)show more', '', cleaned)
    cleaned = re.sub(r'(?i)show less', '', cleaned)
    return cleaned.strip()

def clean_salary(salary_text):
    if not salary_text: return None
    cleaned = clean_text(salary_text)
    cleaned = re.sub(r'\([^)]+\)', '', cleaned)
    cleaned = re.sub(r'\[[^\]]+\]', '', cleaned)
    cleaned = re.sub(r'\b(Employer|Est|Estimated|Glassdoor|Indeed|ZipRecruiter|Provided|est\.)\b', '', cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.replace('( )', '').replace('()', '').strip()
    if '$' in cleaned: return cleaned
    return None

def is_valid_location(location):
    global CURRENT_REGION
    if not location:
        return True
    loc_lower = location.lower()
    
    if CURRENT_REGION == "UK":
        if "remote" in loc_lower or "uk" in loc_lower or "united kingdom" in loc_lower or "england" in loc_lower or "london" in loc_lower or "scotland" in loc_lower or "wales" in loc_lower or "ireland" in loc_lower:
            return True
        return False
        
    if loc_lower in ("united states", "us", "usa") or "remote" in loc_lower:
        return True
        
    # Exclude non-US locations first
    non_us = ['india', 'canada', 'uk', 'united kingdom', 'australia', 'philippines', 'bengaluru', 'toronto', 'ontario', 'manila', 'london', 'pakistan', 'germany', 'mexico']
    for country in non_us:
        if country in loc_lower:
            return False
            
    # Check for state codes (case-sensitive on the original location string for word boundary)
    if re.search(r'\b(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)\b', location):
        return True
        
    # Check for full US state names
    us_states = [
        'alabama', 'alaska', 'arizona', 'arkansas', 'california', 'colorado', 'connecticut', 
        'delaware', 'florida', 'georgia', 'hawaii', 'idaho', 'illinois', 'indiana', 'iowa', 
        'kansas', 'kentucky', 'louisiana', 'maine', 'maryland', 'massachusetts', 'michigan', 
        'minnesota', 'mississippi', 'missouri', 'montana', 'nebraska', 'nevada', 'new hampshire', 
        'new jersey', 'new mexico', 'new york', 'north carolina', 'north dakota', 'ohio', 
        'oklahoma', 'oregon', 'pennsylvania', 'rhode island', 'south carolina', 'south dakota', 
        'tennessee', 'texas', 'utah', 'vermont', 'virginia', 'washington', 'west virginia', 
        'wisconsin', 'wyoming'
    ]
    for state in us_states:
        if state in loc_lower:
            return True
            
    # Check for major US cities and metro area keywords
    us_cities = [
        'chicago', 'seattle', 'austin', 'boston', 'san francisco', 'los angeles', 'nyc', 
        'atlanta', 'dallas', 'houston', 'denver', 'phoenix', 'philadelphia', 'detroit', 
        'minneapolis', 'miami', 'san diego', 'portland', 'silicon valley', 'bay area', 
        'brooklyn', 'manhattan', 'queens', 'bronx', 'staten island'
    ]
    for city in us_cities:
        if city in loc_lower:
            return True
            
    if 'united states' in loc_lower or 'usa' in loc_lower.split():
        return True
        
    return False


# ── Skill map (Fix 9) ────────────────────────────────────────────────────────
# Built once at module load instead of on every extract_skills_from_text call.
_SKILL_MAP = {
    'Python': ['python'],
    'Java': ['java '],
    'JavaScript': ['javascript', 'js ', 'es6'],
    'TypeScript': ['typescript', 'ts '],
    'C++': ['c++', 'cpp'],
    'C#': ['c#', '.net'],
    'Go': ['golang', 'go '],
    'Rust': ['rust'],
    'Ruby': ['ruby', 'rails'],
    'PHP': ['php', 'laravel'],
    'Swift': ['swift', 'ios'],
    'Kotlin': ['kotlin', 'android'],

    'React': ['react', 'react.js', 'reactjs'],
    'Angular': ['angular', 'angularjs'],
    'Vue': ['vue', 'vue.js', 'vuejs'],
    'Node.js': ['node.js', 'nodejs', 'node '],
    'Django': ['django'],
    'Spring': ['spring boot', 'spring '],

    'SQL': ['sql', 'mysql', 'postgresql', 'postgres'],
    'NoSQL': ['nosql', 'mongodb', 'mongo ', 'cassandra', 'dynamodb'],
    'Redis': ['redis'],

    'AWS': ['aws', 'amazon web services'],
    'Azure': ['azure'],
    'GCP': ['gcp', 'google cloud'],

    'Docker': ['docker'],
    'Kubernetes': ['kubernetes', 'k8s'],
    'CI/CD': ['ci/cd', 'jenkins', 'github actions', 'gitlab ci'],
    'Git': ['git '],
    'Linux': ['linux', 'unix'],
    'GraphQL': ['graphql'],
    'REST API': ['rest api', 'restful'],
}

def extract_skills_from_text(text):
    if not text: return []
    text_lower = text.lower()
    skills = []

    for exact_skill, keywords in _SKILL_MAP.items():
        for keyword in keywords:
            # Special chars like C++, C# don't work with \b — match literally
            if '+' in keyword or '#' in keyword:
                if keyword in text_lower:
                    skills.append(exact_skill)
                    break
            else:
                if re.search(r'\b' + re.escape(keyword) + r'(?:\b|\s)', text_lower):
                    skills.append(exact_skill)
                    break

    return list(set(skills))

def extract_experience_from_text(text):
    if not text: return "Not Specified"
    text_lower = text.lower()
    
    match_range = re.search(r'(\d+)\s*(?:to|-)\s*(\d+)\s*(?:years?|yrs?)', text_lower)
    if match_range:
        return f"{match_range.group(1)}-{match_range.group(2)} Years"
        
    match_single = re.search(r'(\d+)\+?\s*(?:years?|yrs?)', text_lower)
    if match_single:
        val = int(match_single.group(1))
        if val <= 30:
            return f"{val}+ Years"
            
    if re.search(r'\b(senior|sr\.|lead|principal|manager|director)\b', text_lower):
        return "Senior Level"
    if re.search(r'\b(junior|jr\.|entry(-|\s)level|intern)\b', text_lower):
        return "Entry Level"
        
    return "Not Specified"

def extract_salary_from_text(text):
    if not text: return None
    text_lower = text.lower()
    
    # 1. Match Ranges (e.g., $100k - 120k, 100,000 to 120,000, $80 - $100 /hr)
    range_pattern = r'(?:\$\d+(?:,\d{3})*(?:\.\d+)?k?|\d{2,3}(?:,\d{3}|k))\s*(?:-|to)\s*(?:\$\d+(?:,\d{3})*(?:\.\d+)?k?|\d{2,3}(?:,\d{3}|k))(?:\s*/\s*(?:yr|year|month|hr|hour|annually))?'
    match_range = re.search(range_pattern, text_lower)
    if match_range: return match_range.group(0).title()
    
    # 2. Match Single with Time Period (e.g., $120,000/yr, 120k/yr, $80/hr)
    single_time_pattern = r'(?:\$\d+(?:,\d{3})*(?:\.\d+)?k?|\d{2,3}(?:,\d{3}|k))\s*/\s*(?:yr|year|month|hr|hour|annually)'
    match_single = re.search(single_time_pattern, text_lower)
    if match_single: return match_single.group(0).title()
    
    # 3. Match Single Dollar Amount (e.g., $120,000, $120k, $80.00)
    dollar_pattern = r'\$\d+(?:,\d{3})+(?:\.\d+)?|\$\d{2,3}(?:\.\d+)?k'
    match_dollar = re.search(dollar_pattern, text_lower)
    if match_dollar: return match_dollar.group(0).title()
    
    return None

# Canonical category names — the SINGLE source of truth used everywhere
# (scraper output → DB → API → dashboard → external portal).
# Keep these stable; renaming any of these breaks downstream consumers.
CANONICAL_CATEGORIES = {
    'ui/ux': 'UI/UX',
    'frontend developer': 'Frontend Developer',
    'backend developer': 'Backend Developer',
    'fullstack developer': 'Fullstack Developer',
    'ml engineer': 'ML Engineer',
    'ai engineer': 'AI Engineer',
    'data scientist': 'Data Scientist',
    'data engineer': 'Data Engineer',
    'data analyst': 'Data Analyst',
    'devops/cloud engineer': 'Devops/Cloud Engineer',
    'cyber security': 'Cyber Security',
    'network engineer': 'Network Engineer',
    'system administrator': 'System Administrator',
    'business analyst': 'Business Analyst',
    'financial analyst': 'Financial Analyst',
    'supply chain analyst': 'Supply Chain Analyst',
    'product manager': 'Product Manager',
    'project manager': 'Project Manager',
    'physical design engineer': 'Physical Design Engineer',
    'asic design engineer': 'ASIC Design Engineer',
    'ic design engineer': 'IC Design Engineer',
    'java engineer': 'Java Engineer',
    'process engineer': 'Process Engineer',
    'automotive engineer': 'Automotive Engineer',
    'product engineer': 'Product Engineer',
    'software engineer': 'Software Engineer',
    'bioinformatics/ biomedical engineering': 'Bioinformatics/ Biomedical Engineering',
    'research assistant/ scientist': 'Research Assistant/ Scientist',
    'electrical engineer': 'Electrical Engineer',
    'electric engineer': 'Electrical Engineer',
    'manufacturing engineer': 'Manufacturing Engineer',
    'product development engineer': 'Product Development Engineer',
    # legacy / config-side aliases
    'software developer/engineer': 'Software Engineer',
}

# Categories that are "generic" — i.e. when the original scrape target was just
# "Software Developer/Engineer", we still want to slot a generic "Software Engineer"
# title into Software Engineer. But for a SPECIFIC scrape target (e.g. Frontend
# Developer), we MUST keep the original category for generic titles instead of
# dumping them all into Software Engineer.
GENERIC_SOURCE_CATEGORIES = {
    'software developer/engineer',
    'software engineer',
    'general',
    'unknown',
    '',
}

def _canonicalize(cat):
    """Map a raw category string to the canonical name. Falls back to the title-cased value."""
    if not cat:
        return 'Unknown'
    key = str(cat).strip().lower()
    if key in CANONICAL_CATEGORIES:
        return CANONICAL_CATEGORIES[key]
    return str(cat).strip()

def get_refined_category(title, current_category):
    """
    Returns the initial assigned category without doing any title-based refinement,
    as requested by the user.
    """
    return _canonicalize(current_category)

def get_element_text(driver, selectors):
    for selector in selectors:
        try:
            elem = driver.find_element(By.CSS_SELECTOR, selector)
            text = driver.execute_script("return arguments[0].textContent;", elem)
            if text and len(text.strip()) > 0:
                return clean_text(text)
        except: continue
    return None

def get_element_data(driver, selectors):
    for selector in selectors:
        try:
            elem = driver.find_element(By.CSS_SELECTOR, selector)
            text = driver.execute_script("return arguments[0].textContent;", elem)
            html = driver.execute_script("return arguments[0].innerHTML;", elem)
            if text and len(text.strip()) > 0:
                return clean_text(text), clean_html(html)
        except: continue
    return None, None

def get_platform_available_job_count(driver, platform="LinkedIn"):
    """
    Extract the total number of available matching jobs reported by the platform.
    Returns a string (e.g. '10,000+', '1,240', '520') or 'Unknown'.
    """
    if not driver:
        return "Unknown"

    try:
        count_js = """
            let selectors = [
                // LinkedIn
                '.results-context-header__job-count',
                'span.jobs-search-results-list__subtitle',
                'small.jobs-search-results-list__text',
                '[data-test-search-results-total]',
                'h1.job-search-results-header__headline',
                '.jobs-search-results-header__summary',
                // Indeed
                '.jobsearch-JobCountAndSortPane-jobCount',
                'div[class*="jobsearch-JobCount"]',
                'span.css-15093sn',
                // Glassdoor
                '[data-test="job-count"]',
                'h1.SearchResultsHeader',
                '.count',
                // SimplyHired
                'span[data-testid="jobCount"]',
                '.css-1wh2oox',
                // General
                '.search-results-count',
                '.total-jobs'
            ];
            for (let sel of selectors) {
                try {
                    let el = document.querySelector(sel);
                    if (el && el.innerText && el.innerText.trim()) {
                        return el.innerText.trim();
                    }
                } catch(e) {}
            }

            // Fallback: search visible page text for patterns like '1,234 jobs' or '10,000+ results'
            try {
                let text = document.body ? document.body.innerText : '';
                let m = text.match(/(?:page\\s+\\d+\\s+of\\s+)?([0-9,]+(?:\\+)?)\\s*(?:results|jobs|openings|positions)/i);
                if (m) return m[1].trim();
            } catch(e) {}
            return null;
        """
        raw_count = driver.execute_script(count_js)
        if raw_count:
            cleaned = re.sub(r'(?i)(results|jobs|openings|positions|page\s+\d+\s+of)', '', str(raw_count)).strip()
            if re.search(r'\d', cleaned):
                return cleaned
            return raw_count.strip()
    except Exception:
        pass
    return "Unknown"


def update_progress_file(progress_data, progress_file=PROGRESS_FILE_PATH, force=False):
    """Atomically saves the progress state to progress_file.

    Throttled to at most once every _PROGRESS_MIN_INTERVAL seconds to avoid
    hammering the filesystem on every single job scrape. Pass force=True for
    end-of-category / end-of-run checkpoints that must be written immediately.
    """
    global _PROGRESS_LAST_FLUSH
    now = time.monotonic()
    if not force and (now - _PROGRESS_LAST_FLUSH) < _PROGRESS_MIN_INTERVAL:
        return
    _PROGRESS_LAST_FLUSH = now
    try:
        progress_data["updatedAt"] = datetime.now().isoformat()
        temp_file = f"{progress_file}.tmp"
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(progress_data, f, indent=4, ensure_ascii=False)
        os.replace(temp_file, progress_file)
    except Exception:
        try:
            with open(progress_file, "w", encoding="utf-8") as f:
                json.dump(progress_data, f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"[Progress] Warning: could not write {progress_file}: {e}")

def setup_chrome_driver():
    """Sets up a robust Chrome driver for both local and cloud (Render) environments."""
    print("[Browser] Configuring Chrome options...")
    
    # Get binary paths from environment (set in Dockerfile) or use defaults
    chrome_bin = os.getenv("CHROME_BIN", "/usr/bin/chromium")
    chromedriver_path = os.getenv("CHROMEDRIVER_PATH", "/usr/bin/chromedriver")
    
    # Shared options — tuned for Render Starter (512 MB RAM)
    def add_standard_options(opt):
        opt.add_argument("--headless=new")           # CRITICAL for Render/Cloud
        opt.add_argument("--no-sandbox")
        opt.add_argument("--disable-dev-shm-usage")  # Use /tmp instead of /dev/shm
        opt.add_argument("--disable-gpu")
        opt.add_argument("--disable-extensions")
        opt.add_argument("--disable-setuid-sandbox")
        opt.add_argument("--disable-blink-features=AutomationControlled")
        opt.add_argument("--no-first-run")
        opt.add_argument("--no-default-browser-check")
        opt.add_argument("--window-size=1280,800")

        # ── Memory-saving flags (safe on all platforms) ─────────────────────────
        opt.add_argument("--disk-cache-size=1")
        opt.add_argument("--media-cache-size=1")
        opt.add_argument("--disable-background-networking")
        opt.add_argument("--disable-default-apps")
        opt.add_argument("--disable-sync")
        opt.add_argument("--disable-translate")
        opt.add_argument("--hide-scrollbars")
        opt.add_argument("--mute-audio")
        opt.add_argument("--safebrowsing-disable-auto-update")
        # Disable images — LinkedIn data is in DOM/JS, not images
        opt.add_argument("--blink-settings=imagesEnabled=false")
        # ───────────────────────────────────────────────────────────────────────

        opt.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        return opt

    def add_selenium_prefs(opt):
        """add_experimental_option only works with regular selenium Options, NOT uc.ChromeOptions."""
        prefs = {
            "profile.managed_default_content_settings.images": 2,
            "profile.default_content_setting_values.notifications": 2,
            "profile.managed_default_content_settings.stylesheets": 2,
        }
        opt.add_experimental_option("prefs", prefs)
        return opt

    try:
        import ssl
        ssl._create_default_https_context = ssl._create_unverified_context
        import undetected_chromedriver as uc
        print("[Browser] Attempting to use undetected-chromedriver...")
        options = uc.ChromeOptions()
        options = add_standard_options(options)
        # eager: stop waiting for images/stylesheets; DOM+JS is enough for job data
        options.page_load_strategy = "eager"

        # In Docker/Render, we MUST specify the browser path for UC
        if os.path.exists(chrome_bin):
            print(f"[Browser] Using Chrome binary at: {chrome_bin}")
            try:
                driver = uc.Chrome(
                    options=options,
                    browser_executable_path=chrome_bin,
                    driver_executable_path=chromedriver_path if os.path.exists(chromedriver_path) else None,
                    use_subprocess=True
                )
            except Exception as uc_e:
                import re
                m = re.search(r"Current browser version is (\d+)", str(uc_e))
                if m:
                    v = int(m.group(1))
                    print(f"[Browser] UC version mismatch. Retrying with version_main={v}...")
                    driver = uc.Chrome(
                        options=options,
                        browser_executable_path=chrome_bin,
                        driver_executable_path=chromedriver_path if os.path.exists(chromedriver_path) else None,
                        use_subprocess=True,
                        version_main=v
                    )
                else:
                    raise
        else:
            try:
                driver = uc.Chrome(options=options, use_subprocess=True)
            except Exception as uc_e:
                import re
                m = re.search(r"Current browser version is (\d+)", str(uc_e))
                if m:
                    v = int(m.group(1))
                    print(f"[Browser] UC version mismatch. Retrying with version_main={v}...")
                    driver = uc.Chrome(options=options, use_subprocess=True, version_main=v)
                else:
                    raise

        # 0 + explicit WebDriverWait is the correct pattern (mixing causes
        # unpredictable cumulative timeouts — Selenium docs)
        driver.implicitly_wait(0)
        driver.set_page_load_timeout(15)
        print("[Browser] Successfully initialized undetected-chromedriver")
        return driver
        
    except Exception as e:
        print(f"[Browser] undetected-chromedriver failed or not installed: {e}")
        print("[Browser] Falling back to regular Selenium with Chromium...")

    # Fallback: regular Selenium with anti-detection flags
    try:
        options = Options()
        options = add_standard_options(options)
        options = add_selenium_prefs(options)  # image/CSS blocking (Selenium only)
        options.page_load_strategy = "eager"   # don't wait for images/stylesheets
        if os.path.exists(chrome_bin):
            options.binary_location = chrome_bin

        service = Service(executable_path=chromedriver_path) if os.path.exists(chromedriver_path) else Service()

        driver = webdriver.Chrome(service=service, options=options)
        # 0 + explicit WebDriverWait is the correct pattern (mixing causes
        # unpredictable cumulative timeouts — Selenium docs)
        driver.implicitly_wait(0)
        driver.set_page_load_timeout(15)

        # Stealth tactics for regular Selenium
        stealth(driver,
            languages=["en-US", "en"],
            vendor="Google Inc.",
            platform="Win32",
            webgl_vendor="Intel Inc.",
            renderer="Intel Iris OpenGL Engine",
            fix_hairline=True,
        )

        print("[Browser] Successfully initialized regular Selenium (Chromium) with Stealth")
        return driver
    except Exception as e:
        print(f"[Browser] CRITICAL: Failed to initialize any browser: {e}")
        raise

# Sentinel returned by scrape_linkedin_jobs when LinkedIn shows an auth wall
# so the caller can force-restart the Chrome driver to get a fresh session.
class LinkedInAuthWall(Exception):
    pass

# Max job links to deep-scrape per LinkedIn search per cycle.
# With 2 GB RAM on the upgraded plan we paginate across up to 4 LinkedIn
# pages (25 each) giving 100 unique job URLs per search — 4x the old limit.


def canonical_url(url):
    """Strip query string + trailing slash from a job URL so the same job from
    two different referrers collapses to one identity. Preserves unique ID params."""
    if not url:
        return ""
    
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    
    # Preserve Indeed jk parameter
    if "indeed.com" in parsed.netloc and "jk" in qs:
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}?jk={qs['jk'][0]}".lower()
        
    # Preserve Glassdoor jl parameter
    if "glassdoor.com" in parsed.netloc and "jl" in qs:
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}?jl={qs['jl'][0]}".lower()
        
    base = url.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    return base.lower()


def make_stable_job_id(source, url, fallback_idx=0):
    """
    Build a STABLE upsert key for a job from the source + URL.

    Why this matters: the previous implementation used
        f"li_{random.randint(...)}_{i}"
    which generated a brand new jobId every cycle. Since the DB upsert is
    `WHERE jobId = X`, the same LinkedIn job was inserted as a NEW row every
    cycle — so after running for a few days the DB had 5-10 copies of every
    job and the portal kept showing the same handful of duplicates instead of
    a diverse set of fresh listings.

    By deriving jobId deterministically from the URL, the next cycle finds the
    existing row and UPDATES it instead of inserting a duplicate. The portal
    starts seeing real variety without any code change on its side.
    """
    src = (source or "src").lower()
    prefix_map = {
        "linkedin": "li",
        "simplyhired": "sh",
        "glassdoor": "gd",
        "naukri": "nk",
        "hiringcafe": "hc",
        "indeed": "in",
    }
    prefix = prefix_map.get(src, src[:2] or "xx")

    if url:
        cu = canonical_url(url)

        # LinkedIn: /jobs/view/<slug>-<numeric-id>  — the trailing digits are
        # LinkedIn's stable job id.
        if "linkedin.com" in cu:
            m = re.search(r"/jobs/view/[^/]*?-(\d{6,})$", cu)
            if not m:
                m = re.search(r"/jobs/view/(\d{6,})$", cu)
            if not m:
                m = re.search(r"(\d{8,})", cu)
            if m:
                return f"li_{m.group(1)}"

        # SimplyHired: /job/<base64-ish-hash>
        if "simplyhired.com" in cu:
            m = re.search(r"/job/([A-Za-z0-9_\-]+)", cu)
            if m:
                return f"sh_{m.group(1)[:200]}"

        # Glassdoor: ...jobListingId=<id>... OR /job-listing/...-JV_<id>
        if "glassdoor." in cu:
            m = re.search(r"jobListingId[=:](\d+)", cu)
            if not m:
                m = re.search(r"jv_[a-z0-9_]+?(\d{8,})", cu)
            if not m:
                m = re.search(r"(\d{8,})", cu)
            if m:
                return f"gd_{m.group(1)}"

        # HiringCafe: /job/<slug>
        if "hiring.cafe" in cu:
            m = re.search(r"/job/([A-Za-z0-9_\-]+)", cu)
            if m:
                return f"hc_{m.group(1)[:200]}"

        # Generic fallback: SHA1 of the canonical URL. This is still stable
        # across cycles for the same job, just opaque.
        digest = hashlib.sha1(cu.encode("utf-8")).hexdigest()[:20]
        return f"{prefix}_{digest}"

    # No URL at all — last resort, use a time-bucket + index. This still
    # avoids per-cycle randomness in the common case where the SAME job has
    # no URL: identical inputs map to identical jobIds within the same minute.
    bucket = int(time.time() // 60)
    return f"{prefix}_nourl_{bucket}_{fallback_idx}"

# Statuses that are worth retrying; everything else (4xx) is returned as-is / None.
_RETRY_STATUSES = {429, 500, 502, 503, 504}

def requests_get_with_retry(url, headers=None, timeout=10, retries=3):
    """GET with smart retry: uses a pooled session, retries only on 429/5xx,
    respects Retry-After, and returns None immediately for 4xx errors (e.g. 404)
    instead of stalling for 60 s.
    """
    last_exc = None
    for attempt in range(retries):
        try:
            r = _http_session.get(url, headers=headers, timeout=timeout)
            if r.status_code == 200:
                return r
            if r.status_code not in _RETRY_STATUSES:
                # 404, 410, 403 etc. — not worth retrying
                print(f"  [HTTP] {r.status_code} on {url[:80]} — skipping (no retry)")
                return None
            # 429 / 5xx — back off and retry
            wait = float(r.headers.get("Retry-After", 2 ** attempt * 2))
            print(f"  [HTTP] {r.status_code} on {url[:80]} — retrying in {wait:.1f}s (attempt {attempt+1}/{retries})")
        except _requests.RequestException as e:
            last_exc = e
            wait = 2 ** attempt * 2
            print(f"  [HTTP] Exception on {url[:80]}: {e} — retrying in {wait:.1f}s (attempt {attempt+1}/{retries})")
        time.sleep(wait + random.random())
    if last_exc:
        print(f"  [HTTP] All {retries} attempts failed for {url[:80]}: {last_exc}")
    return None

def collect_linkedin_links_via_api(url, max_jobs=400):
    """
    Collect LinkedIn job links using the guest jobs API — no browser needed.
    Returns up to max_jobs unique job dicts: {url, title, company, location}
    
    LinkedIn's guest API: /jobs-guest/jobs/api/seeMoreJobPostings/search?...
    In testing this returned 400 jobs across 40 pages without any blocking.
    """
    import requests
    from bs4 import BeautifulSoup
    import urllib.parse

    # Convert browser search URL → guest API URL
    parsed = urllib.parse.urlparse(url)
    params = dict(urllib.parse.parse_qsl(parsed.query))
    # Remove browser-only params that confuse the API
    for key in ['sortBy', 'currentJobId', 'origin', 'refresh', 'position', 'pageNum', 'start']:
        params.pop(key, None)

    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': 'https://www.linkedin.com/jobs/search/',
    }

    all_jobs = []
    seen_urls = set()
    consecutive_empty = 0

    for start in range(0, max_jobs + 25, 25):  # +25 so we don't miss the last page
        if len(all_jobs) >= max_jobs:
            break
        params['start'] = str(start)
        api_url = f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?{urllib.parse.urlencode(params)}"
        try:
            r = requests_get_with_retry(api_url, headers=headers, timeout=10)
            if not r or r.status_code != 200:
                print(f"  [LinkedIn API] HTTP {r.status_code if r else 'Error'} at start={start}. Stopping.")
                break

            soup = BeautifulSoup(r.text, 'html.parser')
            cards = soup.find_all(['div', 'li'], class_=lambda c: c and 'base-card' in c)

            new_count = 0
            for card in cards:
                link_el = card.find('a', class_=lambda c: c and 'base-card__full-link' in c)
                title_el = card.find(class_=lambda c: c and any(x in (c if isinstance(c, str) else ' '.join(c))
                                                                  for x in ['base-search-card__title', 'job-card-list__title']))
                company_el = card.find(class_=lambda c: c and any(x in (c if isinstance(c, str) else ' '.join(c))
                                                                    for x in ['base-search-card__subtitle', 'job-card-container__company-name']))
                location_el = card.find(class_=lambda c: c and any(x in (c if isinstance(c, str) else ' '.join(c))
                                                                     for x in ['job-search-card__location', 'job-card-container__metadata-item']))
                if not link_el:
                    continue
                job_url = link_el.get('href', '').split('?')[0].rstrip('/')
                if not job_url or job_url in seen_urls:
                    continue
                seen_urls.add(job_url)
                all_jobs.append({
                    'url': job_url,
                    'title': title_el.get_text(strip=True) if title_el else 'Unknown',
                    'company': company_el.get_text(strip=True) if company_el else 'Unknown',
                    'location': location_el.get_text(strip=True) if location_el else 'Unknown',
                })
                new_count += 1

            print(f"  [LinkedIn API] start={start}: +{new_count} new | total={len(all_jobs)}")

            if new_count == 0:
                consecutive_empty += 1
                if consecutive_empty >= 2:
                    print(f"  [LinkedIn API] No new jobs for 2 pages. Stopping.")
                    break
            else:
                consecutive_empty = 0

            time.sleep(random.uniform(0.3, 0.7))  # polite delay

        except Exception as e:
            print(f"  [LinkedIn API] Error at start={start}: {e}")
            break

    return all_jobs[:max_jobs]


def scrape_linkedin_jobs(driver, url, category, on_count=None, on_job_scraped=None, count_only=False):
    print(f"\n[LinkedIn - {category}] Collecting job links via guest API (up to {LINKEDIN_JOBS_PER_URL})...")

    # ── Step 1: Collect links via the fast guest API (no browser needed) ──────
    all_jobs = collect_linkedin_links_via_api(url, max_jobs=LINKEDIN_JOBS_PER_URL)

    # If API returned nothing, fall back to browser-based collection
    if not all_jobs:
        print(f"  [LinkedIn] Guest API returned 0 jobs — falling back to browser scraping...")
        base_url = re.sub(r'[&?]start=\d+', '', url).rstrip('&').rstrip('?')
        separator = '&' if '?' in base_url else '?'
        all_links_seen: set = set()
        all_jobs_browser: list = []
        block_signals = ["authwall", "checkpoint", "login", "verify", "challenge", "signup"]
        page_num = 0
        while len(all_jobs_browser) < LINKEDIN_JOBS_PER_URL:
            start_offset = page_num * LINKEDIN_PAGE_SIZE
            page_url = f"{base_url}{separator}start={start_offset}"
            driver.get(page_url)
            try:
                WebDriverWait(driver, 8).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='/jobs/view/']"))
                )
            except:
                pass
            for _ in range(4):
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(0.5)
            current_url = driver.current_url
            page_title = driver.title
            if any(s in current_url.lower() for s in block_signals) or \
               any(s in page_title.lower() for s in ["sign in", "join now", "verify"]):
                raise LinkedInAuthWall(category)
            page_jobs = driver.execute_script("""
                let cards = document.querySelectorAll('.base-card, .job-card-container, .job-search-card');
                let out = [];
                cards.forEach(card => {
                    let titleEl = card.querySelector('.base-search-card__title, .job-card-list__title, h3');
                    let compEl  = card.querySelector('.base-search-card__subtitle, .job-card-container__company-name, h4');
                    let locEl   = card.querySelector('.job-search-card__location, .job-card-container__metadata-item');
                    let linkEl  = card.querySelector('a.base-card__full-link, a.job-card-container__link');
                    if (titleEl && linkEl) {
                        let href = linkEl.href.split('?')[0];
                        out.push({title: titleEl.innerText.trim(), company: compEl ? compEl.innerText.trim() : 'Unknown',
                                  location: locEl ? locEl.innerText.trim() : 'Unknown', url: href});
                    }
                });
                return out;
            """) or []
            new_jobs = [j for j in page_jobs if j['url'] not in all_links_seen]
            for j in new_jobs:
                all_links_seen.add(j['url'])
                all_jobs_browser.append(j)
            print(f"  [LinkedIn Browser] Page {page_num+1}: {len(page_jobs)} found, {len(new_jobs)} new. Total: {len(all_jobs_browser)}")
            if not new_jobs:
                break
            page_num += 1
            time.sleep(random.uniform(1.5, 3.0))
        all_jobs = all_jobs_browser[:LINKEDIN_JOBS_PER_URL]

    available_reported = str(len(all_jobs)) + "+ (guest API)"
    print(f"  [COUNT] LinkedIn available reported: 'guest API' | Scrapable jobs found: {len(all_jobs)}")
    if on_count:
        on_count(category, "LinkedIn", available_reported, len(all_jobs))

    if count_only or not all_jobs:
        return []

    print(f"  [LinkedIn] Found {len(all_jobs)} jobs. Fetching descriptions via jobs-guest API...")
    jobs = []
    import requests

    for i, job_info in enumerate(all_jobs):
        title    = job_info['title']
        company  = job_info['company']
        location = job_info['location']
        link     = job_info['url']

        print(f"  -> [{i+1}] title={title!r} | company={company!r} | loc={location!r}")

        # ━━ Fix 2: Filter BEFORE fetching the description so we skip the API call
        # for jobs that will be discarded anyway (missing title, non-USA location).
        if not title or not company:
            print(f"  -> SKIPPED (missing title or company)")
            continue
        if not is_valid_location(location):
            print(f"  -> SKIPPED (non-target location: {location})")
            continue

        desc_text  = ""
        desc_html  = ""
        salary_text = None
        extracted_api_logo = None

        m = re.search(r'[-/](\d{9,10})', link)
        job_id = m.group(1) if m else None

        if job_id:
            try:
                api_url = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
                r = requests_get_with_retry(api_url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}, timeout=5)
                if r and r.status_code == 200:
                    raw_html  = r.text
                    
                    # Try to isolate the actual description div instead of capturing the entire page with buttons/signup
                    if 'description__text' in raw_html or 'show-more-less-html__markup' in raw_html:
                        from bs4 import BeautifulSoup
                        soup = BeautifulSoup(raw_html, 'html.parser')
                        desc_div = soup.find('div', class_='description__text') or soup.find('div', class_='show-more-less-html__markup')
                        desc_html = str(desc_div) if desc_div else raw_html
                    else:
                        desc_html = raw_html
                        
                    desc_text  = clean_text(desc_html)
                    
                    m_logo = re.search(r'data-tracking-control-name="public_jobs_topcard_logo"[^>]*>\s*<[^>]*?data-delayed-url="([^"]+)"', desc_html)
                    if m_logo:
                        import html
                        extracted_api_logo = html.unescape(m_logo.group(1))
                        
                    m_sal = re.search(r'\$[\d,]+(?:\.\d+)?(?:k|K)?(?:\s*[-\/]\s*\$?[\d,]+(?:\.\d+)?(?:k|K)?)?(?:\/(?:yr|year|hour|hr|mo|month))?', desc_text)
                    if m_sal:
                        salary_text = m_sal.group(0)
            except Exception as e:
                print(f"  [LinkedIn] Failed API fetch for {job_id}: {e}")

        time.sleep(random.uniform(0.1, 0.3))

        if not title or not company:
            # Should not reach here (filtered above), kept as safety net
            continue
        if not is_valid_location(location):
            continue

        job_data = {
            'jobId':           make_stable_job_id('LinkedIn', link, i),
            'jobTitle':        title[:500],
            'companyName':     company[:255],
            # Fix 8: get_company_logo(company, None) always returned None (driver is
            # None), so use extracted_api_logo directly instead of calling it.
            'companyLogo':     extracted_api_logo,
            'companyLocation': location[:255] if location else "United States",
            'jobLocation':     location[:255] if location else "United States",
            'jobType':         'Full-time',
            'yearOfExperience': extract_experience_from_text(desc_text),
            'skills':          extract_skills_from_text(desc_text),
            'jobPostTime':     datetime.now().isoformat(),
            'jobDescription':  desc_html,
            'salary':          (clean_salary(salary_text)[:255] if salary_text else None) or extract_salary_from_text(desc_text),
            'category':        category,
            'jobSource':       'LinkedIn',
            'jobUrl':          link,  # Guest API gives clean URLs directly — no browser visit needed
            'createdAt':       datetime.now().isoformat(),
            'updatedAt':       datetime.now().isoformat()
        }
        # AI metadata enrichment (useapi=True) removed — meta_extract not available

        # Fix 1b: Do NOT call save_to_json here — it reads+rewrites the whole
        # file on every job, growing O(N²). The final block in
        # count_and_scrape_with_progress writes all JSON files once at the end.

        jobs.append(job_data)
        if on_job_scraped:
            on_job_scraped(category, "LinkedIn", job_data)
    return jobs

# ── SimplyHired: curl_cffi + __NEXT_DATA__ (no Selenium / no Cloudflare) ─────
_SH_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.DOTALL,
)
_SH_BASE = "https://www.simplyhired.com"
_SH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
_sh_session = None  # shared curl_cffi session (lazily created)

def _get_sh_session():
    """Return (or create) a shared curl_cffi session that impersonates Chrome."""
    global _sh_session
    if _sh_session is None:
        if not _CURL_CFFI_AVAILABLE:
            return None
        _sh_session = cffi_requests.Session(impersonate="chrome124")
        _sh_session.headers.update(_SH_HEADERS)
        # Prime with a homepage visit so Cloudflare cookies are set
        try:
            _sh_session.get(_SH_BASE + "/", timeout=15)
            time.sleep(0.5)
        except Exception:
            pass
    return _sh_session


def _sh_extract_next_data(html):
    m = _SH_NEXT_DATA_RE.search(html)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}


def _sh_fetch_search_page(session, query, location, page=1):
    """Fetch one page of SimplyHired search results via __NEXT_DATA__."""
    params = {"q": query, "l": location, "t": "1"}
    if page > 1:
        params["pn"] = page
    url = _SH_BASE + "/search?" + urlencode(params)
    try:
        resp = session.get(url, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        print(f"  [SimplyHired] Search page {page} failed: {e}")
        return {}
    data = _sh_extract_next_data(resp.text)
    pp = data.get("props", {}).get("pageProps", {})
    return {
        "jobs": pp.get("jobs", []),
        "pageCursors": pp.get("pageCursors", {}),
        "resultCount": pp.get("resultCount", 0),
    }


def _sh_fetch_job_detail(session, job_key):
    """Fetch the full job detail page and return its pageProps dict."""
    url = _SH_BASE + "/job/" + job_key
    try:
        resp = session.get(url, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        print(f"  [SimplyHired] Detail fetch failed ({job_key}): {e}")
        return {}
    data = _sh_extract_next_data(resp.text)
    return data.get("props", {}).get("pageProps", {})

# ---------Simply Hired ----------------
def scrape_simplyhired_jobs(driver, url, category, on_count=None, on_job_scraped=None, count_only=False):
    """Scrape SimplyHired using curl_cffi + __NEXT_DATA__ JSON — no Selenium/Cloudflare."""
    print(f"\n[SimplyHired - {category}] Fetching USA jobs via __NEXT_DATA__ (no browser)...")

    if not _CURL_CFFI_AVAILABLE:
        print("  [SimplyHired] SKIP — curl_cffi not installed. Run: pip install curl_cffi")
        if on_count:
            on_count(category, "SimplyHired", "Unavailable", 0)
        return []

    # Parse query + location from the URL config
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    query    = qs.get("q", ["software engineer"])[0]
    location = qs.get("l", ["United States"])[0]

    session = _get_sh_session()

    # --- Count phase ---
    first_page = _sh_fetch_search_page(session, query, location, page=1)
    stubs_p1   = first_page.get("jobs", [])
    result_count = first_page.get("resultCount", 0)
    available_reported = str(result_count) if result_count else "Unknown"
    scrapable = min(len(stubs_p1), SIMPLYHIRED_JOBS_PER_URL)

    print(f"  [COUNT] SimplyHired available reported: '{available_reported}' | Scrapable: {scrapable}")
    if on_count:
        on_count(category, "SimplyHired", available_reported, scrapable)

    if count_only or not stubs_p1:
        return []

    # --- Scrape phase ---
    jobs = []
    page = 1
    page_stubs = stubs_p1

    while len(jobs) < SIMPLYHIRED_JOBS_PER_URL:
        for stub in page_stubs:
            if len(jobs) >= SIMPLYHIRED_JOBS_PER_URL:
                break

            job_key = stub.get("jobKey", "")
            if not job_key:
                continue

            print(f"  [{len(jobs)+1}/{SIMPLYHIRED_JOBS_PER_URL}] {stub.get('title','?')} @ {stub.get('company','?')}")

            detail = _sh_fetch_job_detail(session, job_key)
            if detail.get("jobFound") == False:
                print("    [SKIP] expired")
                continue

            job_url    = f"{_SH_BASE}/job/{job_key}"
            title      = (detail.get("jobTitle")     or stub.get("title", "")).strip()
            company    = (detail.get("employerName") or stub.get("company", "")).strip()
            # Fix 6: use a separate variable so we don't overwrite the search-level
            # `location` variable that _sh_fetch_search_page depends on for page 2+.
            job_location = (detail.get("formattedLocation") or stub.get("location", "United States")).strip()
            desc_html  = detail.get("jobDescriptionHtml", "")
            desc_text  = re.sub(r"<[^>]+>", " ", desc_html)

            if not title or not company:
                continue

            # Salary: prefer structured baseSalary field, then text extraction
            salary = stub.get("salaryInfo") or ""
            bs = detail.get("baseSalary") or {}
            if bs.get("minValue") and bs.get("maxValue"):
                salary = f"${bs['minValue']} - ${bs['maxValue']} {bs.get('unitText','')}".strip()
            if not salary:
                salary = extract_salary_from_text(desc_text)

            job_types = detail.get("jobTypes") or stub.get("jobTypes") or ["Full-time"]

            job_data = {
                'jobId':            make_stable_job_id('SimplyHired', job_key, len(jobs)),
                'jobTitle':         title[:500],
                'companyName':      company[:255],
                'companyLogo':      detail.get("employerSquareLogoUrl"),
                'companyLocation':  job_location[:255],
                'jobLocation':      job_location[:255],
                'jobType':          job_types[0] if job_types else 'Full-time',
                'yearOfExperience': extract_experience_from_text(desc_text),
                'skills':           extract_skills_from_text(desc_text) or stub.get("requirements", []),
                'jobPostTime':      datetime.now().isoformat(),
                'jobDescription':   desc_html,
                'salary':           salary,
                'category':         category,
                'jobSource':        'SimplyHired',
                'jobUrl':           job_url,
                'createdAt':        datetime.now().isoformat(),
                'updatedAt':        datetime.now().isoformat(),
            }
            jobs.append(job_data)
            if on_job_scraped:
                on_job_scraped(category, "SimplyHired", job_data)

            # Fix 7a: the _sh_fetch_job_detail network call already adds latency;
            # a small jitter is enough — no need for 0.4–1.0 s here.
            time.sleep(random.uniform(0.1, 0.3))

        if len(jobs) >= SIMPLYHIRED_JOBS_PER_URL:
            break

        # Paginate
        page_cursors = first_page.get("pageCursors", {})
        next_page_num = str(page + 1)
        if next_page_num not in page_cursors:
            break
        page += 1
        time.sleep(random.uniform(1.0, 2.0))
        next_result = _sh_fetch_search_page(session, query, location, page=page)
        page_stubs = next_result.get("jobs", [])
        if not page_stubs:
            break

    print(f"[SimplyHired] Done: {len(jobs)} jobs collected for '{category}'")
    return jobs

# def scrape_glassdoor_jobs(driver, url, category, on_count=None, on_job_scraped=None, count_only=False):
#     print(f"\n[Glassdoor - {category}] Fetching USA job links...")
#     driver.get(url)
    
#     # Wait for job listings to load
#     try:
#         WebDriverWait(driver, 8).until(
#             EC.presence_of_element_located((By.CSS_SELECTOR, "li[data-test='jobListing'] a, a[data-test='job-title']"))
#         )
#     except:
#         pass
        
#     for _ in range(2):
#         driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
#         time.sleep(0.5)
        
#     # Get available count
#     available_reported = get_platform_available_job_count(driver, "Glassdoor")
    
#     # Count how many cards are on the page
#     num_cards = driver.execute_script("return document.querySelectorAll(\"li[data-test='jobListing'] a, a[data-test='job-title']\").length;")
#     num_cards = min(num_cards, GLASSDOOR_JOBS_PER_URL)
    
#     print(f"  [COUNT] Glassdoor available reported: '{available_reported}' | Scrapable cards found: {num_cards}")
#     if on_count:
#         on_count(category, "Glassdoor", available_reported, num_cards)

#     if count_only or num_cards == 0:
#         return []

#     print(f"[Glassdoor] Found {num_cards} job cards. Starting deep scrape...")
#     jobs = []
    
#     for i in range(num_cards):
#         # Click the job card via JS to bypass overlays
#         driver.execute_script(f"""
#             let cards = document.querySelectorAll("li[data-test='jobListing'] a, a[data-test='job-title']");
#             if (cards.length > {i}) {{
#                 cards[{i}].click();
#             }}
#             // Try to close annoying popups if they appear
#             let closeBtn = document.querySelector(".CloseButton, [alt='Close']");
#             if (closeBtn) closeBtn.click();
#         """)
        
#         time.sleep(random.uniform(1.5, 2.5))
            
#         # Fast JS Extraction from the right pane / selected card
#         page_data = driver.execute_script(f"""
#             function getText(selectors) {{
#                 for (let sel of selectors) {{
#                     try {{
#                         let el = document.querySelector(sel);
#                         if (el && el.innerText.trim()) return el.innerText.trim();
#                     }} catch(e) {{}}
#                 }}
#                 return null;
#             }}
#             function getHtml(selectors) {{
#                 for (let sel of selectors) {{
#                     try {{
#                         let el = document.querySelector(sel);
#                         if (el && el.innerHTML.trim()) return el.innerHTML.trim();
#                     }} catch(e) {{}}
#                 }}
#                 return null;
#             }}
            
#             let linkHref = "";
#             try {{
#                 let cards = document.querySelectorAll("li[data-test='jobListing'] a, a[data-test='job-title']");
#                 if (cards.length > {i}) linkHref = cards[{i}].href;
#             }} catch(e) {{}}
            
#             return {{
#                 url: linkHref,
#                 title: getText(["header section h1", "[data-test='job-title-test']", "h1", "h2", ".job-title", "[class*='title']", "[class*='Title']"]),
#                 company: getText(["header section h4", "[data-test='employer-name-test']", ".employerName", "[class*='employer']", "[class*='Employer']", "[class*='company']", "[class*='Company']", "div.companyName"]),
#                 location: getText(["header section div.location", "[data-test='location']", ".location", "[class*='location']", "[class*='Location']"]),
#                 desc_text: getText(["div#JobDescriptionContainer", ".jobDescriptionContent", "[id*='jobDescription']", "[class*='description']", "[class*='Description']"]),
#                 desc_html: getHtml(["div#JobDescriptionContainer", ".jobDescriptionContent", "[id*='jobDescription']", "[class*='description']", "[class*='Description']"])
#             }};
#         """) or {}
        
#         link = page_data.get('url') or driver.current_url
#         title = page_data.get('title')
#         company = page_data.get('company')
#         location = page_data.get('location')
#         desc_text = page_data.get('desc_text')
#         desc_html = page_data.get('desc_html')

#         print(f"  -> [{i+1}/{num_cards}] URL: {link[:80]}")

#         if not title or not company: 
#             print(f"  -> SKIPPED (missing title or company)")
#             continue
            
#         job_data = {
#             'jobId': make_stable_job_id('Glassdoor', link, i),
#             'jobTitle': title[:500],
#             'companyName': company[:255],
#             'companyLogo': get_company_logo(company, driver),
#             'companyLocation': location[:255] if location else "United States",
#             'jobLocation': location[:255] if location else "United States",
#             'jobType': 'Full-time',
#             'yearOfExperience': extract_experience_from_text(desc_text),
#             'skills': extract_skills_from_text(desc_text),
#             'jobPostTime': datetime.now().isoformat(),
#             'jobDescription': desc_html,
#             'salary': extract_salary_from_text(desc_text),
#             'category': category,
#             'jobSource': 'Glassdoor',
#             'jobUrl': link,
#             'createdAt': datetime.now().isoformat(),
#             'updatedAt': datetime.now().isoformat()
#         }
#         jobs.append(job_data)
#         if on_job_scraped:
#             on_job_scraped(category, "Glassdoor", job_data)
#     return jobs

# def scrape_naukri_jobs(driver, url, category, on_count=None, on_job_scraped=None, count_only=False):
#     print(f"\n[Naukri - {category}] Fetching USA job links...")
#     driver.get(url)
#     time.sleep(5)
#     anchors = driver.find_elements(By.CSS_SELECTOR, "a.title")
#     links = list(set([a.get_attribute("href") for a in anchors if a.get_attribute("href")]))[:NAUKRI_JOBS_PER_URL]
#     available_reported = get_platform_available_job_count(driver, "Naukri")
#     print(f"  [COUNT] Naukri available reported: '{available_reported}' | Scrapable links found: {len(links)}")
#     if on_count:
#         on_count(category, "Naukri", available_reported, len(links))

#     if count_only or not links:
#         return []

#     print(f"[Naukri] Found {len(links)} links. Beginning deep scrape...")
#     jobs = []
#     for i, link in enumerate(links):
#         driver.get(link)
#         time.sleep(3)
#         title = get_element_text(driver, ["h1", "h1.jd-header-title"])
#         company = get_element_text(driver, ["div.jd-header-comp-name a"])
#         desc_text, desc_html = get_element_data(driver, ["div.job-desc"])
        

#         if not title or not company: continue
#         job_data = {
#             'jobId': make_stable_job_id('Naukri', link, i),
#             'jobTitle': title[:500],
#             'companyName': company[:255],
#             'companyLogo': get_company_logo(company, driver),
#             'companyLocation': "United States",
#             'jobLocation': "United States",
#             'jobType': 'Full-time',
#             'yearOfExperience': extract_experience_from_text(desc_text),
#             'skills': extract_skills_from_text(desc_text),
#             'jobPostTime': datetime.now().isoformat(),
#             'jobDescription': desc_html if desc_html else "",
#             'salary': extract_salary_from_text(desc_text),
#             'category': category,
#             'jobSource': 'Naukri',
#             'jobUrl': (get_final_job_url(driver, link) or link),

#             'createdAt': datetime.now().isoformat(),
#             'updatedAt': datetime.now().isoformat()
#         }
#         jobs.append(job_data)
#         if on_job_scraped:
#             on_job_scraped(category, "Naukri", job_data)
#     return jobs

# ---------HIRING CAFE SCRAPPER ----------

def scrape_reed_jobs(driver, url, category, on_count=None, on_job_scraped=None, count_only=False):
    print(f"\n[Reed - {category}] Fetching UK jobs (max {REED_JOBS_PER_URL})...")
    jobs = []
    
    try:
        from bs4 import BeautifulSoup
        
        links = []
        page = 1
        
        while len(links) < REED_JOBS_PER_URL:
            page_url = url if page == 1 else f"{url}&pageno={page}"
            driver.get(page_url)
            time.sleep(3)
            soup = BeautifulSoup(driver.page_source, 'html.parser')
            
            page_links = []
            for article in soup.find_all('article', class_='job-result-card'):
                a_tag = article.find('a', href=True)
                if not a_tag:
                    h2 = article.find('h2')
                    a_tag = h2.find('a', href=True) if h2 else None
                
                if a_tag and '/jobs/' in a_tag['href']:
                    full_url = "https://www.reed.co.uk" + a_tag['href']
                    if full_url not in links and full_url not in page_links:
                        page_links.append(full_url)
                        
            if not page_links:
                for h2 in soup.find_all('h2'):
                    a = h2.find('a')
                    if a and '/jobs/' in a['href']:
                        full_url = "https://www.reed.co.uk" + a['href']
                        if full_url not in links and full_url not in page_links:
                            page_links.append(full_url)
                            
            if not page_links:
                print(f"  [Reed] Reached end of results at page {page}")
                break
                
            links.extend(page_links)
            page += 1
            
        available = len(links)
        scrapable = min(available, REED_JOBS_PER_URL)
        if on_count:
            on_count(category, "Reed", str(available), scrapable)
            
        if count_only or scrapable == 0:
            return []
            
        for i, link in enumerate(links[:REED_JOBS_PER_URL]):
            print(f"  [{i+1}/{scrapable}] Fetching Reed job: {link.split('?')[0]}")
            try:
                headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
                resp = _requests.get(link, headers=headers, timeout=10)
                job_soup = BeautifulSoup(resp.text, 'html.parser')
                
                next_data = job_soup.find('script', id='__NEXT_DATA__')
                if not next_data:
                    print("    [WARN] No __NEXT_DATA__ found for this job")
                    continue
                    
                data = json.loads(next_data.string)
                c_job = data.get('props', {}).get('pageProps', {}).get('consolidatedJobDetails', {})
                jd = c_job.get('jobDetails', {})
                
                title = (jd.get('title') or '').strip()
                owner = jd.get('jobOwner') or {}
                company = (owner.get('profileName') or 'Unknown').strip()
                if not title:
                    continue
                    
                desc_html = jd.get('description', '')
                desc_text = BeautifulSoup(desc_html, 'html.parser').get_text(separator=' ').strip()
                
                location = jd.get('jobLocation', {}).get('town', 'UK')
                
                job_salary = jd.get('jobSalary', {})
                min_sal = job_salary.get('minSalary')
                max_sal = job_salary.get('maxSalary')
                currency = job_salary.get('currency', '£')
                salary_str = ""
                if min_sal and max_sal:
                    salary_str = f"{currency}{min_sal} - {currency}{max_sal}"
                elif min_sal:
                    salary_str = f"{currency}{min_sal}"
                
                job_info = {
                    'jobId': make_stable_job_id('Reed', link, i),
                    'jobTitle': title[:500],
                    'companyName': company[:255],
                    'companyLogo': "",
                    'companyLocation': location[:255] if location else "United Kingdom",
                    'jobLocation': location[:255] if location else "United Kingdom",
                    'jobType': 'Full-time',
                    'yearOfExperience': extract_experience_from_text(desc_text),
                    'skills': extract_skills_from_text(desc_text),
                    'jobPostTime': datetime.now().isoformat(),
                    'jobDescription': desc_html,
                    'salary': salary_str or extract_salary_from_text(desc_text),
                    'category': category,
                    'jobSource': 'Reed',
                    'jobUrl': link,
                    'createdAt': datetime.now().isoformat(),
                    'updatedAt': datetime.now().isoformat()
                }
                
                jobs.append(job_info)
                if on_job_scraped:
                    on_job_scraped(category, "Reed", job_info)
                    
            except Exception as ex:
                print(f"    [ERROR] fetching detail: {ex}")
                
            time.sleep(random.uniform(0.5, 1.5))
            
    except Exception as e:
        print(f"  [ERROR] Scraping Reed search page: {e}")
        
    return jobs

def scrape_hiring_cafe_jobs(driver, url, category, on_count=None, on_job_scraped=None, count_only=False):
    print(f"\n[HiringCafe - {category}] Fetching jobs via curl_cffi session...")

    if not _CURL_CFFI_AVAILABLE:
        print("  [HiringCafe] Error: curl_cffi is required.")
        return []

    target_url = url if (url and url.startswith("http")) else f"https://hiringcafe.com/?searchState=%7B%22searchQuery%22%3A%22{category.replace(' ', '+')}%22%7D"

    # Strategy: cycle through ALL impersonations with a persistent session.
    # A session carries cookies so Cloudflare's challenge cookie is reused once earned.
    # On Render the IP is datacenter — we try every fingerprint before giving up.
    IMPERSONATE_ORDER = [
        "safari17_0", "chrome124", "safari15_3",
        "chrome120", "chrome116", "edge101",
    ]

    def _extract_jobs_from_html(page_content):
        """Parse __NEXT_DATA__ and return list of job dicts, or None on failure."""
        if "__NEXT_DATA__" not in page_content:
            return None
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(page_content, 'html.parser')
        script_tag = soup.find('script', id='__NEXT_DATA__')
        if not script_tag:
            return None
        data = json.loads(script_tag.string)
        hits = data.get('props', {}).get('pageProps', {}).get('ssrHits', [])
        return hits

    # ── Phase 1: curl_cffi session cycling ─────────────────────────────────────
    session = cffi_requests.Session()
    page_content = None
    succeeded_impersonation = None

    for imp in IMPERSONATE_ORDER:
        try:
            print(f"  [HiringCafe] Trying impersonation: {imp}...")
            r = session.get(target_url, impersonate=imp, timeout=20)
            if r.status_code == 200 and "__NEXT_DATA__" in r.text:
                page_content = r.text
                succeeded_impersonation = imp
                print(f"  [HiringCafe] Bypass SUCCESS with {imp}!")
                break
            else:
                print(f"  [HiringCafe] {imp} blocked (HTTP {r.status_code}). Trying next...")
                time.sleep(random.uniform(2.0, 3.5))
        except Exception as e:
            print(f"  [HiringCafe] {imp} error: {e}. Trying next...")
            time.sleep(1.0)

    # ── Phase 2: Selenium fallback (last resort) ────────────────────────────────
    if not page_content and driver:
        print("  [HiringCafe] All curl_cffi impersonations blocked. Trying Selenium...")
        try:
            driver.delete_all_cookies()
            driver.get(target_url)
            time.sleep(random.uniform(6.0, 9.0))
            page_content = driver.page_source
            if "__NEXT_DATA__" in page_content:
                print("  [HiringCafe] Selenium bypass SUCCESS!")
            else:
                print("  [HiringCafe] Selenium also blocked. Giving up.")
                return []
        except Exception as selenium_e:
            print(f"  [HiringCafe] Selenium fallback failed: {selenium_e}")
            return []

    if not page_content or "__NEXT_DATA__" not in page_content:
        print("  [HiringCafe] Could not retrieve page. Returning empty.")
        return []

    hits = _extract_jobs_from_html(page_content)
    if not hits:
        print("  [HiringCafe] JSON payload was empty or unparseable.")
        return []

    print(f"  [HiringCafe] Extracted {len(hits)} perfectly structured jobs!")

    if count_only:
        if on_count: on_count(category, "HiringCafe", str(len(hits)), len(hits))
        return []

    # Use the succeeded impersonation for any follow-up API calls
    imp = succeeded_impersonation or "safari17_0"
    jobs = []
    for i, hit in enumerate(hits[:HIRINGCAFE_JOBS_PER_URL]):
        v5 = hit.get('v5_processed_job_data', {})
        info = hit.get('job_information', {})

        raw_id = hit.get('id') or hit.get('job_id')
        title = info.get('title') or v5.get('core_job_title') or "Software Engineer"
        company = v5.get('company_name') or hit.get('enriched_company_data', {}).get('name') or "Unknown"
        location = v5.get('formatted_workplace_location') or "United States"

        commitment = v5.get('commitment', ['Full Time'])
        workplace_type = v5.get('workplace_type', 'Remote')
        job_type = f"{workplace_type} - {commitment[0]}" if commitment else workplace_type

        yoe = v5.get('min_industry_and_role_yoe')
        experience = f"{yoe}+ Years" if yoe is not None else None

        skills = v5.get('technical_tools', [])
        if not skills:
            skills = extract_skills_from_text(v5.get('requirements_summary', ''))

        post_time = v5.get('estimated_publish_date') or datetime.now().isoformat()

        salary = None
        if v5.get('yearly_min_compensation') and v5.get('yearly_max_compensation'):
            salary = f"${v5['yearly_min_compensation']} - ${v5['yearly_max_compensation']}"

        job_url = hit.get('apply_url')
        if not job_url and raw_id:
            slug = re.sub(r"[^a-zA-Z0-9-]", "-", title.lower())
            job_url = f"https://hiringcafe.com/job/{slug}-{raw_id}"

        domain = hit.get('enriched_company_data', {}).get('homepage_uri')
        logo_url = None
        if domain:
            logo_url = f"https://s2.googleusercontent.com/s2/favicons?domain={domain}&sz=128"

        # Fetch full description from API if not present
        full_desc_html = hit.get('job_information', {}).get('description') or v5.get('description') or v5.get('original_description')
        if not full_desc_html and raw_id:
            for jd_attempt in range(3):
                try:
                    jd_url = f"https://hiringcafe.com/api/job-description?id={raw_id}"
                    jd_r = session.get(jd_url, impersonate=imp, timeout=8)
                    if jd_r.status_code == 200:
                        fetched_desc = jd_r.json().get('job', {}).get('job_information', {}).get('description')
                        if fetched_desc:
                            full_desc_html = fetched_desc
                        break
                except Exception:
                    if jd_attempt < 2:
                        time.sleep(1)
                    pass

        final_desc = full_desc_html or v5.get('requirements_summary', '')

        job_data = {
            "jobId":            make_stable_job_id('HiringCafe', job_url, i),
            "jobTitle":         title[:500],
            "companyName":      company[:255],
            "companyLogo":      logo_url,
            "companyLocation":  location[:255],
            "jobLocation":      location[:255],
            "jobType":          job_type[:50],
            "yearOfExperience": experience,
            "skills":           skills,
            "jobPostTime":      post_time,
            "jobDescription":   final_desc,
            "salary":           salary,
            "category":         category,
            "jobSource":        "HiringCafe",
            "jobUrl":           (job_url or "")[:1000],
            "createdAt":        datetime.now().isoformat(),
            "updatedAt":        datetime.now().isoformat(),
        }

        if not final_desc or len(final_desc.strip()) < 200:
            print(f"  [HiringCafe] Skipping job {title[:30]} - description too short ({len(final_desc) if final_desc else 0} chars)")
            continue

        jobs.append(job_data)
        if on_job_scraped: on_job_scraped(category, "HiringCafe", job_data)

    print(f"\n[HiringCafe] Done: {len(jobs)} jobs collected for '{category}'")
    return jobs


#---------INDEED SCRAPPER ----------

_INDEED_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_indeed_session = None

def _get_indeed_session():
    # Deprecated: Using cffi_requests.get directly like HiringCafe to avoid TLS fingerprint mismatch.
    return None

# -- curl_cffi fetchers (HiringCafe Approach) --

def _fetch_indeed_listing_page_curl(session, url, start=0):
    from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    
    # Safely overwrite or remove the start parameter to prevent duplicates like &start=10&start=10
    if start > 0:
        qs['start'] = [str(start)]
    else:
        qs.pop('start', None)
        
    new_query = urlencode(qs, doseq=True)
    page_url = urlunparse(parsed._replace(query=new_query))

    try:
        # Use exact same approach as HiringCafe: direct get with impersonate="chrome120"
        resp = cffi_requests.get(page_url, impersonate="chrome120", timeout=30)
        resp.raise_for_status()
    except Exception as e:
        print(f"  [Indeed-CURL] Listing page fetch failed: {e}")
        return []

    m = re.search(r'window\.mosaic\.providerData\["mosaic-provider-jobcards"\]\s*=\s*(\{.*?\});', resp.text, re.DOTALL)
    if not m:
        print("  [Indeed-CURL] Could not find mosaic-provider-jobcards in HTML.")
        return []
    
    try:
        data = json.loads(m.group(1))
        jobs = data.get('metaData', {}).get('mosaicProviderJobCardsModel', {}).get('results', [])
        return jobs
    except Exception as e:
        print(f"  [Indeed-CURL] Failed to parse mosaic JSON: {e}")
        return []

def _fetch_descriptions_rpc_curl(session, job_keys):
    if not job_keys:
        return {}
    url = f"https://www.indeed.com/rpc/jobdescs?jks={','.join(job_keys)}"
    try:
        resp = cffi_requests.get(url, impersonate="chrome120", timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"  [Indeed-CURL] RPC description fetch failed: {e}")
        return {}


# -- Selenium fetchers --

def _fetch_indeed_listing_page_selenium(driver, url, start=0):
    from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    
    if start > 0:
        qs['start'] = [str(start)]
    else:
        qs.pop('start', None)
        
    new_query = urlencode(qs, doseq=True)
    page_url = urlunparse(parsed._replace(query=new_query))

    try:
        # For paginated pages (start > 0), warm up the session by loading
        # the base page first (like a real user would), then navigate to the
        # target page. Direct deep-link navigation to start=N often triggers
        # Cloudflare because real users always land on page 1 first.
        if start > 0:
            driver.get(url)
            time.sleep(random.uniform(4, 7))  # let page 1 fully load
        driver.get(page_url)
        time.sleep(random.uniform(6, 10))  # give paginated page more time
        html = driver.page_source
    except Exception as e:
        print(f"  [Indeed-Selenium] Listing page fetch failed: {e}")
        return []

    m = re.search(r'window\.mosaic\.providerData\["mosaic-provider-jobcards"\]\s*=\s*(\{.*?\});', html, re.DOTALL)
    if not m:
        print("  [Indeed-Selenium] Could not find mosaic-provider-jobcards in HTML.")
        return []
    
    try:
        data = json.loads(m.group(1))
        jobs = data.get('metaData', {}).get('mosaicProviderJobCardsModel', {}).get('results', [])
        return jobs
    except Exception as e:
        print(f"  [Indeed-Selenium] Failed to parse mosaic JSON: {e}")
        return []

def _fetch_descriptions_rpc_selenium(driver, job_keys):
    if not job_keys:
        return {}
    url = f"https://www.indeed.com/rpc/jobdescs?jks={','.join(job_keys)}"
    try:
        driver.get(url)
        time.sleep(random.uniform(2, 4))
        json_text = driver.execute_script("return document.body.innerText;")
        if json_text:
            return json.loads(json_text)
        return {}
    except Exception as e:
        print(f"  [Indeed-Selenium] RPC description fetch failed: {e}")
        return {}


def _sanitize_indeed_url(url):
    """
    Strip noisy params copied from browser (vjk, from) from Indeed URLs.
    - vjk= : View Job Key — forces Indeed to show a single job detail panel, not a search results list
    - from= : tracking param — not needed and can trigger bot detection
    - start= is KEPT intentionally — used as the base page offset for pagination
      e.g. Indeed2 with start=10 scrapes from page 2 onwards (unique jobs vs Indeed page 1)
    """
    from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    for key in ["vjk", "from"]:
        params.pop(key, None)
    clean_query = urlencode({k: v[0] for k, v in params.items()})
    clean_url = urlunparse(parsed._replace(query=clean_query))
    if clean_url != url:
        print(f"  [Indeed] URL sanitized (removed vjk/from params)")
        print(f"    Original : {url[:100]}")
        print(f"    Sanitized: {clean_url[:100]}")
    return clean_url


def _extract_indeed_base_start(url):
    """Extract start= from URL as an integer (default 0), return (clean_url_without_start, base_start)."""
    from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    base_start = int(params.pop("start", ["0"])[0])
    clean_query = urlencode({k: v[0] for k, v in params.items()})
    base_url = urlunparse(parsed._replace(query=clean_query))
    return base_url, base_start


def scrape_indeed_jobs(driver, url, category,
                       on_count=None, on_job_scraped=None,
                       count_only=False, max_jobs=INDEED_JOBS_PER_URL):
    """
    Indeed scraper that tries curl_cffi first, and falls back to Selenium if blocked.
    start= in the URL is used as the base page offset so multiple Indeed URLs
    can cover different page ranges without duplicate jobs.
    """
    # Strip vjk/from — but KEEP start= to honor the intended page offset
    url = _sanitize_indeed_url(url)
    # Extract start= as base offset, remove it from base URL (we'll append the right offsets ourselves)
    base_url, base_start = _extract_indeed_base_start(url)

    print(f"\n{'='*60}")
    print(f"[Indeed] Scraping: {category}")
    print(f"  URL: {base_url[:80]}")
    if base_start > 0:
        print(f"  Starting from page offset {base_start} (page {base_start // 10 + 1})")
    print(f"{'='*60}")

    use_selenium = False
    stubs = []

    if _CURL_CFFI_AVAILABLE:
        print(f"\n  [Indeed-CURL] Fetching listing page (start={base_start})...")
        stubs = _fetch_indeed_listing_page_curl(None, base_url, start=base_start)

    if not stubs:
        print("  [Indeed] curl_cffi returned 0 jobs (likely blocked by Cloudflare). Switching to Selenium fallback...")
        use_selenium = True
        print(f"  [Indeed-Selenium] Fetching listing page (start={base_start})...")
        stubs = _fetch_indeed_listing_page_selenium(driver, base_url, start=base_start)

    available_reported = "Unknown"
    print(f"  [COUNT] Page 1 stubs: {len(stubs)}")
    if on_count:
        on_count(category, "Indeed", available_reported, min(len(stubs), max_jobs))

    if count_only or not stubs:
        return []

    all_stubs = list(stubs)
    start_offset = base_start + 10

    # Step 2: Paginate from base_start onwards
    while len(all_stubs) < max_jobs:
        time.sleep(random.uniform(4.0, 8.0))
        if use_selenium:
            print(f"  [Indeed-Selenium] Fetching listing page offset {start_offset}...")
            new_stubs = _fetch_indeed_listing_page_selenium(driver, base_url, start=start_offset)
        else:
            print(f"  [Indeed-CURL] Fetching listing page offset {start_offset}...")
            new_stubs = _fetch_indeed_listing_page_curl(None, base_url, start=start_offset)
            
        if not new_stubs:
            print(f"  No more stubs found at offset {start_offset}.")
            break
        all_stubs.extend(new_stubs)
        start_offset += 10

    all_stubs = all_stubs[:max_jobs]
    print(f"\n  [Indeed] Fetching full descriptions for {len(all_stubs)} jobs via RPC...")

    # Step 3: Fetch descriptions in batches
    job_keys = [s.get("jobkey") for s in all_stubs if s.get("jobkey")]
    descriptions_map = {}
    
    chunk_size = 20
    for i in range(0, len(job_keys), chunk_size):
        chunk = job_keys[i:i+chunk_size]
        print(f"    -> Fetching descriptions batch {i//chunk_size + 1} ({len(chunk)} jobs)...")
        if use_selenium:
            desc_data = _fetch_descriptions_rpc_selenium(driver, chunk)
        else:
            desc_data = _fetch_descriptions_rpc_curl(None, chunk)
        descriptions_map.update(desc_data)
        time.sleep(1.0)

    # Step 4: Build normalized records
    jobs = []
    for idx, stub in enumerate(all_stubs):
        jobkey = stub.get("jobkey")
        title = stub.get("displayTitle") or stub.get("title", "")
        company = stub.get("company", "")
        location = stub.get("formattedLocation", "")
        
        salary = None
        extracted_salary = stub.get("extractedSalary")
        if extracted_salary:
            if extracted_salary.get("max") and extracted_salary.get("min"):
                salary = f"${extracted_salary['min']:,.0f} - ${extracted_salary['max']:,.0f} {extracted_salary.get('type', '')}".strip()
        if not salary:
            salary = stub.get("salarySnippet", {}).get("text")
            
        desc_html = descriptions_map.get(jobkey, "")
        desc_text = re.sub(r'<[^>]+>', ' ', desc_html) if desc_html else (stub.get("snippet") or "")
        
        skills = extract_skills_from_text(desc_text)
        
        record = {
            "jobId":            f"indeed_{jobkey}",
            "jobTitle":         title[:500],
            "companyName":      company[:255],
            "companyLogo":      None,
            "companyLocation":  location[:255],
            "jobLocation":      location[:255],
            "jobType":          "Full-time",
            "yearOfExperience": extract_experience_from_text(desc_text),
            "skills":           skills,
            "jobPostTime":      datetime.now().isoformat(),
            "jobDescription":   desc_html,
            "salary":           salary,
            "category":         category,
            "jobSource":        "Indeed",
            "jobUrl":           f"https://www.indeed.com/viewjob?jk={jobkey}",
            "createdAt":        datetime.now().isoformat(),
            "updatedAt":        datetime.now().isoformat(),
        }
        
        jobs.append(record)
        if on_job_scraped:
            on_job_scraped(category, "Indeed", record)

    print(f"\n[Indeed] Done: {len(jobs)} jobs collected for '{category}'")
    return jobs



def save_to_json(jobs, filename=None, category=None):
    """
    Saves a list of job dictionaries to a JSON file.
    - If filename is provided, saves directly to that file.
    - If category is provided without filename, saves to jobs_<category>_latest.json.
    - Otherwise defaults to jobs_scraped_latest.json.
    If the file already exists, merges jobs while updating existing ones by jobId/jobUrl.
    """
    if not jobs:
        print("[JSON] No jobs to save.")
        return

    if not isinstance(jobs, list):
        jobs = [jobs]

    if not filename:
        if category:
            safe_cat = str(category).lower().replace(" ", "_").replace("/", "_").replace(".", "_")
            filename = f"jobs_{safe_cat}_latest.json"
        else:
            filename = "jobs_scraped_latest.json"

    # Ensure parent directory exists if filename contains directory path
    dir_name = os.path.dirname(filename)
    if dir_name and not os.path.exists(dir_name):
        try:
            os.makedirs(dir_name, exist_ok=True)
        except Exception:
            pass

    try:
        existing_jobs = []
        if os.path.exists(filename):
            try:
                with open(filename, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        existing_jobs = data
            except Exception:
                existing_jobs = []

        # Map existing jobs by stable key (jobId or canonical jobUrl)
        seen_keys = {}
        for idx, ej in enumerate(existing_jobs):
            key = ej.get("jobId") or canonical_url(ej.get("jobUrl", ""))
            if key:
                seen_keys[key] = idx

        # Merge or update
        for j in jobs:
            key = j.get("jobId") or canonical_url(j.get("jobUrl", ""))
            if key and key in seen_keys:
                existing_jobs[seen_keys[key]] = j
            else:
                existing_jobs.append(j)
                if key:
                    seen_keys[key] = len(existing_jobs) - 1

        with open(filename, "w", encoding="utf-8") as f:
            json.dump(existing_jobs, f, indent=4, ensure_ascii=False, default=str)

        print(f"[JSON] Saved {len(jobs)} jobs into '{filename}' (Total in file: {len(existing_jobs)})")
    except Exception as e:
        print(f"[JSON] Error saving jobs to '{filename}': {e}")

def export_all_scraped_jobs(all_scraped_jobs, output_prefix=""):
    if not all_scraped_jobs:
        print("\n[WARN] No jobs collected to export in consolidated JSON.")
        return

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    prefix     = f"{output_prefix}_" if output_prefix else ""
    out_file_latest   = f"jobs_{prefix}all_categories_latest.json" if output_prefix else "jobs_all_categories_latest.json"
    out_file_archived = f"json_archives/jobs_{prefix}all_categories_{timestamp}.json"

    try:
        with open(out_file_latest, 'w', encoding='utf-8') as f:
            json.dump(all_scraped_jobs, f, indent=2, ensure_ascii=False, default=str)

        with open(out_file_archived, 'w', encoding='utf-8') as f:
            json.dump(all_scraped_jobs, f, indent=2, ensure_ascii=False, default=str)

        print(f"[SUCCESS] Exported ALL {len(all_scraped_jobs)} consolidated jobs -> {out_file_latest} and archive")
    except Exception as e:
        print(f"[ERROR] Failed to export consolidated JSON: {e}")

def count_and_scrape_with_progress(config_file=None, progress_file=PROGRESS_FILE_PATH, count_only=False, output_prefix=""):
    global CURRENT_REGION
    if not config_file:
        config_file = sys.argv[1] if (len(sys.argv) > 1 and not sys.argv[1].startswith("-")) else "scraping_links.json"
        
    if 'uk' in config_file.lower():
        CURRENT_REGION = "UK"
    else:
        CURRENT_REGION = "US"

    # ─── HARDCODED PRODUCTION CONFIG (fallback when scraping_links.json is absent) ───
    # This ensures the scraper ALWAYS runs on Render even if the JSON file is missing.
    # Keep this in sync with scraping_links.json!
    HARDCODED_CONFIG = {
        "Software Developer/Engineer": {
            "LinkedIn": "https://www.linkedin.com/jobs/search/?distance=25&f_TPR=r86400&geoId=103644278&keywords=%22software%20engineer%22&origin=JOB_SEARCH_PAGE_SEARCH_BUTTON&refresh=true&sortBy=DD",
            "Glassdoor": "https://www.glassdoor.co.in/Job/united-states-senior-software-engineer-jobs-SRCH_IL.0,13_IN1_KO14,38.htm?minRating=3.0&fromAge=1&maxSalary=249000&minSalary=103000"
        },
        "Product Engineer": {
            "LinkedIn": "https://www.linkedin.com/jobs/search/?keywords=product%20engineer&f_TPR=r86400&sortBy=DD",
            "SimplyHired": "https://www.simplyhired.com/search?q=product+engineer&l=United+States&t=1"
        },
        "Devops/Cloud Engineer": {
            "LinkedIn": "https://www.linkedin.com/jobs/search/?keywords=cloud%20engineer&location=United%20States&f_TPR=r86400&sortBy=DD",
            "SimplyHired": "https://www.simplyhired.com/search?q=cloud+engineer&l=United+States&t=1",
            "Glassdoor": "https://www.glassdoor.co.in/Job/united-states-cloud-engineer-jobs-SRCH_IL.0,13_IN1_KO14,28.htm?fromAge=1"
        },
        "Java Engineer": {
            "LinkedIn": "https://www.linkedin.com/jobs/search/?f_E=2%2C3%2C4&f_TPR=r86400&geoId=103644278&keywords=java%20software%20engineer&sortBy=DD",
            "SimplyHired": "https://www.simplyhired.com/search?q=java+engineer&l=United+States&t=1",
            "Glassdoor": "https://www.glassdoor.co.in/Job/united-states-java-developer-jobs-SRCH_IL.0,13_IN1_KO14,28.htm?fromAge=1"
        },
        "Product Manager": {
            "LinkedIn": "https://www.linkedin.com/jobs/search/?f_E=3%2C4&f_TPR=r86400&keywords=Product%20Manager&sortBy=DD",
            "LinkedIn2": "https://www.linkedin.com/jobs/search/?f_E=2%2C3%2C4&f_TPR=r86400&geoId=105080838&keywords=%22Product%20Manager%22&origin=JOB_SEARCH_PAGE_LOCATION_AUTOCOMPLETE&refresh=true",
            "LinkedIn3": "https://www.linkedin.com/jobs/search/?distance=25.0&f_E=2%2C3%2C4&f_TPR=r604800&f_WT=1%2C2%2C3&geoId=103644278&keywords=product%20manager&origin=JOBS_HOME_KEYWORD_HISTORY",
            "SimplyHired": "https://www.simplyhired.com/search?q=product+manager&l=United+States&t=1"
        },
        "Data Analyst": {
            "LinkedIn": "https://www.linkedin.com/jobs/search/?distance=25.0&f_AL=true&f_E=2%2C3%2C4&f_TPR=r86400&geoId=103644278&keywords=%22Data%20Analyst%22&origin=JOB_SEARCH_PAGE_SEARCH_BUTTON&refresh=true&spellCorrectionEnabled=true",
            "SimplyHired": "https://www.simplyhired.com/search?q=data+analyst&l=United+States&t=1",
            "HiringCafe": "https://hiring.cafe/?searchState=%7B%22searchQuery%22%3A%22data+analyst%22%2C%22departments%22%3A%5B%22Data+and+Analytics%22%5D%2C%22dateFetchedPastNDays%22%3A2%2C%22applicationFormEase%22%3A%5B%22Time+Consuming%22%5D%2C%22locations%22%3A%5B%7B%22id%22%3A%22FxY1yZQBoEtHp_8UEq7V%22%2C%22types%22%3A%5B%22country%22%5D%2C%22address_components%22%3A%5B%7B%22long_name%22%3A%22United+States%22%2C%22short_name%22%3A%22US%22%2C%22types%22%3A%5B%22country%22%5D%7D%5D%2C%22formatted_address%22%3A%22United+States%22%2C%22population%22%3A327167434%2C%22workplace_types%22%3A%5B%5D%2C%22options%22%3A%7B%22flexible_regions%22%3A%5B%22anywhere_in_continent%22%2C%22anywhere_in_world%22%5D%7D%7D%5D%2C%22roleYoeRange%22%3A%5B0%2C6%5D%2C%22managementYoeRange%22%3A%5B0%2C6%5D%2C%22securityClearances%22%3A%5B%22None%22%5D%7D"
        },
        "Frontend Developer": {
            "LinkedIn": "https://www.linkedin.com/jobs/search/?keywords=frontend%20developer&location=United%20States&f_TPR=r86400&sortBy=DD",
            "HiringCafe": "https://hiring.cafe/?searchState=%7B%22locations%22%3A%5B%7B%22id%22%3A%22FxY1yZQBoEtHp_8UEq7V%22%2C%22types%22%3A%5B%22country%22%5D%2C%22address_components%22%3A%5B%7B%22long_name%22%3A%22United+States%22%2C%22short_name%22%3A%22US%22%2C%22types%22%3A%5B%22country%22%5D%7D%5D%2C%22formatted_address%22%3A%22United+States%22%2C%22population%22%3A327167434%2C%22workplace_types%22%3A%5B%5D%2C%22options%22%3A%7B%22flexible_regions%22%3A%5B%22anywhere_in_continent%22%2C%22anywhere_in_world%22%5D%7D%7D%5D%2C%22dateFetchedPastNDays%22%3A2%2C%22seniorityLevel%22%3A%5B%22No+Prior+Experience+Required%22%2C%22Entry+Level%22%2C%22Mid+Level%22%5D%2C%22excludeAllLicensesAndCertifications%22%3Atrue%2C%22securityClearances%22%3A%5B%22None%22%5D%2C%22searchQuery%22%3A%22frontend+Engineer%22%7D"
        },
        "Backend Developer": {
            "LinkedIn": "https://www.linkedin.com/jobs/search/?keywords=backend%20developer&location=United%20States&f_TPR=r86400&sortBy=DD"
        },
        "Fullstack Developer": {
            "LinkedIn": "https://www.linkedin.com/jobs/search/?keywords=fullstack%20developer&location=United%20States&f_TPR=r86400&sortBy=DD"
        },
        "ML Engineer": {
            "LinkedIn": "https://www.linkedin.com/jobs/search/?keywords=machine%20learning%20engineer&location=United%20States&f_TPR=r86400&sortBy=DD"
        },
        "AI Engineer": {
            "LinkedIn": "https://www.linkedin.com/jobs/search/?keywords=artificial%20intelligence%20engineer&location=United%20States&f_TPR=r86400&sortBy=DD"
        },
        "Business Analyst": {
            "LinkedIn": "https://www.linkedin.com/jobs/search/?f_TPR=r86400&keywords=business%20analyst&origin=JOB_SEARCH_PAGE_JOB_FILTER&sortBy=DD",
            "Glassdoor": "https://www.glassdoor.com/Job/united-states-busisness-analyst-jobs-SRCH_IL.0,13_IN1_KO14,31.htm?minRating=4.0&fromAge=1",
            "HiringCafe": "https://hiring.cafe/?searchState=%7B%22locations%22%3A%5B%7B%22id%22%3A%22FxY1yZQBoEtHp_8UEq7V%22%2C%22types%22%3A%5B%22country%22%5D%2C%22address_components%22%3A%5B%7B%22long_name%22%3A%22United+States%22%2C%22short_name%22%3A%22US%22%2C%22types%22%3A%5B%22country%22%5D%7D%5D%2C%22formatted_address%22%3A%22United+States%22%2C%22population%22%3A327167434%2C%22workplace_types%22%3A%5B%5D%2C%22options%22%3A%7B%22flexible_regions%22%3A%5B%22anywhere_in_continent%22%2C%22anywhere_in_world%22%5D%7D%7D%5D%2C%22searchQuery%22%3A%22business+analyst%22%2C%22dateFetchedPastNDays%22%3A29%2C%22applicationFormEase%22%3A%5B%22Time+Consuming%22%5D%7D",
            "Indeed": "https://www.indeed.com/jobs?q=business+analyst&l=United+States&fromage=1&from=searchOnDesktopSerp"
        },
        "Data Engineer": {
            "LinkedIn": "https://www.linkedin.com/jobs/search/?distance=25.0&f_AL=true&f_E=2%2C3%2C4&f_TPR=r86400&geoId=103644278&keywords=data%20engineer&origin=JOB_SEARCH_PAGE_KEYWORD_AUTOCOMPLETE&refresh=true&spellCorrectionEnabled=true",
            "Glassdoor": "https://www.glassdoor.co.in/Job/united-states-data-engineer-jobs-SRCH_IL.0,13_IN1_KO14,27.htm?fromAge=1"
        },
        "Cyber Security": {
            "LinkedIn": "https://www.linkedin.com/jobs/search/?f_E=2%2C3%2C4&f_JT=F%2CC&f_TPR=r18000&f_WT=1%2C2%2C3&keywords=(%22Cybersecurity%20Engineer%22%20OR%20%22Security%20Engineer%22%20OR%20%22Information%20Security%20Engineer%22%20OR%20%22Cloud%20Security%20Engineer%22%20OR%20%22Network%20Security%20Engineer%22%20OR%20%22Application%20Security%20Engineer%22%20OR%20%22SOC%20Engineer%22%20OR%20%22Security%20Analyst%22%20OR%20%22Cyber%20Security%20Analyst%22)%20AND%20(SIEM%20OR%20EDR%20OR%20XDR%20OR%20%22Incident%20Response%22%20OR%20%22Threat%20Detection%22%20OR%20%22Threat%20Hunting%22%20OR%20Splunk%20OR%20Sentinel%20OR%20CrowdStrike%20OR%20Defender%20OR%20%22IAM%22%20OR%20%22Identity%20and%20Access%20Management%22%20OR%20%22Vulnerability%20Management%22%20OR%20NIST%20OR%20ISO27001)&location=United%20States&origin=JOB_SEARCH_PAGE_JOB_FILTER&sortBy=DD"
        },
        "Financial Analyst": {
            "LinkedIn": "https://www.linkedin.com/jobs/search/?keywords=financial%20analyst&location=United%20States&f_TPR=r86400&f_AL=true&sortBy=DD",
            "Glassdoor": "https://www.glassdoor.co.in/Job/united-states-financial-analyst-jobs-SRCH_IL.0,13_IN1_KO14,31.htm?fromAge=1",
            "HiringCafe": "https://hiring.cafe/?searchState=%7B%22locations%22%3A%5B%7B%22id%22%3A%22FxY1yZQBoEtHp_8UEq7V%22%2C%22types%22%3A%5B%22country%22%5D%2C%22address_components%22%3A%5B%7B%22long_name%22%3A%22United+States%22%2C%22short_name%22%3A%22US%22%2C%22types%22%3A%5B%22country%22%5D%7D%5D%2C%22formatted_address%22%3A%22United+States%22%2C%22population%22%3A327167434%2C%22workplace_types%22%3A%5B%5D%2C%22options%22%3A%7B%22flexible_regions%22%3A%5B%22anywhere_in_continent%22%2C%22anywhere_in_world%22%5D%7D%7D%5D%2C%22searchQuery%22%3A%22Financial+analyst%22%2C%22hideJobTypes%22%3A%5B%22Saved%22%2C%22Applied%22%2C%22Viewed%22%5D%2C%22applicationFormEase%22%3A%5B%22Time+Consuming%22%5D%2C%22seniorityLevel%22%3A%5B%22No+Prior+Experience+Required%22%2C%22Mid+Level%22%2C%22Entry+Level%22%5D%2C%22securityClearances%22%3A%5B%22None%22%5D%2C%22commitmentTypes%22%3A%5B%22Full+Time%22%2C%22Contract%22%5D%7D"
        },
        "Network Engineer": {
            "LinkedIn": "https://www.linkedin.com/jobs/search/?f_TPR=r86400&keywords=Network%20Engineer&origin=JOB_SEARCH_PAGE_JOB_FILTER&sortBy=DD"
        },
        "UI/UX": {
            "LinkedIn": "https://www.linkedin.com/jobs/search/?keywords=%22UI%22&origin=JOB_SEARCH_PAGE_JOB_FILTER&f_TPR=r86400&sortBy=DD",
            "SimplyHired": "https://www.simplyhired.com/search?q=ui+designer&l=united+states&t=1",
            "HiringCafe": "https://hiring.cafe/?searchState=%7B%22locations%22%3A%5B%7B%22id%22%3A%22FxY1yZQBoEtHp_8UEq7V%22%2C%22types%22%3A%5B%22country%22%5D%2C%22address_components%22%3A%5B%7B%22long_name%22%3A%22United+States%22%2C%22short_name%22%3A%22US%22%2C%22types%22%3A%5B%22country%22%5D%7D%5D%2C%22formatted_address%22%3A%22United+States%22%2C%22population%22%3A327167434%2C%22workplace_types%22%3A%5B%5D%2C%22options%22%3A%7B%22flexible_regions%22%3A%5B%22anywhere_in_continent%22%2C%22anywhere_in_world%22%5D%7D%7D%5D%2C%22dateFetchedPastNDays%22%3A2%2C%22searchQuery%22%3A%22%5C%22Ux+Designer%5C%22%22%2C%22applicationFormEase%22%3A%5B%22Time+Consuming%22%5D%2C%22sortBy%22%3A%22date%22%7D"
        },
        "Physical Design Engineer": {
            "LinkedIn": "https://www.linkedin.com/jobs/search/?distance=25&f_E=2%2C3%2C4&f_TPR=r86400&geoId=103644278&keywords=%22Physical%20Design%20Engineer%22&origin=JOB_SEARCH_PAGE_JOB_FILTER&refresh=true&sortBy=DD"
        },
        "ASIC Design Engineer": {
            "LinkedIn": "https://www.linkedin.com/jobs/search/?distance=25&f_E=2%2C3%2C4&f_TPR=r86400&geoId=103644278&keywords=%22ASIC%20Design%20Engineer%22&origin=JOB_SEARCH_PAGE_JOB_FILTER&refresh=true&sortBy=DD",
            "HiringCafe": "https://hiring.cafe/?searchState=%7B%22locations%22%3A%5B%7B%22id%22%3A%22FxY1yZQBoEtHp_8UEq7V%22%2C%22types%22%3A%5B%22country%22%5D%2C%22address_components%22%3A%5B%7B%22long_name%22%3A%22United+States%22%2C%22short_name%22%3A%22US%22%2C%22types%22%3A%5B%22country%22%5D%7D%5D%2C%22formatted_address%22%3A%22United+States%22%2C%22population%22%3A327167434%2C%22workplace_types%22%3A%5B%22Onsite%22%2C%22Hybrid%22%2C%22Remote%22%5D%2C%22options%22%3A%7B%22flexible_regions%22%3A%5B%22anywhere_in_continent%22%2C%22anywhere_in_world%22%5D%7D%7D%5D%2C%22seniorityLevel%22%3A%5B%22No+Prior+Experience+Required%22%2C%22Entry+Level%22%2C%22Mid+Level%22%5D%2C%22securityClearances%22%3A%5B%22None%22%5D%2C%22roleYoeRange%22%3A%5B0%2C5%5D%2C%22managementYoeRange%22%3A%5B0%2C5%5D%2C%22dateFetchedPastNDays%22%3A14%2C%22searchQuery%22%3A%22asic%22%2C%22applicationFormEase%22%3A%5B%22Time+Consuming%22%5D%7D"
        },
        "IC Design Engineer": {
            "LinkedIn": "https://www.linkedin.com/jobs/search/?distance=25&f_E=2%2C3%2C4&f_TPR=r86400&geoId=103644278&keywords=%22IC%20Design%20Engineer%22&origin=JOB_SEARCH_PAGE_JOB_FILTER&refresh=true&sortBy=DD"
        },
        "Process Engineer": {
            "LinkedIn": "https://www.linkedin.com/jobs/search/?keywords=process%20engineer&f_TPR=r86400&sortBy=DD",
            "SimplyHired": "https://www.simplyhired.com/search?q=process+engineer&l=United+States&t=1"
        },
        "Project Manager": {
            "LinkedIn": "https://www.linkedin.com/jobs/search/?f_TPR=r86400&keywords=project%20manager&sortBy=DD",
            "SimplyHired": "https://www.simplyhired.com/search?q=project+manager&l=United+States&t=1"
        },
        "Automotive Engineer": {
            "LinkedIn": "https://www.linkedin.com/jobs/search/?keywords=automotive%20engineer&f_TPR=r86400&sortBy=DD",
            "SimplyHired": "https://www.simplyhired.com/search?q=automotive+engineer&l=United+States&t=1"
        },
        "System Administrator": {
            "LinkedIn": "https://www.linkedin.com/jobs/search/?distance=25&f_TPR=r86400&geoId=103644278&keywords=system%20administrator&origin=JOB_SEARCH_PAGE_JOB_FILTER&refresh=true&sortBy=DD"
        },
        "Data Scientist": {
            "LinkedIn": "https://www.linkedin.com/jobs/search/?f_JT=F&f_TPR=r86400&keywords=data%20scientist&origin=JOB_SEARCH_PAGE_JOB_FILTER&sortBy=DD",
            "Indeed": "https://www.indeed.com/jobs?q=Data%20Scientist&l=united%20state&fromage=1&sc=0kf%3Aattr%28CF3CP%29explvl%28MID_LEVEL%29%3B&from=searchOnDesktopSerp",
            "HiringCafe": "https://hiring.cafe/?searchState=%7B%22locations%22%3A%5B%7B%22id%22%3A%22FxY1yZQBoEtHp_8UEq7V%22%2C%22types%22%3A%5B%22country%22%5D%2C%22address_components%22%3A%5B%7B%22long_name%22%3A%22United+States%22%2C%22short_name%22%3A%22US%22%2C%22types%22%3A%5B%22country%22%5D%7D%5D%2C%22formatted_address%22%3A%22United+States%22%2C%22population%22%3A327167434%2C%22workplace_types%22%3A%5B%5D%2C%22options%22%3A%7B%22flexible_regions%22%3A%5B%22anywhere_in_continent%22%2C%22anywhere_in_world%22%5D%7D%7D%5D%2C%22searchQuery%22%3A%22Data+scientist%22%2C%22dateFetchedPastNDays%22%3A2%7D"
        },
        "Supply Chain Analyst": {
            "HiringCafe": "https://hiring.cafe/?searchState=%7B%22locations%22%3A%5B%7B%22id%22%3A%22FxY1yZQBoEtHp_8UEq7V%22%2C%22types%22%3A%5B%22country%22%5D%2C%22address_components%22%3A%5B%7B%22long_name%22%3A%22United+States%22%2C%22short_name%22%3A%22US%22%2C%22types%22%3A%5B%22country%22%5D%7D%5D%2C%22formatted_address%22%3A%22United+States%22%2C%22population%22%3A327167434%2C%22workplace_types%22%3A%5B%5D%2C%22options%22%3A%7B%22flexible_regions%22%3A%5B%22anywhere_in_continent%22%2C%22anywhere_in_world%22%5D%7D%7D%5D%2C%22searchQuery%22%3A%22supply+chain+analyst+%22%2C%22dateFetchedPastNDays%22%3A2%2C%22applicationFormEase%22%3A%5B%22Time+Consuming%22%5D%2C%22hideJobTypes%22%3A%5B%22Applied%22%2C%22Viewed%22%2C%22Saved%22%5D%7D"
        },
        "Bioinformatics/ Biomedical Engineering": {
            "LinkedIn": "https://www.linkedin.com/jobs/search/?currentJobId=4403980468&distance=25.0&f_TPR=r86400&geoId=103644278&keywords=bioinformatics&origin=JOBS_HOME_KEYWORD_HISTORY"
        },
        "Research Assistant/ Scientist": {
            "LinkedIn": "https://www.linkedin.com/jobs/search/?currentJobId=4402977920&f_TPR=r604800&geoId=103644278&keywords=Research%20Assitant&origin=JOB_SEARCH_PAGE_JOB_FILTER"
        },
        "Electrical Engineer": {
            "LinkedIn": "https://www.linkedin.com/jobs/search/?distance=25&f_TPR=r86400&geoId=103644278&keywords=%22electrical%20engineer%22&origin=JOB_SEARCH_PAGE_JOB_FILTER&refresh=true&sortBy=DD",
            "SimplyHired": "https://www.simplyhired.com/search?q=electrical+engineer&l=United+States&t=1"
        },
        "Manufacturing Engineer": {
            "LinkedIn": "https://www.linkedin.com/jobs/search/?distance=25&f_TPR=r86400&geoId=103644278&keywords=%22manufacturing%20engineer%22&origin=JOB_SEARCH_PAGE_JOB_FILTER&refresh=true&sortBy=DD",
            "SimplyHired": "https://www.simplyhired.com/search?q=manufacturing+engineer&l=United+States&t=1"
        },
        "Product Development Engineer": {
            "LinkedIn": "https://www.linkedin.com/jobs/search/?distance=25&f_TPR=r86400&geoId=103644278&keywords=%22product%20development%20engineer%22&origin=JOB_SEARCH_PAGE_JOB_FILTER&refresh=true&sortBy=DD",
            "SimplyHired": "https://www.simplyhired.com/search?q=product+development+engineer&l=United+States&t=1"
        }
    }

    # 1. Load config from file if present, else use hardcoded fallback
    if not os.path.exists(config_file):
        print(f"[CONFIG] '{config_file}' not found. Using hardcoded production config (Render-safe).")
        config = HARDCODED_CONFIG
        # Also write it out for visibility/editing
        try:
            with open(config_file, "w") as f:
                json.dump(HARDCODED_CONFIG, f, indent=4)
            print(f"[CONFIG] Saved hardcoded config to '{config_file}' for reference.")
        except Exception as e:
            print(f"[CONFIG] Could not write config file: {e}")
    else:
        # 2. Load the filled out configuration
        with open(config_file, "r") as f:
            config = json.load(f)

        # Check if all URLs are empty (blank template was generated) → use hardcoded fallback
        has_any_url = any(
            url.strip()
            for platforms in config.values()
            for url in platforms.values()
            if url
        )
        if not has_any_url:
            print(f"[CONFIG] '{config_file}' exists but all URLs are empty. Using hardcoded production config.")
            config = HARDCODED_CONFIG

    # 3. Master Collection across all Categories
    all_scraped_jobs = []
    # Per-category accumulator: used for crash-safe per-link JSON saves.
    # Writing from memory avoids the O(N²) read+merge+rewrite that the old
    # save_to_json calls caused. Updated alongside all_scraped_jobs.
    category_jobs_map: dict = {}  # canonical_safe_cat_key -> list[job_data]
    
    # Ensure an archive directory exists for saving JSONs for review
    if not os.path.exists("json_archives"):
        os.makedirs("json_archives")
        
    # SAFETY CLEANUP FOR RENDER: Prevent 'disk full' crashes by keeping only recent archives
    try:
        now = time.time()
        for f in os.listdir("json_archives"):
            filepath = os.path.join("json_archives", f)
            # Delete files older than 24 hours
            if os.path.isfile(filepath) and os.stat(filepath).st_mtime < now - 86400:
                os.remove(filepath)
    except Exception as e:
        print(f"[WARN] Failed to clean up old archives: {e}")

    func_map = {
        "LinkedIn": scrape_linkedin_jobs,
        "SimplyHired": scrape_simplyhired_jobs,
        # "Glassdoor": scrape_glassdoor_jobs,
        # "Naukri": scrape_naukri_jobs,
        "Reed": scrape_reed_jobs,
        "HiringCafe": scrape_hiring_cafe_jobs,
        "Indeed": scrape_indeed_jobs,
    }

    # ONE shared driver for the entire cycle — avoids spawning 18 Chrome instances.
    # With 2 GB RAM on Render Pro, we can hold Chrome alive much longer before
    # restarting — cutting overhead and letting us scrape more per cycle.
    DRIVER_RESTART_EVERY = 10  # Restart Chrome every 10 categories; upgraded RAM means less-frequent restarts
    driver = None
    category_count = 0

    def get_or_restart_driver(current_driver, force=False):
        """Return existing driver, or restart it if it's time or forced."""
        nonlocal category_count
        if current_driver and not force:
            return current_driver
        if current_driver:
            try:
                print(f"[Browser] Recycling driver after {category_count} categories to free RAM...")
                current_driver.quit()
            except:
                pass
        gc.collect()  # Force Python GC so OS reclaims Chrome's memory before new instance
        time.sleep(2) # Brief pause to let OS fully reclaim pages
        category_count = 0
        return setup_chrome_driver()

    # Per-cycle URL-based duplicate guard.
    # The jobId-based upsert in push_to_db already prevents DB duplicates
    # across cycles, but within a SINGLE cycle the same job URL can appear
    # in multiple search results (e.g. LinkedIn1 and LinkedIn2 both surface it).
    # Tracking canonical URLs here avoids wasting Chrome time on pages we
    # already scraped this cycle.
    scraped_urls_this_cycle: set = set()

    # ── Initialize Live Progress Tracking ─────────────────────────────────
    progress_data = {
        "status": "counting" if count_only else "in_progress",
        "startedAt": datetime.now().isoformat(),
        "updatedAt": datetime.now().isoformat(),
        "mode": "count_only" if count_only else "count_and_scrape",
        "summary": {
            "totalCategories": len(config),
            "completedCategories": 0,
            "totalPlatformSearches": sum(len(p) for p in config.values()),
            "completedPlatformSearches": 0,
            "totalScrapableLinksQueued": 0,
            "totalJobsScrapedSuccessfully": 0,
            "totalJobsSaved": 0
        },
        "categories": {},
        "recentJobs": []
    }

    # Do NOT pre-populate categories. The user wants them to appear in the JSON
    # dynamically as the script processes them, acting like a live log.
    if "categories" not in progress_data:
        progress_data["categories"] = {}
    
    update_progress_file(progress_data, progress_file)

    def on_count_callback(cat, plat, available_count, scrapable_count):
        if cat not in progress_data["categories"]:
            progress_data["categories"][cat] = {"status": "in_progress", "platforms": {}, "totalScraped": 0}
        if plat not in progress_data["categories"][cat]["platforms"]:
            progress_data["categories"][cat]["platforms"][plat] = {}
        p_info = progress_data["categories"][cat]["platforms"][plat]
        p_info["availableReported"] = str(available_count)
        p_info["scrapableLinksQueued"] = scrapable_count
        # Reset scrapedCount to 0 when a new platform starts so the counter
        # reads cleanly for THIS run's scraping session.
        p_info["scrapedCount"] = 0
        p_info["status"] = "queued" if count_only else "scraping"
        progress_data["summary"]["totalScrapableLinksQueued"] += scrapable_count
        update_progress_file(progress_data, progress_file)

    def on_job_scraped_callback(cat, plat, job):
        progress_data["summary"]["totalJobsScrapedSuccessfully"] += 1
        # Also update per-platform and per-category counters so the JSON is accurate
        if cat in progress_data["categories"]:
            progress_data["categories"][cat]["totalScraped"] = \
                progress_data["categories"][cat].get("totalScraped", 0) + 1
            if plat in progress_data["categories"][cat]["platforms"]:
                p_info = progress_data["categories"][cat]["platforms"][plat]
                # Note: scrapedCount is set once in batch at the end of each
                # platform run (len(fresh_jobs)), so we don't increment here
                # to avoid double-counting.
        recent_entry = {
            "jobId": job.get("jobId"),
            "jobTitle": job.get("jobTitle"),
            "companyName": job.get("companyName"),
            "location": job.get("jobLocation"),
            "category": job.get("category"),
            "jobSource": job.get("jobSource"),
            "salary": job.get("salary"),
            "jobUrl": job.get("jobUrl"),
            "scrapedAt": job.get("createdAt", datetime.now().isoformat())
        }
        progress_data["recentJobs"].insert(0, recent_entry)
        if len(progress_data["recentJobs"]) > 30:
            progress_data["recentJobs"] = progress_data["recentJobs"][:30]
        update_progress_file(progress_data, progress_file)

    try:
        driver = get_or_restart_driver(None)

        # The user requested sequential scraping according to the JSON file
        category_items = list(config.items())
        # random.shuffle(category_items)  # Disabled to preserve JSON sequence

        consecutive_authwalls = 0  # track LinkedIn blocks in a row

        for category, platforms in category_items:
            print(f"\n=======================================================")
            print(f"[START] STARTING SCRAPE FOR TARGET CATEGORY: {category}")
            print(f"=======================================================")
            
            # Initialize category dynamically if it doesn't exist
            if category not in progress_data["categories"]:
                progress_data["categories"][category] = {
                    "status": "in_progress",
                    "platforms": {},
                    "totalScraped": 0
                }
            else:
                progress_data["categories"][category]["status"] = "in_progress"
                
            update_progress_file(progress_data, progress_file)

            # Restart driver every DRIVER_RESTART_EVERY categories
            if category_count > 0 and category_count % DRIVER_RESTART_EVERY == 0:
                driver = get_or_restart_driver(driver, force=True)

            category_count += 1

            for platform, url in platforms.items():
                if not url or url.strip() == "":
                    print(f"--- Skipping {platform} for {category} (URL is empty in JSON)")
                    continue

                # Resolve scraper function dynamically based on platform prefix
                func = None
                for key, val in func_map.items():
                    if platform.startswith(key):
                        func = val
                        break
                if not func:
                    continue
                    
                # Initialize platform dynamically if it doesn't exist
                if platform not in progress_data["categories"][category]["platforms"]:
                    progress_data["categories"][category]["platforms"][platform] = {
                        "url": url,
                        "availableReported": "Pending",
                        "scrapableLinksQueued": 0,
                        "scrapedCount": 0,
                        "status": "pending"
                    }
                    update_progress_file(progress_data, progress_file)

                try:
                    # Closures that capture the exact platform key (LinkedIn, LinkedIn2, LinkedIn3...)
                    # so both the count and scrape callbacks update the correct platform entry.
                    def make_count_callback(plat_key):
                        def _count_cb(cat, _plat_from_func, available, scrapable):
                            on_count_callback(cat, plat_key, available, scrapable)
                        return _count_cb

                    def make_scrape_callback(plat_key):
                        def _scrape_cb(cat, _plat_from_func, job):
                            on_job_scraped_callback(cat, plat_key, job)
                        return _scrape_cb

                    # Restart the Chrome driver between consecutive Indeed scrapes.
                    # Indeed flags the Selenium session after the first scrape;
                    # a fresh driver instance gets a clean session that bypasses this.
                    prev_platform = list(platforms.keys())[list(platforms.keys()).index(platform) - 1] if list(platforms.keys()).index(platform) > 0 else None
                    if platform.startswith("Indeed") and prev_platform and prev_platform.startswith("Indeed"):
                        print(f"\n  [Indeed] Restarting Chrome driver for fresh session before {platform}...")
                        driver = get_or_restart_driver(driver, force=True)
                        time.sleep(random.uniform(5, 10))  # brief settle time after restart

                    jobs = func(driver, url, category,
                                on_count=make_count_callback(platform),
                                on_job_scraped=make_scrape_callback(platform),
                                count_only=count_only)
                    if platform.startswith("LinkedIn"):
                        consecutive_authwalls = 0  # success → reset counter

                    if category in progress_data["categories"] and platform in progress_data["categories"][category]["platforms"]:
                        p_info = progress_data["categories"][category]["platforms"][platform]
                        p_info["status"] = "completed"

                    if jobs:
                        # ── Per-cycle URL dedup ───────────────────────────────
                        # Drop jobs whose URL we already visited this cycle
                        # (can happen when multiple search URLs surface the same
                        # posting). The DB upsert handles cross-cycle dedup.
                        fresh_jobs = []
                        for job in jobs:
                            cu = canonical_url(job.get('jobUrl', ''))
                            if cu and cu in scraped_urls_this_cycle:
                                print(f"  [DEDUP] Skipping already-scraped URL this cycle: {cu[:70]}")
                                continue
                            if cu:
                                scraped_urls_this_cycle.add(cu)
                            fresh_jobs.append(job)

                        if not fresh_jobs:
                            print(f"[Incremental] All {len(jobs)} jobs for {category}/{platform} were duplicates this cycle — skipping push.")
                        else:
                            dup_count = len(jobs) - len(fresh_jobs)
                            if dup_count:
                                print(f"[Incremental] Filtered {dup_count} within-cycle duplicate(s). Pushing {len(fresh_jobs)} unique jobs.")
                            # Apply refined categories BEFORE pushing to DB.
                            # The improved get_refined_category preserves the
                            # original target category for generic titles so
                            # Frontend/Backend/etc don't all get dumped into
                            # the Software Engineer bucket.
                            for job in fresh_jobs:
                                job['category'] = get_refined_category(job['jobTitle'], job['category'])
                                job['country'] = "UK" if output_prefix == "uk" else "US"

                            all_scraped_jobs.extend(fresh_jobs)

                            # Track per-category jobs in memory for crash-safe saves
                            safe_cat = category.lower().replace(" ", "_").replace("/", "_").replace(".", "_")
                            if safe_cat not in category_jobs_map:
                                category_jobs_map[safe_cat] = []
                            category_jobs_map[safe_cat].extend(fresh_jobs)

                            if category in progress_data["categories"] and platform in progress_data["categories"][category]["platforms"]:
                                p_info = progress_data["categories"][category]["platforms"][platform]
                                # Set the final scraped count for this platform URL.
                                # This is the single source of truth — on_job_scraped_callback
                                # no longer increments scrapedCount to avoid double-counting.
                                p_info["scrapedCount"] = len(fresh_jobs)

                            # Per-link JSON save: write from memory (no file read).
                            # Crash-safe: if the run dies mid-way, completed links are already
                            # on disk. O(M) where M = jobs in this category, not O(N²).
                            if SAVE_TO_JSON:
                                cat_filename = f"jobs_{safe_cat}_latest.json"
                                cat_jobs = category_jobs_map.get(safe_cat, [])
                                try:
                                    with open(cat_filename, "w", encoding="utf-8") as _f:
                                        json.dump(cat_jobs, _f, indent=2, ensure_ascii=False, default=str)
                                    with open("jobs_all_categories_latest.json", "w", encoding="utf-8") as _f:
                                        json.dump(all_scraped_jobs, _f, indent=2, ensure_ascii=False, default=str)
                                    print(f"[Saved] {len(fresh_jobs)} new jobs → '{cat_filename}' ({len(cat_jobs)} total). All-categories: {len(all_scraped_jobs)}.")
                                except Exception as _save_err:
                                    print(f"[WARN] Per-link JSON save failed: {_save_err}")

                            # Push to DB if enabled
                            if SAVE_TO_DB:
                                print(f"[Incremental] Pushing {len(fresh_jobs)} jobs for {category} to DB...")
                                try:
                                    push_jobs_list(fresh_jobs)
                                    print(f"[Incremental] Done pushing {len(fresh_jobs)} jobs.")
                                except Exception as push_err:
                                    print(f"[Incremental] Push failed: {push_err}")
                            else:
                                print(f"[Incremental] SAVE_TO_DB is False — skipping DB push for {len(fresh_jobs)} jobs.")

                    progress_data["summary"]["completedPlatformSearches"] += 1
                    if SAVE_TO_JSON or SAVE_TO_DB:
                        progress_data["summary"]["totalJobsSaved"] = len(all_scraped_jobs)
                    update_progress_file(progress_data, progress_file, force=True)
                except LinkedInAuthWall:
                    # LinkedIn served an auth-wall on this session. Recycle the
                    # driver right now so the NEXT category gets a fresh Chrome
                    # session instead of inheriting the blocked one (this is the
                    # core fix for "only first 2-3 categories get scraped").
                    consecutive_authwalls += 1
                    print(f"[Recovery] Recycling Chrome driver after LinkedIn auth-wall on {category}...")
                    driver = get_or_restart_driver(driver, force=True)
                    # If LinkedIn keeps blocking back-to-back, cool down longer
                    # to let the IP look less bot-like.
                    if consecutive_authwalls >= 2:
                        cool = random.randint(45, 90)
                        print(f"[Recovery] {consecutive_authwalls} consecutive auth-walls — cooling down {cool}s...")
                        time.sleep(cool)
                except Exception as e:
                    print(f"[ERROR] Error scraping {platform} for {category}: {e}")

                # Fix 7b: The 2.5–6.0 s delay guards against LinkedIn/Glassdoor
                # rate limits. SimplyHired and HiringCafe don't need it.
                if platform.startswith("LinkedIn") or platform.startswith("Glassdoor"):
                    time.sleep(random.uniform(2.5, 6.0))

            if category in progress_data["categories"]:
                progress_data["categories"][category]["status"] = "completed"
            progress_data["summary"]["completedCategories"] += 1
            update_progress_file(progress_data, progress_file, force=True)

    finally:
        if driver:
            print("[Browser] Closing shared driver.")
            try:
                driver.quit()
            except:
                pass

    # 4. Refine Categories and Re-group
    if all_scraped_jobs:
        print(f"\n[STATS] Total jobs collected: {len(all_scraped_jobs)}. Refining categories and grouping...")
        refined_groups = {}
        for job in all_scraped_jobs:
            new_cat = get_refined_category(job['jobTitle'], job['category'])
            job['category'] = new_cat
            safe_cat = new_cat.lower().replace(" ", "_").replace("/", "_").replace(".", "_")
            if safe_cat not in refined_groups:
                refined_groups[safe_cat] = []
            refined_groups[safe_cat].append(job)

        # 5. Export sorted and cleaned JSON clusters if SAVE_TO_JSON is enabled
        if SAVE_TO_JSON:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            for safe_cat, group_jobs in refined_groups.items():
                # Save the latest one in the main folder for easy access
                out_file_latest = f"jobs_{safe_cat}_latest.json"
                save_to_json(group_jobs, filename=out_file_latest)
                
                # Save an archived copy with a timestamp for review
                out_file_archived = f"json_archives/jobs_{safe_cat}_{timestamp}.json"
                save_to_json(group_jobs, filename=out_file_archived)

            # Export consolidated JSON file with ALL scraped jobs across all categories
            export_all_scraped_jobs(all_scraped_jobs, output_prefix=output_prefix)
        else:
            print("\n[JSON] SAVE_TO_JSON is False — skipping JSON cluster exports.")

    # 6. Final summary
    progress_data["status"] = "completed"
    progress_data["summary"]["totalJobsSaved"] = len(all_scraped_jobs)
    update_progress_file(progress_data, progress_file)
    print(f"\n[PROGRESS] Live progress and results saved to '{progress_file}'.")

    if all_scraped_jobs:
        dest_items = []
        if SAVE_TO_JSON:
            dest_items.append("JSON")
        if SAVE_TO_DB:
            dest_items.append("DB")
        dest_str = " & ".join(dest_items) if dest_items else "memory only (all persistence disabled)"
        print(f"\n[DONE] Cycle complete. {len(all_scraped_jobs)} total jobs processed ({dest_str}).")
    else:
        print("\n[WARN] No jobs collected this cycle.")

    return all_scraped_jobs, progress_data


def scrape_with_progress(config_file=None, progress_file=PROGRESS_FILE_PATH):
    """Alias for count_and_scrape_with_progress."""
    return count_and_scrape_with_progress(config_file=config_file, progress_file=progress_file, count_only=False)


def count_available_jobs(config_file=None, progress_file=PROGRESS_FILE_PATH):
    """Tells and outputs the available and scrapable job counts to job_progress.json without deep scraping."""
    return count_and_scrape_with_progress(config_file=config_file, progress_file=progress_file, count_only=True)


def main(config_file=None, uk_config_file=None, count_only=False, progress_file=None):
    """Run a full scrape cycle: US jobs first, then UK jobs.

    When called with no arguments (e.g. from run_scrapers.py), uses defaults:
      - US  config : latest_scraping_links.json  (or hardcoded fallback)
      - UK  config : link_uk.json
      - progress   : job_progress.json

    Command-line flags (when run directly):
      --count-only              only count available jobs, no deep scrape
      --progress-file=<path>    custom progress file path
      <positional>              US config file (first positional arg)
      --uk-config=<path>        UK config file (default: link_uk.json)
    """
    if config_file is None:
        config_file = "latest_scraping_links.json"
    if uk_config_file is None:
        uk_config_file = "link_uk.json"
    if progress_file is None:
        progress_file = PROGRESS_FILE_PATH

    # Parse CLI overrides when called as a script
    for arg in sys.argv[1:]:
        if arg == "--count-only":
            count_only = True
        elif arg.startswith("--progress-file="):
            progress_file = arg.split("=", 1)[1]
        elif arg.startswith("--uk-config="):
            uk_config_file = arg.split("=", 1)[1]
        elif not arg.startswith("-"):
            config_file = arg

    # ── Pass 1: US jobs ───────────────────────────────────────────────────────
    print("\n" + "="*65)
    print("  PASS 1 — US Jobs")
    print("="*65)
    us_progress_file = progress_file  # reuse the standard progress file
    count_and_scrape_with_progress(
        config_file=config_file,
        progress_file=us_progress_file,
        count_only=count_only,
    )

    # ── Pass 2: UK jobs ───────────────────────────────────────────────────────
    _uk_config = os.path.join(os.path.dirname(os.path.abspath(__file__)), uk_config_file) \
        if not os.path.isabs(uk_config_file) else uk_config_file

    if not os.path.exists(_uk_config):
        print(f"\n[UK] '{uk_config_file}' not found — skipping UK scrape pass.")
    else:
        print("\n" + "="*65)
        print("  PASS 2 — UK Jobs")
        print("="*65)
        # Use a separate progress file so UK stats don't overwrite US stats
        uk_progress_file = os.path.join(
            os.path.dirname(progress_file),
            "job_progress_uk.json",
        )
        count_and_scrape_with_progress(
            config_file=_uk_config,
            progress_file=uk_progress_file,
            count_only=count_only,
            output_prefix="uk",
        )

    print("\n[DONE] Full cycle complete (US + UK).")


if __name__ == "__main__":
    main()




