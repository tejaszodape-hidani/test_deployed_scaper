import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
import os
import sys
import re
import urllib.parse
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium_stealth import stealth
import undetected_chromedriver as uc
from sqlalchemy import text
# Add parent directory to path to import backend modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Centralised DB config (engine, Job model, push helpers) ──────────────────
from push_to_db import (
    engine, Job, Base, DATABASE_URL,
    check_db_connection, push_data, push_jobs_list
)

# ── SSL fix for macOS ─────────────────────────────────────────────────────────
# Python installed via python.org on macOS does not use the system keychain.
# This makes certifi's CA bundle the trust store, fixing the
# "SSL: CERTIFICATE_VERIFY_FAILED" error that undetected_chromedriver triggers.
import ssl
import certifi
ssl._create_default_https_context = ssl.create_default_context
os.environ.setdefault('SSL_CERT_FILE', certifi.where())
os.environ.setdefault('REQUESTS_CA_BUNDLE', certifi.where())
# ─────────────────────────────────────────────────────────────────────────────

_DIR = os.path.dirname(os.path.abspath(__file__))

# Resolve all paths relative to THIS script file, so the scraper
# works whether you run it from the project root OR from inside
# the tejas-green_work/ directory.
_DIR = os.path.dirname(os.path.abspath(__file__))

PROGRESS_FILE        = os.path.join(_DIR, "workday_progress.json")
JSON_OUTPUT_FILE     = os.path.join(_DIR, "workday_jobs_output.json")
_WORKDAY_COMPANY_JSON = os.path.join(_DIR, "workday_company_urls.json")

# ─────────────────────────────────────────────────────────────
# Chrome version — must match your locally installed Chrome.
# undetected_chromedriver uses this to download the RIGHT ChromeDriver.
# Run: /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --version
# then set the MAJOR version number below.
# ─────────────────────────────────────────────────────────────
CHROME_VERSION = 152   # <-- update this if Chrome auto-updates

# ─────────────────────────────────────────────────────────────
# OUTPUT TOGGLES — controlled via .env (set to 'true' or 'false'):
#   SAVE_TO_DATABASE=true  → save jobs to MySQL / PostgreSQL database
#   SAVE_TO_JSON=true      → also keep a JSON backup file
# ─────────────────────────────────────────────────────────────
SAVE_TO_DATABASE = os.getenv("SAVE_TO_DATABASE", "true").strip().lower() == "true"
SAVE_TO_JSON     = os.getenv("SAVE_TO_JSON",     "true").strip().lower() == "true"

# ─────────────────────────────────────────────────────────────
# TOGGLE: Set to True  → scrape companies in parallel (3× faster)
#         Set to F
# 
# alse → scrape sequentially one-by-one (safe, original)
# ─────────────────────────────────────────────────────────────
USE_WORKERS = True    # <-- flip to False for original sequential mode
WORKERS     = 3      # number of parallel Chrome instances (raise to 5 if RAM allows)

def load_companies():
    """Load company names from workday_company_urls.json (keys only)."""
    with open(_WORKDAY_COMPANY_JSON, 'r', encoding='utf-8') as f:
        data = json.load(f)
    # Return all company names (keys), including those whose URL is null —
    # get_pre_mapped_url() already handles the null case gracefully.
    return list(data.keys())

def text_from_html(html):
    """Extract clean plain text from HTML, stripping all tags."""
    if not html:
        return 'Job description not available'
    clean = re.sub(r'<script\b[^<]*(?:(?!<\/script>)<[^<]*)*<\/script>', '', html, flags=re.IGNORECASE)
    clean = re.sub(r'<style\b[^<]*(?:(?!<\/style>)<[^<]*)*<\/style>', '', clean, flags=re.IGNORECASE)
    clean = re.sub(r'<[^>]+>', ' ', clean)
    clean = clean.replace('&nbsp;', ' ').replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    clean = re.sub(r'&#?\w+;', '', clean)
    clean = re.sub(r'\s+', ' ', clean).strip()
    return clean or 'Job description not available'


def _html_tag_text(tag_html):
    """Strip HTML tags from a single tag snippet and return clean inner text."""
    t = re.sub(r'<[^>]+>', ' ', tag_html)
    t = t.replace('&nbsp;', ' ').replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    t = re.sub(r'&#?\w+;', '', t)
    return re.sub(r'\s+', ' ', t).strip()


# Section heading keywords used to classify description blocks
_SECTION_KEYWORDS = {
    'responsibilities':  ['responsibilities', 'what you will do', 'what you\'ll do', 'key responsibilities',
                          'role responsibilities', 'your role', 'day-to-day', 'duties', 'your responsibilities'],
    'qualifications':    ['qualifications', 'requirements', 'what you bring', 'what we\'re looking for',
                          'what you need', 'must have', 'basic qualifications', 'minimum qualifications',
                          'required skills', 'required experience', 'you have', 'about you'],
    'preferred':         ['preferred qualifications', 'nice to have', 'preferred skills', 'bonus points',
                          'preferred experience', 'plus if you have', 'advantageous'],
    'benefits':          ['benefits', 'perks', 'what we offer', 'compensation', 'total rewards',
                          'why join us', 'what\'s in it for you', 'our benefits'],
    'about_company':     ['about us', 'about the company', 'who we are', 'our mission', 'our story',
                          'company overview', 'about the team'],
    'about_role':        ['about the role', 'the role', 'position overview', 'job summary',
                          'role overview', 'what this role is about', 'overview'],
    'equal_opportunity': ['equal opportunity', 'eeo', 'diversity', 'inclusion', 'accommodations'],
}


