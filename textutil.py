"""Shared text helpers: whole-word term matching and company-name matching.

Plain ``keyword in text`` checks match inside other words ("ev" in
"development", "die" in "audience"). ``term_pattern`` builds a case-insensitive
whole-word regex instead, so keyword scoring only fires on real occurrences.

``company_matches`` compares company names on whole tokens after dropping legal
suffixes (Pvt, Ltd, Inc ...), so "ABB" no longer matches "Abbott" while
"Portescap" still matches "Portescap India Pvt. Ltd.".
"""

import re
from functools import lru_cache

# Words that identify a legal form rather than the company itself.
LEGAL_SUFFIXES = frozenset({
    "pvt", "private", "ltd", "limited", "llp", "llc", "inc", "incorporated",
    "corp", "corporation", "co", "company", "plc", "gmbh", "ag", "sa", "bv",
    "pte", "opc",
})


def tokens(text):
    """Lower-case alphanumeric tokens of ``text``."""
    return re.findall(r"[a-z0-9]+", (text or "").lower())


@lru_cache(maxsize=4096)
def term_pattern(term):
    """Compile a whole-word, case-insensitive pattern for ``term``.

    The term may be followed by a plural ``s``/``es``. Spaces inside a phrase
    match any run of whitespace, so "supply chain" also matches "supply  chain".
    """
    escaped = re.escape(term.strip().lower()).replace(r"\ ", r"\s+")
    return re.compile(rf"(?<![a-z0-9]){escaped}(?:s|es)?(?![a-z0-9])", re.I)


def find_terms(text, terms):
    """Return the subset of ``terms`` that occur in ``text`` as whole words."""
    haystack = (text or "").lower()
    return {term for term in terms if term_pattern(term).search(haystack)}


def company_tokens(name):
    """Tokens that identify a company, with legal suffixes removed."""
    found = tokens(name)
    core = [token for token in found if token not in LEGAL_SUFFIXES]
    return core or found


def company_matches(query, candidate):
    """Whether ``candidate`` is the company named by ``query``.

    True when the two names are equal after dropping legal suffixes (also when
    they differ only in spacing, e.g. "Hindujatech" vs "Hinduja Tech"), or when
    every token of the query appears as a whole token of the candidate
    ("Portescap" matches "Portescap India Pvt Ltd"). A short query can still be
    ambiguous: "Tata" matches every Tata group company.
    """
    wanted = company_tokens(query)
    other = company_tokens(candidate)
    if not wanted or not other:
        return False
    if wanted == other or "".join(wanted) == "".join(other):
        return True
    return set(wanted) <= set(other)
