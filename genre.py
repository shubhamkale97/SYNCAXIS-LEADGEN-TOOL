"""Infer a company's industry (genre) from the job postings it advertises.

Two stages, cheapest first:

1. Heuristics - keyword/skill scoring against a fixed taxonomy.  Title matches
   count for more than body matches.
2. LLM fallback - only when the heuristic result is weak *and* an
   OpenAI-compatible endpoint is configured (LLM_API_KEY / LLM_BASE_URL /
   LLM_MODEL).  Degrades silently to the heuristic answer if unavailable.
"""

import json
import os
import re
import urllib.request

# Each industry maps to lower-case keywords/phrases.  Keep phrases specific so
# generic words do not cross-match (e.g. "engineer" is deliberately absent).
TAXONOMY = {
    "Electronics & Semiconductor Manufacturing": [
        "pcb", "pcba", "smt", "soldering", "solder", "semiconductor", "wafer",
        "electronics", "embedded hardware", "surface mount", "reflow",
        "pick and place", "cleanroom", "microelectronic", "aoi", "ict test",
    ],
    "Precision Engineering & Motion Control": [
        "motion control", "servo", "actuator", "encoder", "gearbox", "cnc",
        "precision", "tolerance", "stepper", "brushless", "linear stage",
        "ballscrew", "metrology", "portescap", "winding", "coil winding",
    ],
    "Industrial Automation & Robotics": [
        "plc", "scada", "robotics", "robot", "automation", "hmi",
        "industrial iot", "machine vision", "cobot", "siemens", "allen bradley",
    ],
    "Software & IT Services": [
        "software", "developer", "backend", "frontend", "full stack", "python",
        "java", "react", "node", "api", "devops", "cloud", "aws", "kubernetes",
        "data engineer", "saas", "microservices", "mobile app", "qa engineer",
    ],
    "Mechanical & Metal Fabrication": [
        "mechanical", "fabrication", "welding", "machining", "lathe", "milling",
        "sheet metal", "tooling", "casting", "forging", "die", "mould",
    ],
    "Automotive & EV": [
        "automotive", "vehicle", "ev", "electric vehicle", "powertrain",
        "chassis", "adas", "battery pack", "auto components", "tier 1",
    ],
    "Aerospace & Defence": [
        "aerospace", "avionics", "defence", "defense", "satellite", "spacecraft",
        "uav", "drone", "isro",
    ],
    "Pharmaceuticals & Healthcare": [
        "pharma", "pharmaceutical", "clinical", "drug", "gmp", "formulation",
        "medical device", "nurse", "hospital", "biotech", "regulatory affairs",
    ],
    "Logistics & Supply Chain": [
        "logistics", "supply chain", "warehouse", "procurement", "sourcing",
        "dispatch", "freight", "inventory", "3pl", "exim",
    ],
    "Construction & Infrastructure": [
        "construction", "civil", "site engineer", "structural", "rcc",
        "infrastructure", "contractor", "surveyor", "hvac", "mep",
    ],
    "Energy & Power": [
        "solar", "renewable", "power plant", "substation", "transformer",
        "grid", "oil and gas", "turbine", "electrical engineer",
    ],
    "Chemicals & Plastics": [
        "chemical", "polymer", "plastic", "injection moulding", "resin",
        "adhesive", "coating", "petrochemical", "process engineer",
    ],
    "Textiles & Apparel": [
        "textile", "garment", "apparel", "spinning", "weaving", "dyeing",
        "fashion", "merchandiser", "knitwear",
    ],
    "Food & Beverage": [
        "food", "beverage", "dairy", "bakery", "restaurant", "chef",
        "brewery", "food packaging",
    ],
    "Retail & E-commerce": [
        "retail", "e-commerce", "ecommerce", "merchandising",
        "category manager", "marketplace", "shopify",
    ],
    "Finance & Fintech": [
        "fintech", "banking", "accounting", "audit", "taxation", "investment",
        "insurance", "nbfc", "treasury", "finance",
    ],
    "Marketing & Advertising": [
        "marketing", "seo", "advertising", "brand", "digital marketing",
        "social media", "content", "campaign", "growth",
    ],
    "Telecommunications": [
        "telecom", "5g", "rf engineer", "network engineer", "optical fiber",
        "wireless", "base station", "lte",
    ],
    "Education & Training": [
        "teacher", "trainer", "faculty", "curriculum", "tutor", "instructor",
        "edtech",
    ],
    "Agriculture & Agritech": [
        "agriculture", "agritech", "farm", "crop", "irrigation", "agronomy",
        "horticulture",
    ],
}

TITLE_WEIGHT = 3
BODY_WEIGHT = 1
LLM_THRESHOLD = 0.45