def parse_description_with_metadata(html):
    """Parse a Workday job description HTML and return a structured metadata dict.

    Returns a dict with:
      {
        "sections": [
            {
                "tag": "<original section heading tag, e.g. h2/h3/strong>",
                "heading": "<clean heading text>",
                "category": "<classified category or 'other'>",
                "items": ["bullet 1", "bullet 2", ...],  # from <li> / <p> under this heading
                "raw_html": "<original HTML fragment for this section>"
            },
            ...
        ],
        "summary": "<first paragraph text, usually the intro>",
        "full_text": "<full plain-text version>"
      }
    """
    if not html:
        return {"sections": [], "summary": "", "full_text": ""}

    # ── Strip script/style noise ──────────────────────────────────────────────
    html = re.sub(r'<script\b[^<]*(?:(?!<\/script>)<[^<]*)*<\/script>', '', html, flags=re.IGNORECASE)
    html = re.sub(r'<style\b[^<]*(?:(?!<\/style>)<[^<]*)*<\/style>',  '', html, flags=re.IGNORECASE)

    full_text = text_from_html(html)

    # ── Extract summary (first <p> text before any heading) ───────────────────
    summary = ''
    first_p_match = re.search(r'<p[^>]*>(.*?)</p>', html, re.IGNORECASE | re.DOTALL)
    if first_p_match:
        summary = _html_tag_text(first_p_match.group(1)).strip()

    # ── Split HTML into sections by block-level headings ──────────────────────
    # We split on <h1..h6>, <p><strong>…</strong></p>, or <div class=... containing bold text
    heading_pattern = re.compile(
        r'(<(?:h[1-6]|p)\b[^>]*>\s*(?:<(?:strong|b|em)[^>]*>)?[^<]{3,}(?:</(?:strong|b|em)>)?\s*</(?:h[1-6]|p)>)',
        re.IGNORECASE | re.DOTALL
    )

    parts = heading_pattern.split(html)

    sections = []
    i = 0
    while i < len(parts):
        part = parts[i]
        heading_match = heading_pattern.fullmatch(part.strip()) if part.strip() else None
        if heading_match:
            heading_html = part.strip()
            heading_text = _html_tag_text(heading_html).strip()
            # Next part is the content block after this heading
            content_html = parts[i + 1] if (i + 1) < len(parts) else ''

            # Classify the heading
            heading_lower = heading_text.lower()
            category = 'other'
            for cat, keywords in _SECTION_KEYWORDS.items():
                if any(kw in heading_lower for kw in keywords):
                    category = cat
                    break

            # Extract bullet items from the content block
            items = []
            li_matches = re.findall(r'<li[^>]*>(.*?)</li>', content_html, re.IGNORECASE | re.DOTALL)
            if li_matches:
                items = [_html_tag_text(li).strip() for li in li_matches if _html_tag_text(li).strip()]
            else:
                # Fall back to <p> paragraphs if no list items
                p_matches = re.findall(r'<p[^>]*>(.*?)</p>', content_html, re.IGNORECASE | re.DOTALL)
                items = [_html_tag_text(p).strip() for p in p_matches if _html_tag_text(p).strip()]

            # Determine original heading tag name (h2, h3, p, etc.)
            tag_name_match = re.match(r'<(h[1-6]|p)', heading_html, re.IGNORECASE)
            tag_name = tag_name_match.group(1).lower() if tag_name_match else 'p'

            sections.append({
                "tag": tag_name,
                "heading": heading_text,
                "category": category,
                "items": items,
                "raw_html": (heading_html + content_html)[:5000]  # cap at 5 KB per section
            })
            i += 2  # skip both the heading part and its content part
        else:
            i += 1

    # ── Fallback: if no headings found, try to extract all <li> as a flat list ─
    if not sections:
        all_li = re.findall(r'<li[^>]*>(.*?)</li>', html, re.IGNORECASE | re.DOTALL)
        if all_li:
            items = [_html_tag_text(li).strip() for li in all_li if _html_tag_text(li).strip()]
            sections.append({
                "tag": "ul",
                "heading": "Job Details",
                "category": "other",
                "items": items,
                "raw_html": ""
            })

    return {
        "sections": sections,
        "summary": summary,
        "full_text": full_text
    }

def parse_experience(text):
    """Extract years of experience from text"""
    if not text:
        return None
    match = re.search(r'(\d+)(?:\+|\s*-\s*\d+)?\s*(?:\+\s*)?years?\s+(?:of\s+)?experience', text, re.IGNORECASE)
    if match:
        years = match.group(1)
        if 'plus' in match.group(0).lower() or '+' in match.group(0):
            return f"{years}+ years"
        return f"{years} years"
    return None

# ─────────────────────────────────────────────────────────────
# Comprehensive Industry Skill Taxonomy (400+ Skills)
# ─────────────────────────────────────────────────────────────

# Skills that need special word-boundary handling to avoid false positives
# (e.g. 'go' matching 'good', 'api' matching 'rapid', 'r' matching everywhere)
_AMBIGUOUS_SKILLS = {
    'go':   r'\bgo\b(?!ogle|od|ne|al|ing|ver|t\b)',   # avoid "google", "good", "gone", "goal"
    'r':    r'\bR\b(?!\w)',                             # capital R only (R language), not inside words
    'api':  r'\bAPI[s]?\b',                            # API / APIs — uppercase guard
    'node': r'\bNode\.?js\b|\bNode\b(?=\.js| js)',    # Node.js explicitly
    'sql':  r'\bSQL\b',                                # SQL in uppercase (avoid "casual")
    'elk':  r'\bELK\b|\bELK stack\b',
    'soc':  r'\bSOC\b(?!\d)',                          # avoid "SOC2" matching as both
    'iam':  r'\bIAM\b',
}

