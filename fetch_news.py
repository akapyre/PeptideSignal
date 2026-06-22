#!/usr/bin/env python3
"""
PYRE Peptide News Fetcher
==========================
Pulls peptide-related news/research from:
  - PubMed (E-utilities API)
  - ClinicalTrials.gov (v2 API)
  - FDA (openFDA + RSS where available)
  - General news (via NewsAPI or similar, swap in your key)

Writes results to data/news.json in a schema the static site reads.

USAGE:
    python fetch_news.py

CONFIG:
    Edit COMPOUNDS below to track the peptides relevant to your content.
    Set NEWSAPI_KEY as an environment variable if you want general news search
    (free tier at https://newsapi.org). PubMed/ClinicalTrials.gov need no key.

This script is meant to run on a schedule via GitHub Actions (see
.github/workflows/fetch.yml) or any machine with internet access and cron.
It does NOT run inside Claude's sandboxed preview environment, which has no
outbound network access.
"""

import json
import os
import re
import time
import hashlib
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from xml.etree import ElementTree

# ---------------------------------------------------------------------------
# CONFIG — edit this list to match what your community cares about
# ---------------------------------------------------------------------------
COMPOUNDS = [
    "BPC-157", "TB-500", "retatrutide", "tesamorelin", "GHK-Cu", "CJC-1295",
    "Ipamorelin", "MOTS-c", "SS-31", "Semax", "Selank", "kisspeptin",
    "ARA-290", "LL-37", "SLU-PP-332", "PE-22-28", "BAM-15", "5-Amino-1MQ",
    "AOD-9604", "Thymosin Alpha-1", "KPV", "Melanotan",
]

OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "news.json")
NEWSAPI_KEY = os.environ.get("NEWSAPI_KEY", "")  # optional, set in repo secrets
USER_AGENT = "PYRE-PeptideNews/1.0 (research aggregator; contact: you@example.com)"

MAX_PER_COMPOUND_PUBMED = 3
MAX_PER_COMPOUND_TRIALS = 3
REQUEST_DELAY_SECONDS = 0.4  # be polite to free public APIs