def heuristic_genre(jobs):
    """Score a job corpus against the taxonomy; return the best match or None."""
    scores = {genre: 0.0 for genre in TAXONOMY}
    matched = {genre: set() for genre in TAXONOMY}
    for job in jobs:
        title = (job.get("title") or "").lower()
        body = " ".join(
            str(job.get(key) or "")
            for key in ("title", "description", "company_industry", "skills")
        ).lower()
        for genre, keywords in TAXONOMY.items():
            for keyword in keywords:
                if keyword in title:
                    scores[genre] += TITLE_WEIGHT
                    matched[genre].add(keyword)
                elif keyword in body:
                    scores[genre] += BODY_WEIGHT
                    matched[genre].add(keyword)
    best = max(scores, key=scores.get)
    if scores[best] <= 0:
        return None
    total = sum(scores.values()) or 1.0
    confidence = round(min(0.95, scores[best] / total), 2)
    return {
        "genre": best,
        "confidence": confidence,
        "score": scores[best],
        "matched_keywords": sorted(matched[best]),
        "observed_industries": sorted(
            {job.get("company_industry") for job in jobs if job.get("company_industry")}
        ),
    }


def llm_genre(jobs):
    """Classify with an OpenAI-compatible chat endpoint, if configured."""
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    if not api_key:
        return None
    base_url = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("LLM_MODEL", "gpt-4o-mini")
    sample = "\n".join(
        f"- {job.get('title', '')}: {(job.get('description') or '')[:160]}"
        for job in jobs[:25]
    )
    prompt = (
        "You classify a company's industry from its job postings. "
        'Reply with JSON only: {"genre": str, "confidence": 0-1, "reasoning": str}.'
        f"\n\nJob postings:\n{sample}"
    )
    payload = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        }
    ).encode()
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read())
        content = data["choices"][0]["message"]["content"]
        match = re.search(r"\{.*\}", content, re.S)
        return json.loads(match.group(0)) if match else None
    except Exception:  # noqa: BLE001 - never let the fallback break the request
        return None


def infer_genre(jobs, llm_threshold=LLM_THRESHOLD):
    """Return {genre, confidence, method, ...} for a corpus of job postings."""
    heuristic = heuristic_genre(jobs)
    if heuristic and heuristic["confidence"] >= llm_threshold:
        return {
            "genre": heuristic["genre"],
            "confidence": heuristic["confidence"],
            "method": "heuristics",
            "matched_keywords": heuristic["matched_keywords"],
            "observed_industries": heuristic.get("observed_industries", []),
        }
    llm = llm_genre(jobs)
    if llm and llm.get("genre"):
        return {
            "genre": llm.get("genre"),
            "confidence": llm.get("confidence"),
            "method": "llm",
            "reasoning": llm.get("reasoning"),
        }
    if heuristic:
        return {
            "genre": heuristic["genre"],
            "confidence": heuristic["confidence"],
            "method": "heuristics",
            "matched_keywords": heuristic["matched_keywords"],
            "observed_industries": heuristic.get("observed_industries", []),
        }
    return {"genre": None, "confidence": 0.0, "method": "none"}


# --------------------------------------------------------------------------- #
# Field match: is this company actually in the field the keyword describes?
# --------------------------------------------------------------------------- #

def field_terms(keyword):
    """Expand a free-text field keyword into match terms.

    The keyword's own words are always used. The keyword is expanded to a whole
    taxonomy industry's keywords only when it names that industry (e.g.
    "electronics" -> the electronics keywords). A specific technique such as
    "welding" is left as-is, so it does not match everything in its wider
    category.
    """
    keyword = (keyword or "").lower().strip()
    if not keyword:
        return set()
    terms = {t for t in re.findall(r"[a-z0-9+#.]+", keyword) if len(t) > 2}
    for genre, keywords in TAXONOMY.items():
        if keyword in genre.lower():
            terms.update(keywords)
    return terms


def match_field(jobs, keyword):
    """Decide whether a company's postings show it operates in the keyword's field.

    Returns a verdict dict. ``matches`` is True when at least one posting names
    the field in its title, or at least two mention it anywhere - i.e. there is
    real evidence the company hires for that field. ``confidence`` weights title
    matches 1.5x. With no postings, ``matches`` is None (unknown).
    """
    terms = field_terms(keyword)
    total = len(jobs)
    result = {
        "keyword": keyword,
        "field_terms": sorted(terms),
        "total_postings": total,
        "matched_postings": 0,
        "title_matches": 0,
        "match_ratio": 0.0,
        "matches": None,
        "confidence": 0.0,
        "evidence": [],
    }
    if not jobs:
        result["reason"] = "no postings found for this company in this city"
        return result
    if not terms:
        result["reason"] = "keyword is too short to match on"
        return result

    evidence = []
    for job in jobs:
        title = (job.get("title") or "").lower()
        body = " ".join(
            str(job.get(key) or "")
            for key in ("title", "description", "company_industry", "skills")
        ).lower()
        hits = sorted({t for t in terms if t in body})
        if not hits:
            continue
        title_hits = sorted({t for t in terms if t in title})
        result["matched_postings"] += 1
        if title_hits:
            result["title_matches"] += 1
        evidence.append({
            "title": job.get("title", ""),
            "company": job.get("company", ""),
            "location": job.get("location", ""),
            "url": job.get("url", ""),
            "matched_terms": hits,
            "in_title": bool(title_hits),
            "sources": job.get("sources", []),
        })
    result["evidence"] = evidence
    result["match_ratio"] = round(result["matched_postings"] / total, 2)
    result["matches"] = result["title_matches"] >= 1 or result["matched_postings"] >= 2
    result["confidence"] = round(
        min(0.99, (1.5 * result["title_matches"] + result["matched_postings"]) / (1.5 * total)),
        2,
    )
    return result