SKILL_TAXONOMY = {
    # ── Programming Languages ──────────────────────────────────────────────
    'python', 'javascript', 'typescript', 'java', 'c++', 'c#', 'golang',
    'rust', 'ruby', 'php', 'swift', 'kotlin', 'scala', 'perl',
    'bash', 'shell', 'powershell', 'sql', 'html', 'css', 'sass', 'less',
    'matlab', 'vba', 'cobol', 'fortran', 'haskell', 'elixir', 'clojure',
    'dart', 'groovy', 'lua', 'objective-c',
    # Ambiguous — handled separately by _AMBIGUOUS_SKILLS
    'go', 'r', 'api', 'node', 'elk', 'soc', 'iam',

    # ── Frontend / Mobile ──────────────────────────────────────────────────
    'react', 'react native', 'angular', 'vue', 'vue.js', 'svelte',
    'next.js', 'nuxt', 'express', 'flutter', 'ios', 'android',
    'tailwind', 'bootstrap', 'jquery', 'webpack', 'vite', 'gatsby',
    'remix', 'storybook', 'styled-components', 'redux', 'graphql',

    # ── Backend & APIs ─────────────────────────────────────────────────────
    'django', 'flask', 'fastapi', 'spring', 'spring boot', 'asp.net',
    '.net', 'rails', 'ruby on rails', 'laravel', 'restful', 'grpc',
    'microservices', 'soap', 'websocket', 'kafka streams','restapi'

    # ── Cloud & DevOps ─────────────────────────────────────────────────────
    'aws', 'amazon web services', 'azure', 'gcp', 'google cloud',
    'docker', 'kubernetes', 'k8s', 'terraform', 'ansible', 'jenkins',
    'ci/cd', 'helm', 'prometheus', 'grafana', 'cloudformation', 'linux',
    'unix', 'git', 'github', 'gitlab', 'datadog', 'new relic', 'splunk',
    'opentelemetry', 'nginx', 'apache', 'pulumi', 'argocd', 'vault',
    'istio', 'service mesh', 'lambda', 'ec2', 's3', 'eks', 'ecs',
    'azure devops', 'github actions', 'bitbucket',

    # ── Data / AI / ML ─────────────────────────────────────────────────────
    'pytorch', 'tensorflow', 'keras', 'scikit-learn', 'pandas', 'numpy',
    'scipy', 'spark', 'apache spark', 'hadoop', 'kafka', 'apache kafka',
    'snowflake', 'databricks', 'bigquery', 'redshift', 'airflow', 'dbt',
    'tableau', 'power bi', 'looker', 'langchain', 'langgraph',
    'llm', 'llms', 'genai', 'generative ai', 'nlp', 'computer vision',
    'deep learning', 'machine learning', 'data modeling', 'data warehousing',
    'etl', 'elt', 'mlflow', 'kubeflow', 'ray', 'hugging face',
    'openai', 'chatgpt', 'gemini', 'claude', 'copilot', 'rag',
    'vector database', 'pinecone', 'weaviate', 'chroma', 'embeddings',
    'fine-tuning', 'prompt engineering', 'reinforcement learning',
    'feature engineering', 'data pipeline', 'data lake', 'lakehouse',
    'apache flink', 'apache beam', 'glue', 'sagemaker', 'vertex ai',
    'azure ml', 'automl', 'xgboost', 'lightgbm', 'random forest',

    # ── Databases & Storage ────────────────────────────────────────────────
    'postgresql', 'postgres', 'mysql', 'mongodb', 'redis', 'elasticsearch',
    'cassandra', 'dynamodb', 'oracle', 'sqlite', 'neo4j', 'mariadb',
    'couchbase', 'cosmosdb', 'sql server', 'aurora', 'firestore',
    'supabase', 'planetscale', 'cockroachdb', 'timescaledb', 'influxdb',

    # ── Security & Infrastructure ──────────────────────────────────────────
    'siem', 'penetration testing', 'crowdstrike', 'oauth', 'okta',
    'pki', 'zero trust', 'cryptography', 'cissp', 'wireshark', 'owasp',
    'network security', 'firewalls', 'devsecops', 'sonarqube',
    'snyk', 'aqua security', 'cis benchmarks', 'soc 2', 'iso 27001',
    'cism', 'security+', 'active directory', 'ldap', 'saml',

    # ── Testing & QA ──────────────────────────────────────────────────────
    'selenium', 'cypress', 'playwright', 'jest', 'pytest', 'junit',
    'mocha', 'postman', 'cucumber', 'testng', 'appium', 'k6',
    'load testing', 'e2e testing', 'tdd', 'bdd',

    # ── Design & Product ───────────────────────────────────────────────────
    'figma', 'sketch', 'adobe', 'photoshop', 'illustrator', 'indesign',
    'ux', 'ui', 'wireframing', 'prototyping', 'user research',
    'product design', 'adobe xd', 'invision', 'zeplin', 'miro',
    'design system', 'accessibility', 'wcag', 'after effects', 'premiere pro',
    'lightroom', 'capcut', 'canva', 'video editing', 'motion graphics',
    'adobe creative suite', 'brand design', 'typography',

    # ── Business, Finance & Management ────────────────────────────────────
    'jira', 'confluence', 'agile', 'scrum', 'kanban', 'salesforce',
    'sap', 'servicenow', 'hubspot', 'quickbooks', 'excel', 'bloomberg',
    'factset', 'ncino', 'workday', 'oracle hcm', 'netsuite',
    'microsoft 365', 'sharepoint', 'ms project', 'linear',
    'notion', 'asana', 'monday.com',

    # ── Emerging / Specialized ─────────────────────────────────────────────
    'blockchain', 'web3', 'solidity', 'smart contracts', 'defi',
    'ar', 'vr', 'augmented reality', 'virtual reality', 'unity',
    'unreal engine', 'webgl', 'three.js', 'ros', 'embedded systems',
    'fpga', 'vhdl', 'verilog', 'pcb design', 'cad', 'autocad',
    'solidworks', 'ansys', 'matlab simulink',

    # ═══════════════════════════════════════════════════════════════════════
    # NON-TECH SKILLS
    # ═══════════════════════════════════════════════════════════════════════

    # ── Marketing & Advertising ────────────────────────────────────────────
    'seo', 'sem', 'google ads', 'facebook ads', 'meta ads', 'tiktok ads',
    'programmatic advertising', 'display advertising', 'digital advertising',
    'google analytics', 'google tag manager', 'google search console',
    'social media marketing', 'social media management', 'content marketing',
    'email marketing', 'marketing automation', 'mailchimp', 'marketo',
    'pardot', 'klaviyo', 'constant contact', 'hootsuite', 'sprout social',
    'brand strategy', 'brand management', 'campaign management',
    'content strategy', 'content creation', 'copywriting', 'storytelling',
    'creative strategy', 'growth marketing', 'performance marketing',
    'affiliate marketing', 'influencer marketing', 'public relations',
    'media planning', 'media buying', 'a/b testing', 'conversion optimization',
    'cro', 'demand generation', 'lead generation', 'market research',
    'competitive analysis', 'go-to-market', 'gtm strategy',

    # ── Sales & Business Development ──────────────────────────────────────
    'salesforce crm', 'crm', 'hubspot crm', 'outreach', 'salesloft',
    'zoominfo', 'linkedin sales navigator', 'cold calling', 'cold outreach',
    'pipeline management', 'account management', 'account executive',
    'business development', 'b2b sales', 'b2c sales', 'enterprise sales',
    'solution selling', 'consultative selling', 'sales cycle',
    'revenue operations', 'revops', 'quota management',

    # ── Finance & Accounting ───────────────────────────────────────────────
    'financial modeling', 'financial analysis', 'financial reporting',
    'budgeting', 'forecasting', 'variance analysis', 'gaap', 'ifrs',
    'accounts payable', 'accounts receivable', 'general ledger',
    'reconciliation', 'audit', 'tax', 'cpa', 'cfa', 'cma',
    'erp', 'oracle financials', 'sap fi', 'netsuite', 'sage',
    'cost accounting', 'fund accounting', 'investment analysis',
    'portfolio management', 'risk management', 'compliance',
    'treasury', 'cash flow', 'p&l', 'income statement', 'balance sheet',
    'valuation', 'dcf', 'mergers and acquisitions', 'm&a',
    'private equity', 'venture capital', 'fixed income', 'equity research',

    # ── Human Resources ────────────────────────────────────────────────────
    'talent acquisition', 'recruiting', 'sourcing', 'onboarding',
    'hris', 'workday hcm', 'adp', 'bamboohr', 'peoplesoft',
    'performance management', 'compensation', 'benefits administration',
    'employee relations', 'labor relations', 'workforce planning',
    'succession planning', 'learning and development', 'l&d',
    'organizational development', 'culture', 'dei', 'diversity',
    'payroll', 'fmla', 'ada', 'eeoc', 'erisa',

    # ── Healthcare & Clinical ──────────────────────────────────────────────
    'epic', 'ehr', 'emr', 'cerner', 'meditech', 'allscripts',
    'hipaa', 'icd-10', 'cpt codes', 'medical billing', 'medical coding',
    'clinical trials', 'gcp', 'fda regulations', 'clinical research',
    'patient care', 'case management', 'care coordination',
    'rn', 'lpn', 'cna', 'bls', 'acls', 'pals',
    'pharmacy', 'radiology', 'laboratory', 'pathology', 'phlebotomy',
    'nursing', 'surgery', 'oncology', 'cardiology', 'pediatrics',
    'behavioral health', 'mental health', 'social work',
    'health informatics', 'population health', 'value-based care',

    # ── Legal & Compliance ─────────────────────────────────────────────────
    'contract management', 'contract negotiation', 'contract review',
    'legal research', 'legal writing', 'litigation', 'regulatory compliance',
    'privacy law', 'gdpr', 'ccpa', 'intellectual property', 'patent',
    'corporate law', 'employment law', 'securities law', 'due diligence',
    'paralegal', 'e-discovery', 'compliance management',

    # ── Operations & Supply Chain ──────────────────────────────────────────
    'supply chain management', 'procurement', 'vendor management',
    'logistics', 'inventory management', 'warehouse management',
    'lean', 'six sigma', 'kaizen', 'process improvement',
    'operations management', 'project management', 'pmp',
    'erp systems', 'sap erp', 'oracle erp',
    'demand planning', 'forecasting', 's&op',
    'quality assurance', 'quality management', 'iso 9001',
    'manufacturing', 'production planning', 'lean manufacturing',

    # ── Customer Success & Support ─────────────────────────────────────────
    'customer success', 'customer service', 'customer experience',
    'zendesk', 'freshdesk', 'intercom', 'kustomer', 'genesys',
    'help desk', 'ticketing system', 'technical support', 'it support',
    'net promoter score', 'nps', 'csat', 'churn reduction',
    'account management', 'client management', 'client relations',
    'escalation management', 'saas onboarding',

    # ── Real Estate, Construction & Facilities ─────────────────────────────
    'real estate', 'property management', 'leasing', 'mri software',
    'yardi', 'argus', 'construction management', 'project scheduling',
    'primavera', 'ms project', 'estimating', 'cost estimation',
    'facilities management', 'building management', 'hvac', 'bim',
    'revit', 'autocad architecture',

    # ── Education & Training ───────────────────────────────────────────────
    'curriculum development', 'instructional design', 'lms',
    'canvas', 'blackboard', 'moodle', 'articulate', 'e-learning',
    'training delivery', 'coaching', 'mentoring', 'facilitation',

    # ── Soft Skills (measurable/structured) ───────────────────────────────
    'cross-functional collaboration', 'stakeholder management',
    'executive communication', 'presentation skills', 'data-driven',
    'problem solving', 'critical thinking', 'change management',
    'strategic planning', 'decision making', 'negotiation',
}