def http_get(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read()


def make_id(*parts):
    raw = "|".join(parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# PubMed (E-utilities) — no API key required, rate limit ~3 req/sec
# ---------------------------------------------------------------------------
def fetch_pubmed(compound, max_results=MAX_PER_COMPOUND_PUBMED):
    items = []
    try:
        search_url = (
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?"
            + urllib.parse.urlencode({
                "db": "pubmed",
                "term": f"{compound}[Title/Abstract]",
                "retmax": max_results,
                "sort": "date",
                "retmode": "json",
            })
        )
        search_data = json.loads(http_get(search_url))
        ids = search_data.get("esearchresult", {}).get("idlist", [])
        if not ids:
            return items

        time.sleep(REQUEST_DELAY_SECONDS)
        summary_url = (
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?"
            + urllib.parse.urlencode({"db": "pubmed", "id": ",".join(ids), "retmode": "json"})
        )
        summary_data = json.loads(http_get(summary_url))
        result = summary_data.get("result", {})

        for pmid in ids:
            doc = result.get(pmid)
            if not doc:
                continue
            title = doc.get("title", "").strip()
            pubdate = doc.get("pubdate", "")
            source = doc.get("source", "PubMed")
            items.append({
                "id": make_id("pubmed", pmid),
                "category": "research",
                "compound": compound,
                "title": title or f"New PubMed entry on {compound}",
                "summary": f"Published in {source}. See abstract for full findings.",
                "why_it_matters": "",
                "source": f"PubMed ({source})",
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                "published": normalize_date(pubdate),
                "tags": [compound.lower(), "research", "pubmed"],
            })
    except Exception as e:
        print(f"  [pubmed] error for {compound}: {e}")
    return items


# ---------------------------------------------------------------------------
# ClinicalTrials.gov v2 API — no API key required
# ---------------------------------------------------------------------------
def fetch_clinicaltrials(compound, max_results=MAX_PER_COMPOUND_TRIALS):
    items = []
    try:
        url = (
            "https://clinicaltrials.gov/api/v2/studies?"
            + urllib.parse.urlencode({
                "query.term": compound,
                "pageSize": max_results,
                "sort": "LastUpdatePostDate:desc",
            })
        )
        data = json.loads(http_get(url))
        for study in data.get("studies", []):
            protocol = study.get("protocolSection", {})
            ident = protocol.get("identificationModule", {})
            status = protocol.get("statusModule", {})
            nct_id = ident.get("nctId", "")
            title = ident.get("briefTitle", "")
            overall_status = status.get("overallStatus", "")
            last_update = status.get("lastUpdatePostDateStruct", {}).get("date", "")

            items.append({
                "id": make_id("ctgov", nct_id),
                "category": "clinical-trial",
                "compound": compound,
                "title": title or f"Clinical trial update: {compound}",
                "summary": f"Status: {overall_status}. Registry entry updated {last_update}.",
                "why_it_matters": "",
                "source": "ClinicalTrials.gov",
                "url": f"https://clinicaltrials.gov/study/{nct_id}",
                "published": normalize_date(last_update),
                "tags": [compound.lower(), "clinicaltrials.gov", overall_status.lower()],
            })
    except Exception as e:
        print(f"  [clinicaltrials] error for {compound}: {e}")
    return items


# ---------------------------------------------------------------------------
# openFDA — drug enforcement / event reports (no key required, rate limited)
# ---------------------------------------------------------------------------
def fetch_openfda(compound):
    items = []
    try:
        url = (
            "https://api.fda.gov/drug/enforcement.json?"
            + urllib.parse.urlencode({
                "search": f'product_description:"{compound}"',
                "limit": 3,
            })
        )
        data = json.loads(http_get(url))
        for r in data.get("results", []):
            items.append({
                "id": make_id("fda", r.get("recall_number", compound)),
                "category": "regulatory",
                "compound": compound,
                "title": f"FDA enforcement report: {r.get('product_description', compound)[:120]}",
                "summary": r.get("reason_for_recall", "")[:400],
                "why_it_matters": "",
                "source": "openFDA (drug enforcement)",
                "url": "https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts",
                "published": normalize_date(r.get("report_date", "")),
                "tags": [compound.lower(), "FDA", "enforcement"],
            })
    except Exception as e:
        # openFDA returns 404 JSON when there are zero matches — not a real error
        if "404" not in str(e):
            print(f"  [openfda] error for {compound}: {e}")
    return items


# ---------------------------------------------------------------------------
# General news (optional — requires NEWSAPI_KEY env var)
# ---------------------------------------------------------------------------
def fetch_general_news(compound):
    items = []
    if not NEWSAPI_KEY:
        return items
    try:
        url = (
            "https://newsapi.org/v2/everything?"
            + urllib.parse.urlencode({
                "q": f'"{compound}" peptide',
                "sortBy": "publishedAt",
                "language": "en",
                "pageSize": 3,
                "apiKey": NEWSAPI_KEY,
            })
        )
        data = json.loads(http_get(url))
        for a in data.get("articles", []):
            items.append({
                "id": make_id("news", a.get("url", compound)),
                "category": "news",
                "compound": compound,
                "title": a.get("title", ""),
                "summary": (a.get("description") or "")[:400],
                "why_it_matters": "",
                "source": (a.get("source") or {}).get("name", "News"),
                "url": a.get("url", ""),
                "published": normalize_date(a.get("publishedAt", "")[:10]),
                "tags": [compound.lower(), "news"],
            })
    except Exception as e:
        print(f"  [newsapi] error for {compound}: {e}")
    return items


def normalize_date(raw):
    """Best-effort normalize various date formats to YYYY-MM-DD."""
    if not raw:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")
    raw = raw.strip()
    # PubMed dates are messy: "2026 May 21", "2026 May", "2026"
    m = re.match(r"(\d{4})[-\s]?([A-Za-z]{3})?[-\s]?(\d{1,2})?", raw)
    if m:
        year = m.group(1)
        month_str = m.group(2)
        day = m.group(3) or "01"
        months = {"Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04", "May": "05",
                  "Jun": "06", "Jul": "07", "Aug": "08", "Sep": "09", "Oct": "10",
                  "Nov": "11", "Dec": "12"}
        month = months.get(month_str, "01") if month_str else "01"
        return f"{year}-{month}-{day.zfill(2)}"
    if re.match(r"\d{4}-\d{2}-\d{2}", raw):
        return raw[:10]
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def dedupe(items):
    seen = set()
    out = []
    for item in items:
        key = item["id"]
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def main():
    all_items = []
    print(f"Fetching peptide news for {len(COMPOUNDS)} compounds...")

    for compound in COMPOUNDS:
        print(f"-> {compound}")
        all_items.extend(fetch_pubmed(compound))
        time.sleep(REQUEST_DELAY_SECONDS)
        all_items.extend(fetch_clinicaltrials(compound))
        time.sleep(REQUEST_DELAY_SECONDS)
        all_items.extend(fetch_openfda(compound))
        time.sleep(REQUEST_DELAY_SECONDS)
        all_items.extend(fetch_general_news(compound))
        time.sleep(REQUEST_DELAY_SECONDS)

    all_items = dedupe(all_items)
    all_items.sort(key=lambda x: x.get("published", ""), reverse=True)

    # Preserve any hand-written "why_it_matters" notes from the existing file
    # by merging on id, so manual edits aren't clobbered by re-runs.
    existing = {}
    if os.path.exists(OUTPUT_PATH):
        try:
            with open(OUTPUT_PATH) as f:
                old_data = json.load(f)
            existing = {i["id"]: i for i in old_data.get("items", [])}
        except Exception:
            pass

    for item in all_items:
        old = existing.get(item["id"])
        if old and old.get("why_it_matters") and not item.get("why_it_matters"):
            item["why_it_matters"] = old["why_it_matters"]

    output = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "items": all_items[:200],  # cap file size
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nWrote {len(output['items'])} items to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