def extract_skills(text, company_name):
    """Extract skills from job description text using SKILL_TAXONOMY.

    Improvements over v1:
    - Strips HTML tags before matching (avoids tag noise)
    - Uses special regex guards for ambiguous short skills (go, r, c, api…)
    - Returns canonical, sorted, deduplicated skill names
    """
    if not text:
        return []

    # Strip HTML tags first so tag attributes don't pollute matching
    clean_text = re.sub(r'<[^>]+>', ' ', text)
    clean_text = clean_text.replace('&nbsp;', ' ').replace('&amp;', '&')
    clean_text = re.sub(r'\s+', ' ', clean_text)

    # Remove company name to avoid false positives
    if company_name:
        pattern = r'\b' + re.escape(company_name.lower()) + r'\b'
        clean_text = re.sub(pattern, ' ', clean_text, flags=re.IGNORECASE)

    lower_text = clean_text.lower()
    found = set()

    for skill in SKILL_TAXONOMY:
        if skill in _AMBIGUOUS_SKILLS:
            # Use custom pattern for ambiguous short skills
            if re.search(_AMBIGUOUS_SKILLS[skill], clean_text, re.IGNORECASE):
                found.add(skill)
        else:
            # Standard word-boundary matching
            if re.search(r'\b' + re.escape(skill) + r'\b', lower_text):
                found.add(skill)

    return sorted(list(found))

def is_us_location(location):
    """Check if location is strictly in the US"""
    if not location:
        return False
    
    us_terms = ['united states', 'usa', 'u.s.', 'u.s.a.', 'america']
    us_states = [
        'alabama', 'alaska', 'arizona', 'arkansas', 'california', 'colorado', 'connecticut', 'delaware',
        'florida', 'georgia', 'hawaii', 'idaho', 'illinois', 'indiana', 'iowa', 'kansas', 'kentucky',
        'louisiana', 'maine', 'maryland', 'massachusetts', 'michigan', 'minnesota', 'mississippi', 'missouri',
        'montana', 'nebraska', 'nevada', 'new hampshire', 'new jersey', 'new mexico', 'new york',
        'north carolina', 'north dakota', 'ohio', 'oklahoma', 'oregon', 'pennsylvania', 'rhode island',
        'south carolina', 'south dakota', 'tennessee', 'texas', 'utah', 'vermont', 'virginia', 'washington',
        'west virginia', 'wisconsin', 'wyoming', 'district of columbia', 'puerto rico'
    ]
    
    loc = location.lower().strip()
    
    for term in us_terms:
        if re.search(r'\b' + re.escape(term) + r'\b', loc):
            return True

    for state in us_states:
        if re.search(r'\b' + re.escape(state) + r'\b', loc):
            return True
    
    return False


def is_uk_location(location):
    """Check if location is in the United Kingdom."""
    if not location:
        return False

    uk_terms = [
        'united kingdom', 'uk', 'u.k.', 'great britain', 'britain',
        'england', 'scotland', 'wales', 'northern ireland',
    ]
    uk_cities = [
        'london', 'manchester', 'birmingham', 'leeds', 'glasgow', 'liverpool',
        'edinburgh', 'bristol', 'sheffield', 'cardiff', 'belfast', 'nottingham',
        'leicester', 'coventry', 'bradford', 'stoke', 'wolverhampton', 'plymouth',
        'derby', 'reading', 'southampton', 'oxford', 'cambridge', 'bath',
        'exeter', 'york', 'newcastle', 'sunderland', 'middlesbrough',
    ]

    loc = location.lower().strip()

    for term in uk_terms:
        if re.search(r'\b' + re.escape(term) + r'\b', loc):
            return True

    for city in uk_cities:
        if re.search(r'\b' + re.escape(city) + r'\b', loc):
            return True

    return False


def is_us_or_uk_location(location):
    """Return True if the location is in the US or UK."""
    return is_us_location(location) or is_uk_location(location)

# Pre-seeded directory of verified Workday portals for enterprise brands with bespoke site names/domains
KNOWN_WORKDAY_URLS = {
    'snap': 'https://snapchat.wd1.myworkdayjobs.com/snap',
    'state street': 'https://statestreet.wd1.myworkdayjobs.com/Global',
    'parsons': 'https://parsons.wd5.myworkdayjobs.com/CorporateCareers',
    'nvidia': 'https://nvidia.wd5.myworkdayjobs.com/nvidiaExternalCareerSite',
    'capital one': 'https://capitalone.wd12.myworkdayjobs.com/Capital_One',
    'credit acceptance': 'https://creditacceptance.wd5.myworkdayjobs.com/Credit_Acceptance',
    'm&t bank': 'https://mtb.wd5.myworkdayjobs.com/MTB',
    'alsac': 'https://alsacstjude.wd1.myworkdayjobs.com/careersalsacstjude',
    'c.h. robinson': 'https://chrobinson.wd1.myworkdayjobs.com/chrobinson',
}

def get_workday_career_url(company_name):
    """Generate prioritized Workday career URLs for a company.
    
    Handles special characters, known company aliases, multi-datacenter Workday hosts
    (wd5, wd1, wd12, wd3, etc.), and realistic career site paths.
    """
    clean = re.sub(r"[&.,'/\\()\-]+", " ", company_name.lower())
    clean = re.sub(r"\s+", " ", clean).strip()

    slug_nospace = clean.replace(" ", "")
    slug_hyphen  = clean.replace(" ", "-")
    raw_nospace  = re.sub(r"[^a-z0-9]", "", company_name.lower())

    slugs = []
    low = company_name.lower()
    if 'alsac' in low:
        slugs.append('alsacstjude')
    if 'm&t' in low or 'm & t' in low:
        slugs.extend(['mtb', 'mtbank'])
    if 'snap' in low:
        slugs.append('snapchat')
    if 'capital one' in low:
        slugs.append('capitalone')
    if 'credit acceptance' in low:
        slugs.append('creditacceptance')
    if 'c.h. robinson' in low or 'ch robinson' in low:
        slugs.append('chrobinson')

    slugs.extend([slug_nospace, slug_hyphen, raw_nospace])
    slugs = list(dict.fromkeys(slugs))

    patterns = []
    # 1. Check known enterprise portals first
    for known_name, known_url in KNOWN_WORKDAY_URLS.items():
        if known_name in low or low in known_name:
            patterns.append(known_url)

    wd_hosts = [
        "wd5.myworkdayjobs.com",
        "wd1.myworkdayjobs.com",
        "wd12.myworkdayjobs.com",
        "wd3.myworkdayjobs.com",
        "wd2.myworkdayjobs.com",
        "myworkdayjobs.com",
    ]

    title_slug = ''.join(w.capitalize() for w in clean.split())
    underscore_slug = '_'.join(w.capitalize() for w in clean.split())

    path_suffixes = [
        f"/{slug_nospace}",
        f"/{underscore_slug}",
        f"/{title_slug}",
        f"/{clean.upper()}",
        f"/{slug_nospace}ExternalCareerSite",
        "/Global",
        "/en-US/Global",
        "/Careers",
        "/External",
        "/External_Career_Site",
        f"/careers{slug_nospace}",
        f"/en-US/careers{slug_nospace}",
        f"/en-US/{slug_nospace}",
        "/{slug}",
        "/en-US/{slug}",
        "/{slug}ExternalCareerSite",
        "/en-US/{slug}ExternalCareerSite",
        "/{slug}Careers",
        "/CorporateCareers",
        "/jobs",
    ]

    for suffix in path_suffixes:
        for slug in slugs:
            for host in wd_hosts:
                url = f"https://{slug}.{host}" + suffix.format(slug=slug)
                if url not in patterns:
                    patterns.append(url)
    return patterns

def try_workday_cxs_api(working_url, company_name):
    """Attempt fast structured job extraction via Workday CXS REST API, including full descriptions."""
    jobs = []
    try:
        parsed = urllib.parse.urlparse(working_url)
        host = parsed.netloc
        path_parts = [p for p in parsed.path.strip('/').split('/') if p and p != 'en-US']
        if not path_parts:
            return []
        site = path_parts[0]
        tenant = host.split('.')[0]
        api_url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
        headers = {
            'Content-Type': 'application/json',
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        # Fetch all postings from company portal without text filter restriction
        search_text = ''
        r = requests.post(api_url, json={'appliedFacets': {}, 'limit': 20, 'offset': 0, 'searchText': search_text}, headers=headers, timeout=5)

        if r.status_code == 200:
            data = r.json()
            total = data.get('total', 0)
            raw_postings = list(data.get('jobPostings', []))

            # Automatically paginate to collect all postings
            offset = 20
            MAX_JOBS_PER_COMPANY = 500
            while offset < total and offset < MAX_JOBS_PER_COMPANY:
                r_page = requests.post(api_url, json={'appliedFacets': {}, 'limit': 20, 'offset': offset, 'searchText': search_text}, headers=headers, timeout=5)
                if r_page.status_code != 200:
                    break
                batch = r_page.json().get('jobPostings', [])
                if not batch:
                    break
                raw_postings.extend(batch)
                offset += 20

            session = requests.Session()
            adapter = HTTPAdapter(
                pool_connections=15,
                pool_maxsize=15,
                max_retries=Retry(
                    total=3,
                    backoff_factor=0.5,
                    status_forcelist=[429, 500, 502, 503, 504],
                    raise_on_status=False
                )
            )
            session.mount('https://', adapter)
            session.mount('http://', adapter)
            session.headers.update(headers)

            # Fetch full job descriptions for US jobs in parallel with retries
            def _fetch_one_detail(item):
                ext_path = item.get('externalPath', '')
                detail_url = f"https://{host}/wday/cxs/{tenant}/{site}{ext_path}"
                desc_html = ''
                desc_text = ''
                is_us = False
                loc = item.get('locationsText') or ''

                for attempt in range(3):
                    try:
                        res = session.get(detail_url, timeout=12)
                        if res.status_code == 429:
                            time.sleep(1.5 * (attempt + 1))
                            continue
                        if res.status_code == 200:
                            data = res.json()
                            posting_info = data.get('jobPostingInfo', {})
                            desc_html = posting_info.get('jobDescription', '') or ''
                            # Plain text used only for skills/experience extraction
                            desc_text = text_from_html(desc_html)
                            country_desc = (posting_info.get('country', {}) or {}).get('descriptor', '')
                            info_loc = posting_info.get('location', '')
                            addl_locs = posting_info.get('additionalLocations', []) or []

                            # If Workday explicitly specifies a foreign country, reject unless it's US or UK
                            if country_desc and 'united states' not in country_desc.lower() and 'united kingdom' not in country_desc.lower():
                                if not any(is_us_or_uk_location(l) for l in addl_locs):
                                    return None

                            # Determine if US or UK
                            if ('united states' in country_desc.lower() or
                                is_us_location(loc) or is_us_location(info_loc) or
                                any(is_us_location(l) for l in addl_locs)):
                                country_tag = 'US'
                            elif ('united kingdom' in country_desc.lower() or
                                  is_uk_location(loc) or is_uk_location(info_loc) or
                                  any(is_uk_location(l) for l in addl_locs)):
                                country_tag = 'UK'
                            else:
                                return None

                            if info_loc and (not loc or 'location' in loc.lower()):
                                loc = info_loc
                            break
                    except Exception:
                        if attempt < 2:
                            time.sleep(1.0 * (attempt + 1))
                else:
                    return None

                title = item.get('title', 'Unknown Title')
                combined_text = f"{title} {desc_text}"
                job_url = f"https://{host}/en-US/{site}" + (ext_path if ext_path.startswith('/') else f"/{ext_path}") if ext_path else working_url
                return {
                    'title': title,
                    'location': loc,
                    'company': company_name,
                    'jobUrl': job_url,
                    'description': desc_html,          # raw HTML stored as description
                    'skills': extract_skills(combined_text, company_name),
                    'yearOfExperience': parse_experience(desc_text),
                    'country_tag': country_tag
                }

            if raw_postings:
                us_count = 0
                uk_count = 0
                batch_size = 20
                with ThreadPoolExecutor(max_workers=15) as pool:
                    for i in range(0, len(raw_postings), batch_size):
                        batch = raw_postings[i:i+batch_size]
                        futures = [pool.submit(_fetch_one_detail, item) for item in batch]
                        for future in as_completed(futures):
                            try:
                                res = future.result()
                                if res is not None:
                                    tag = res.pop('country_tag', None)
                                    if tag == 'US' and us_count < 50:
                                        res['country'] = 'US'
                                        jobs.append(res)
                                        us_count += 1
                                    elif tag == 'UK' and uk_count < 50:
                                        res['country'] = 'UK'
                                        jobs.append(res)
                                        uk_count += 1
                            except Exception:
                                pass
                                
                        if us_count >= 50 and uk_count >= 50:
                            break
            return jobs
    except Exception:
        pass
    return jobs

_URL_MAP_JSON = os.path.join(_DIR, "workday_company_urls.json")

def get_pre_mapped_url(company_name):
    """Check if company URL was pre-discovered in workday_company_urls.json."""
    if os.path.exists(_URL_MAP_JSON):
        try:
            with open(_URL_MAP_JSON, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if company_name in data:
                    return data[company_name]
                clean_name = company_name.strip().lower()
                for k, v in data.items():
                    if k.strip().lower() == clean_name:
                        return v
        except Exception:
            pass
    return "NOT_FOUND"

def scrape_workday_company_selenium(company_name):
    """Scrape Workday company using fast concurrent URL pre-check, CXS API, and Selenium fallback.
    
    Prevents false-positives on Workday 500 maintenance redirect pages and 406 error pages.
    """
    working_url = None

    # 0. Fast pre-mapped URL lookup (instant, zero HTTP requests)
    mapped_url = get_pre_mapped_url(company_name)
    if mapped_url is None:
        return {'success': False, 'error': 'Non-Workday company (verified in url map)', 'jobs_count': 0}
    elif mapped_url != "NOT_FOUND":
        working_url = mapped_url

    urls = get_workday_career_url(company_name)
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
    }
    
    def _check_candidate(url):
        try:
            r = requests.get(url, headers=headers, timeout=1.5, allow_redirects=True)
            if r.status_code == 200:
                src = r.text.lower()
                if 'maintenance-page' not in src and 'http error 406' not in src and "we can't find that page" not in src and "we can’t find that page" not in src:
                    return url
        except Exception:
            pass
        return None

    # 1. If not pre-mapped, probe candidate URLs concurrently
    if not working_url:
        if urls and urls[0] in KNOWN_WORKDAY_URLS.values():
            if _check_candidate(urls[0]):
                working_url = urls[0]

    if not working_url:
        valid_candidates = []
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(_check_candidate, u): (idx, u) for idx, u in enumerate(urls[:70])}
            for f in as_completed(futures):
                res = f.result()
                if res:
                    idx, u = futures[f]
                    valid_candidates.append((idx, u))
        if valid_candidates:
            valid_candidates.sort(key=lambda x: x[0])  # Preserve original priority order
            working_url = valid_candidates[0][1]
            
    if not working_url:
        return {'success': False, 'error': 'Could not find valid Workday URL', 'jobs_count': 0}

    # 2. Fast CXS REST API attempt (instant, structured JSON response with full descriptions)
    cxs_jobs = try_workday_cxs_api(working_url, company_name)
    if cxs_jobs:
        return {
            'success': True,
            'jobs_count': len(cxs_jobs),
            'jobs': cxs_jobs
        }

    # 3. Selenium fallback with WebDriverWait and modern selectors
    options = uc.ChromeOptions()
    options.add_argument('--headless')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--disable-gpu')
    options.add_argument('--window-size=1920,1080')
    
    driver = None
    jobs = []
    try:
        with _chrome_init_lock:
            driver = uc.Chrome(options=options, version_main=CHROME_VERSION)
        stealth(driver, languages=["en-US", "en"], vendor="Google Inc.", platform="Win32", webgl_vendor="Intel Inc.", renderer="Intel Iris OpenGL Engine", fix_hairline=True)
        
        driver.get(working_url)
        
        # Wait up to 8 seconds for job listings to render
        try:
            WebDriverWait(driver, 8).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, 'a[data-automation-id="jobTitle"], [data-automation-id="keywordSearchInput"], [data-automation-id="compositeContainer"]'))
            )
        except Exception:
            pass

        # Verify page is not a maintenance or error page
        page_src = driver.page_source.lower()
        if 'maintenance-page' in page_src or 'http error 406' in page_src or 'err_name_not_resolved' in page_src:
            return {'success': False, 'error': 'Page loaded error/maintenance redirect', 'jobs_count': 0}

        # Modern Workday selector for job titles
        title_elements = driver.find_elements(By.CSS_SELECTOR, 'a[data-automation-id="jobTitle"]')
        if not title_elements:
            # Older Workday container selector fallback
            title_elements = driver.find_elements(By.CSS_SELECTOR, '[data-automation-id="compositeContainer"] [data-automation-id="jobTitle"], .css-1m740h5 [data-automation-id="jobTitle"]')

        for a in title_elements:
            try:
                title = a.text.strip()
                if not title:
                    continue
                job_url = a.get_attribute('href') or working_url
                
                location = ''
                try:
                    card = a.find_element(By.XPATH, './ancestor::li')
                    loc_elems = card.find_elements(By.CSS_SELECTOR, '[data-automation-id="locations"], [data-automation-id="location"], dd')
                    for le in loc_elems:
                        txt = le.text.replace('locations\n', '').strip()
                        if txt:
                            location = txt
                            break
                except Exception:
                    pass

                if is_us_or_uk_location(location):
                    # Attempt to load the individual job detail page to get full HTML description
                    selenium_desc_html = ''
                    selenium_desc_text = ''
                    if job_url and job_url != working_url:
                        try:
                            driver.get(job_url)
                            WebDriverWait(driver, 8).until(
                                EC.presence_of_element_located((By.CSS_SELECTOR,
                                    '[data-automation-id="jobPostingDescription"], '
                                    '[data-automation-id="job-description"], '
                                    '.css-cygeeu, [class*="jobDescription"]'))
                            )
                            desc_elems = driver.find_elements(By.CSS_SELECTOR,
                                '[data-automation-id="jobPostingDescription"], '
                                '[data-automation-id="job-description"], '
                                '.css-cygeeu, [class*="jobDescription"]')
                            if desc_elems:
                                selenium_desc_html = desc_elems[0].get_attribute('innerHTML') or ''
                                # Plain text used only for skills/experience extraction
                                selenium_desc_text = text_from_html(selenium_desc_html)
                            driver.back()
                        except Exception:
                            pass

                    skills = extract_skills(f"{title} {selenium_desc_text}", company_name)
                    jobs.append({
                        'title': title,
                        'location': location,
                        'company': company_name,
                        'jobUrl': job_url,
                        'description': selenium_desc_html,   # raw HTML stored as description
                        'skills': skills,
                        'yearOfExperience': parse_experience(selenium_desc_text or title)
                    })
            except Exception:
                continue

        return {
            'success': True,
            'jobs_count': len(jobs),
            'jobs': jobs
        }
    except Exception as e:
        if jobs:
            return {'success': False, 'error': str(e), 'jobs_count': len(jobs), 'jobs': jobs}
        return {'success': False, 'error': str(e), 'jobs_count': 0}
    finally:
        if driver:
            driver.quit()

def scrape_workday_company_requests(company_name):
    """Scrape Workday company using requests (for API-based scraping)"""
    return {'success': False, 'error': 'Requests method not implemented for Workday', 'jobs_count': 0}

def map_workday_job(job_data, company_name):
    """Map Workday job to database model. jobDescription stores the original HTML from Workday."""
    # 'description' already holds raw HTML (set by _fetch_one_detail / Selenium fallback)
    description_html = job_data.get('description') or job_data.get('descriptionHtml') or ''
    # Plain text extracted only for skills/experience parsing
    description_text = text_from_html(description_html)

    title = job_data.get('title', 'Unknown Title')
    combined_text = f"{title} {description_text}"
    post_time = datetime.now().isoformat()
    
    skills = job_data.get('skills')
    if skills is None:
        skills = extract_skills(combined_text, company_name)
        
    experience = job_data.get('yearOfExperience')
    if experience is None:
        experience = parse_experience(description_text)
    
    return {
        'jobId': f"workday_{company_name.lower().replace(' ', '_')}_{hash(title)}",
        'jobTitle': title[:500],
        'companyName': company_name[:255],
        'companyLogo': None,
        'companyLocation': job_data.get('location', 'Unknown')[:255],
        'jobLocation': job_data.get('location', 'Unknown')[:255],
        'jobType': 'Full-time',
        'yearOfExperience': experience,
        'skills': skills,
        'jobPostTime': post_time,
        'jobDescription': description_html or '',   # original HTML from Workday
        'salary': None,
        'category': 'Workday Jobs',
        'jobSource': 'Workday',
        'jobUrl': job_data.get('jobUrl'),
        'createdAt': post_time,
        'updatedAt': post_time
    }

def save_jobs_to_db(jobs):
    """Save jobs to database via the centralised push_to_db module."""
    return push_jobs_list(jobs)

# save_to_xlsx removed — output is now DB-only (+ optional JSON backup).

def save_to_json(jobs):
    """Save scraped jobs to a JSON file matching the Job model schema.
    
    Format matches class Job(Base):
      id                Integer primary key (auto-incrementing 1, 2, 3...)
      jobId             String unique identifier
      jobTitle          String
      companyName       String
      companyLogo       String or null
      companyLocation   String or null
      jobLocation       String
      jobType           String
      yearOfExperience  String or null
      skills            JSON list of strings
      jobPostTime       DateTime ISO string
      jobDescription    Text description
      salary            String or null
      jobSource         String
      category          String
      jobUrl            String
      createdAt         DateTime ISO string
      updatedAt         DateTime ISO string
    
    Deduplicates by jobId and persists atomically to prevent data loss.
    """
    existing_jobs = []
    seen_job_ids = set()
    job_index_by_id = {}
    max_id = 0

    if os.path.exists(JSON_OUTPUT_FILE):
        try:
            with open(JSON_OUTPUT_FILE, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                if content:
                    existing_jobs = json.loads(content)
                    if isinstance(existing_jobs, list):
                        for idx, j in enumerate(existing_jobs):
                            if isinstance(j, dict):
                                j_id = j.get('jobId')
                                if j_id:
                                    seen_job_ids.add(j_id)
                                    job_index_by_id[j_id] = idx
                                cur_id = j.get('id')
                                if isinstance(cur_id, int) and cur_id > max_id:
                                    max_id = cur_id
                    else:
                        existing_jobs = []
        except Exception as e:
            print(f"[warn] Could not read existing {JSON_OUTPUT_FILE}: {e}")
            existing_jobs = []

    new_jobs_to_add = []
    updated_count = 0
    now_iso = datetime.now().isoformat()

    for job in jobs:
        job_id = job.get('jobId')
        new_desc = job.get('jobDescription') or ''
        has_new_desc = bool(new_desc and 'not available' not in new_desc.lower())

        if job_id and job_id in seen_job_ids:
            # If existing record has missing description, but new job has valid description, enrich it
            existing_idx = job_index_by_id.get(job_id)
            if existing_idx is not None and has_new_desc:
                existing_record = existing_jobs[existing_idx]
                old_desc = existing_record.get('jobDescription') or ''
                if not old_desc or 'not available' in old_desc.lower():
                    existing_record['jobDescription'] = new_desc
                    if job.get('skills'):
                        existing_record['skills'] = job.get('skills')
                    if job.get('yearOfExperience'):
                        existing_record['yearOfExperience'] = job.get('yearOfExperience')
                    existing_record['updatedAt'] = now_iso
                    updated_count += 1
            continue

        max_id += 1
        job_record = {
            "id": max_id,
            "jobId": job.get('jobId'),
            "jobTitle": job.get('jobTitle'),
            "companyName": job.get('companyName'),
            "companyLogo": job.get('companyLogo'),
            "companyLocation": job.get('companyLocation'),
            "jobLocation": job.get('jobLocation'),
            "jobType": job.get('jobType', 'Full-time'),
            "yearOfExperience": job.get('yearOfExperience'),
            "skills": job.get('skills') if job.get('skills') is not None else [],
            "jobPostTime": job.get('jobPostTime') or now_iso,
            "jobDescription": job.get('jobDescription'),   # original HTML from Workday
            "salary": job.get('salary'),
            "jobSource": job.get('jobSource', 'Workday'),
            "category": job.get('category', 'Workday Jobs'),
            "jobUrl": job.get('jobUrl'),
            "createdAt": job.get('createdAt') or now_iso,
            "updatedAt": job.get('updatedAt') or now_iso
        }
        new_jobs_to_add.append(job_record)
        if job_id:
            seen_job_ids.add(job_id)

    if not new_jobs_to_add and updated_count == 0:
        return 0

    combined = existing_jobs + new_jobs_to_add

    # Atomically write JSON output
    tmp_path = JSON_OUTPUT_FILE + ".tmp"
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(combined, f, indent=2, ensure_ascii=False, default=str)
    os.replace(tmp_path, JSON_OUTPUT_FILE)

    return len(new_jobs_to_add) + updated_count

def load_progress():
    """Load progress from file.
    
    Handles three cases gracefully:
      - File doesn't exist → return fresh default state
      - File exists but is empty → return fresh default state  
      - File exists but has corrupt JSON → return fresh default state
    """
    _default = {
        'completed_companies': [],
        'failed_companies': {},
        'total_jobs_scraped': 0,
        'last_updated': None,
        'status': 'not_started'
    }
    if not os.path.exists(PROGRESS_FILE):
        return _default
    try:
        with open(PROGRESS_FILE, 'r') as f:
            content = f.read().strip()
            if not content:          # empty file
                print("[warn] workday_progress.json is empty — starting fresh")
                return _default
            return json.loads(content)
    except json.JSONDecodeError as e:
        print(f"[warn] workday_progress.json is corrupt ({e}) — starting fresh")
        return _default

def save_progress(progress):
    """Save progress to file"""
    progress['last_updated'] = datetime.now().isoformat()
    with open(PROGRESS_FILE, 'w') as f:
        json.dump(progress, f, indent=2)

# ─────────────────────────────────────────────────────────────
# Parallel worker infrastructure (only active when USE_WORKERS = True)
# ─────────────────────────────────────────────────────────────
_progress_lock    = threading.Lock()   # protects shared progress dict across threads
_chrome_init_lock = threading.Lock()   # prevents ChromeDriver patcher race condition on startup

def _scrape_one(company_name, progress, worker_id):
    """Worker function: scrape one company and safely update shared progress.
    
    Each worker runs its own headless Chrome instance.
    The shared progress dict is guarded by _progress_lock to prevent race conditions.
    """
    tag = f"[W{worker_id}]"
    print(f"{tag} [{datetime.now().strftime('%H:%M:%S')}] Scraping {company_name}...")

    result = scrape_workday_company_selenium(company_name)

    with _progress_lock:
        # Save any jobs that were fetched, regardless of overall success
        if result.get('jobs_count', 0) > 0:
            mapped_jobs = [map_workday_job(job, company_name) for job in result.get('jobs', [])]

            saved = save_jobs_to_db(mapped_jobs)
            status_char = "✓" if result['success'] else "!"
            print(f"{tag}   {status_char} {company_name}: {result['jobs_count']} jobs → DB ({saved} saved)")

            if SAVE_TO_JSON:
                json_saved = save_to_json(mapped_jobs)
                print(f"{tag}   {status_char} {company_name}: {result['jobs_count']} jobs → JSON ({json_saved} added)")

            progress['total_jobs_scraped'] += saved
            
            if result['success']:
                progress['completed_companies'].append(company_name)
            else:
                short_err = str(result['error'])[:120]
                print(f"{tag}   ✗ {company_name} (Partial): {short_err}")
                progress['failed_companies'][company_name] = result['error']

        elif result.get('success'):
            print(f"{tag}   ✓ {company_name}: no jobs found")
            progress['completed_companies'].append(company_name)

        else:
            short_err = str(result.get('error', 'Unknown'))[:120]
            print(f"{tag}   ✗ {company_name}: {short_err}")
            progress['failed_companies'][company_name] = result.get('error', 'Unknown Error')

        save_progress(progress)

def scrape_all_workday():
    """Main function to scrape all Workday companies.
    
    Branches on USE_WORKERS:
      True  → parallel mode   (WORKERS Chrome instances run simultaneously)
      False → sequential mode (original one-at-a-time loop, untouched)
    """
    companies = load_companies()
    progress = load_progress()

    mode      = "DATABASE" if SAVE_TO_DATABASE else f"XLSX → {XLSX_OUTPUT_FILE}"
    exec_mode = f"PARALLEL ({WORKERS} workers)" if USE_WORKERS else "SEQUENTIAL"
    print(f"=== Workday Scraper Started ===")
    print(f"Save mode       : {mode}")
    print(f"Execution mode  : {exec_mode}")
    print(f"Total companies : {len(companies)}")
    print(f"Already completed: {len(progress['completed_companies'])}")
    print(f"Failed companies: {len(progress['failed_companies'])}")
    print(f"Total jobs scraped so far: {progress['total_jobs_scraped']}")
    print()

    progress['status'] = 'running'
    save_progress(progress)

    if USE_WORKERS:
        # ── PARALLEL MODE ──────────────────────────────────────────────────
        todo = [c for c in companies if c not in progress['completed_companies']]
        print(f"Companies to process: {len(todo)}")
        print()

        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            futures = {}
            for i, company_name in enumerate(todo):
                worker_id = (i % WORKERS) + 1
                future = executor.submit(_scrape_one, company_name, progress, worker_id)
                futures[future] = company_name
                # Stagger Chrome launches by 1s to avoid port collisions at startup
                time.sleep(1)

            for future in as_completed(futures):
                company_name = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    print(f"[!] Unexpected exception for {company_name}: {exc}")
                    with _progress_lock:
                        progress['failed_companies'][company_name] = str(exc)
                        save_progress(progress)

    else:
        # ── SEQUENTIAL MODE (original logic, untouched) ──────────────────────
        for company_name in companies:
            # Skip if already completed
            if company_name in progress['completed_companies']:
                print(f"Skipping {company_name} (already completed)")
                continue

            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Scraping {company_name}...")

            # Try Selenium first (more reliable for Workday)
            result = scrape_workday_company_selenium(company_name)

            if result['success'] and result['jobs_count'] > 0:
                mapped_jobs = [map_workday_job(job, company_name) for job in result['jobs']]

                saved = save_jobs_to_db(mapped_jobs)
                print(f"  ✓ Scraped {result['jobs_count']} jobs (saved {saved} to DB)")

                if SAVE_TO_JSON:
                    json_saved = save_to_json(mapped_jobs)
                    print(f"  ✓ Saved {json_saved} jobs to JSON ({JSON_OUTPUT_FILE})")

                progress['total_jobs_scraped'] += saved
                progress['completed_companies'].append(company_name)
            elif result['success'] and result['jobs_count'] == 0:
                print(f"  ✓ No jobs found")
                progress['completed_companies'].append(company_name)
            else:
                print(f"  ✗ Failed: {result['error']}")
                progress['failed_companies'][company_name] = result['error']

            # Save progress after each company
            save_progress(progress)

            # Delay to avoid rate limiting
            time.sleep(2)


    progress['status'] = 'completed'
    save_progress(progress)
    
    print()
    print("=== Workday Scraper Completed ===")
    print(f"Total companies: {len(companies)}")
    print(f"Completed: {len(progress['completed_companies'])}")
    print(f"Failed: {len(progress['failed_companies'])}")
    print(f"Total jobs scraped: {progress['total_jobs_scraped']}")

if __name__ == "__main__":
    check_db_connection()   # ← verify DB is reachable before scraping
    scrape_all_workday()
