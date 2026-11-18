"""Local retrieval primitives for the knowledge-system submission.

The first implementation step focuses only on turning Markdown pages into
structure-aware child chunks. Ranking and answer generation are intentionally
kept separate so each layer can be evaluated independently.
"""

from __future__ import annotations

import re
import math
import calendar
from collections import Counter
from dataclasses import dataclass
from datetime import date
from typing import Iterable
from urllib.parse import urlparse


MARKDOWN_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
BOLD_HEADING = re.compile(r"^\*\*([^*]+?)\*\*\s*(.*)$")
TABLE_SEPARATOR_CELL = re.compile(r"^:?-{3,}:?$")
WORD = re.compile(r"[a-z0-9$%.]+")
ISO_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
MONTH_DATE = re.compile(
    r"\b(january|february|march|april|may|june|july|august|"
    r"september|october|november|december)\s+"
    r"(?:(\d{1,2})(?:st|nd|rd|th)?(?:,\s*|\s+))?(\d{4})\b",
    re.IGNORECASE,
)
PREVIOUS_VALUE = re.compile(
    r"\b(?:previous|prior)\s+"
    r"(?:rate|apy|apr|value|offer|amount|fee|price)\b",
    re.IGNORECASE,
)
MODEL_YEAR_QUESTION = re.compile(
    r"(?:\b(20\d{2})\s+(?:model[-\s]?year\s+)?vehicle\b|"
    r"\b(?:model[-\s]?year|vehicle)\s+(20\d{2})\b)",
    re.IGNORECASE,
)
MODEL_YEAR_RANGE = (
    re.compile(
        r"\b(20\d{2})\s*[-\u2013\u2014\ufffd]\s*(\d{2}|20\d{2})\s+model year\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bmodel year\s+(20\d{2})\s*[-\u2013\u2014\ufffd]\s*(\d{2}|20\d{2})\b",
        re.IGNORECASE,
    ),
)
PERCENT_VALUE = re.compile(r"\b(\d+(?:\.\d+)?)\s*%")

MONTH_NUMBER = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}

WORD_NUMBER = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
}
TENS_NUMBER = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
}
TOKEN_ALIAS = {
    "cd": "certificate",
    "cds": "certificate",
    "certificates": "certificate",
    "months": "month",
    "rates": "rate",
}


@dataclass(frozen=True)
class Chunk:
    """A searchable child plus the context required to interpret it."""

    snapshot_id: str
    taken_on: str
    url: str
    title: str
    section_path: tuple[str, ...]
    heading: str
    parent_text: str
    text: str
    kind: str
    ordinal: int

    @property
    def search_text(self) -> str:
        """Text indexed for retrieval, enriched with its parent labels."""

        parts = [
            self.title,
            " > ".join(self.section_path),
            self.parent_text,
            self.heading,
            self.text,
        ]
        return "\n".join(part for part in parts if part).strip()


@dataclass(frozen=True)
class SearchHit:
    """One ranked retrieval result with transparent scoring evidence."""

    score: float
    matched_terms: tuple[str, ...]
    chunk: Chunk


@dataclass(frozen=True)
class QueryQualifiers:
    """Explicit details that a candidate must respect or should match."""

    products: tuple[str, ...]
    audiences: tuple[str, ...]
    term_months: tuple[int, ...]
    tier: str | None
    metrics: tuple[str, ...]
    balances: tuple[str, ...]
    wants_list: bool
    model_year: int | None


@dataclass(frozen=True)
class CandidateValidation:
    """Explain whether a hit fits the question and its ranking adjustment."""

    accepted: bool
    bonus: float
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class NormalizedHeaderColumn:
    """One table column mapped to a controlled semantic field."""

    position: int
    raw_name: str
    field_name: str | None
    qualifiers: tuple[str, ...]


@dataclass(frozen=True)
class NormalizedTableHeader:
    """A table schema that resolves only unambiguous semantic columns."""

    columns: tuple[NormalizedHeaderColumn, ...]

    def indexes_for(
        self,
        field_name: str,
        required_qualifiers: Iterable[str] = (),
    ) -> tuple[int, ...]:
        required = set(required_qualifiers)
        return tuple(
            column.position
            for column in self.columns
            if column.field_name == field_name
            and required.issubset(column.qualifiers)
        )

    def unique_index(
        self,
        field_name: str,
        required_qualifiers: Iterable[str] = (),
    ) -> int | None:
        """Return a column only when the field and qualifiers select exactly one."""

        indexes = self.indexes_for(field_name, required_qualifiers)
        return indexes[0] if len(indexes) == 1 else None


@dataclass(frozen=True)
class CertificateSourceAssessment:
    """Explain a certificate candidate's role without changing answer ranking."""

    accepted: bool
    audience: str
    role: str
    authority_score: float
    row_name: str | None
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class AutoSourceAssessment:
    """Explain whether an auto-rate row is an authoritative answer source."""

    accepted: bool
    audience: str
    role: str
    authority_score: float
    program_name: str | None
    reasons: tuple[str, ...]


HEADER_QUALIFIER_PATTERNS = (
    (re.compile(r"(?:for\s+)?(?:1st|first)\s+6\s+months", re.I), "intro_period"),
    (re.compile(r"after\s+promotional\s+period", re.I), "post_promo"),
    (re.compile(r"as\s+low\s+as", re.I), "as_low_as"),
)

HEADER_FIELD_ALIASES = {
    "certificate": "certificate_name",
    "loan program": "loan_program",
    "term": "term",
    "dividend rate": "dividend_rate",
    "apy": "apy",
    "annual percentage yield": "apy",
    "apr": "apr",
    "annual percentage rate": "apr",
    "min to earn": "minimum_to_earn",
    "minimum to earn": "minimum_to_earn",
    "min to open": "minimum_to_open",
    "minimum to open": "minimum_to_open",
    "minimum opening balance": "minimum_to_open",
}


def normalize_table_header_cell(cell: str, position: int) -> NormalizedHeaderColumn:
    """Normalize one header without discarding rate-period qualifiers."""

    raw_name = cell.strip()
    normalized = raw_name.lower()
    qualifiers = []
    for pattern, qualifier in HEADER_QUALIFIER_PATTERNS:
        if pattern.search(normalized):
            qualifiers.append(qualifier)
            normalized = pattern.sub(" ", normalized)

    normalized = re.sub(r"\((?:apy|apr|as\s+low\s+as)\)", " ", normalized)
    normalized = re.sub(r"(?:\*+|(?<=[a-z)])\s*\d+)\s*$", "", normalized)
    normalized = re.sub(r"[^a-z]+", " ", normalized).strip()

    return NormalizedHeaderColumn(
        position=position,
        raw_name=raw_name,
        field_name=HEADER_FIELD_ALIASES.get(normalized),
        qualifiers=tuple(qualifiers),
    )


def normalize_table_header(header_line: str) -> NormalizedTableHeader:
    """Convert a Markdown header row into a safely resolvable table schema."""

    cells = [cell.strip() for cell in header_line.strip().strip("|").split("|")]
    columns = tuple(
        normalize_table_header_cell(cell, position)
        for position, cell in enumerate(cells)
    )
    return NormalizedTableHeader(columns=columns)


def classify_certificate_source(
    hit: SearchHit,
    requested_audiences: Iterable[str] = (),
) -> CertificateSourceAssessment:
    """Classify certificate evidence using schema, context, and authority signals."""

    lines = [line.strip() for line in hit.chunk.text.splitlines() if line.strip()]
    path = urlparse(hit.chunk.url).path.rstrip("/").lower()
    context = "\n".join(
        (
            hit.chunk.url,
            hit.chunk.title,
            *hit.chunk.section_path,
            hit.chunk.heading,
            hit.chunk.parent_text,
        )
    ).lower()
    reasons = []

    if "/calculators/" in path or "calculator" in context:
        return CertificateSourceAssessment(
            accepted=False,
            audience="unknown",
            role="calculator",
            authority_score=0.0,
            row_name=None,
            reasons=("calculator context rejected",),
        )

    if len(lines) < 2:
        return CertificateSourceAssessment(
            accepted=False,
            audience="unknown",
            role="informational",
            authority_score=0.0,
            row_name=None,
            reasons=("no certificate table row",),
        )

    schema = normalize_table_header(lines[0])
    values = [cell.strip() for cell in lines[-1].strip("|").split("|")]
    if len(schema.columns) != len(values):
        return CertificateSourceAssessment(
            accepted=False,
            audience="unknown",
            role="invalid_schema",
            authority_score=0.0,
            row_name=None,
            reasons=("header and row width mismatch",),
        )

    name_indexes = tuple(
        index
        for field_name in ("certificate_name", "term")
        for index in schema.indexes_for(field_name)
    )
    minimum_indexes = tuple(
        index
        for field_name in ("minimum_to_earn", "minimum_to_open")
        for index in schema.indexes_for(field_name)
    )
    dividend_index = schema.unique_index("dividend_rate")
    apy_index = schema.unique_index("apy")
    if (
        len(name_indexes) != 1
        or len(minimum_indexes) != 1
        or dividend_index is None
        or apy_index is None
    ):
        return CertificateSourceAssessment(
            accepted=False,
            audience="unknown",
            role="invalid_schema",
            authority_score=0.0,
            row_name=None,
            reasons=("ambiguous or incomplete certificate schema",),
        )

    row_name = values[name_indexes[0]]
    row_context = f"{context}\n{row_name.lower()}"
    if "certificate" not in row_name.lower():
        return CertificateSourceAssessment(
            accepted=False,
            audience="ira" if any(
                token in row_context
                for token in (" ira", "roth", "traditional", "retirement")
            ) else "unknown",
            role="non_certificate_row",
            authority_score=0.0,
            row_name=row_name,
            reasons=("row is not an ordinary certificate product",),
        )

    if any(
        token in row_context
        for token in (" ira", "roth", "traditional", "retirement")
    ):
        audience = "ira"
    elif "business" in row_context:
        audience = "business"
    elif "/personal" in path or "personal" in row_context:
        audience = "personal"
    else:
        audience = "neutral"

    if "/learn" in path or "/blog" in path or " blog" in context:
        role = "informational"
        authority_score = 0.0
    elif (
        path == "/rates"
        or "deposit and loan rates" in hit.chunk.title.lower()
        or hit.chunk.title.lower().startswith("deposit rates")
    ):
        role = "dedicated_rates"
        authority_score = 40.0
    elif "certificate" in path or "certificate" in hit.chunk.title.lower():
        role = "certificate_product"
        authority_score = 35.0
    elif "savings" in path or "savings" in hit.chunk.title.lower():
        role = "savings_product"
        authority_score = 25.0
    else:
        role = "embedded_rates"
        authority_score = 10.0

    requested = set(requested_audiences)
    if not requested:
        compatible = audience in {"neutral", "personal"}
    elif requested.intersection({"ira", "roth", "traditional"}):
        compatible = audience == "ira"
    elif "business" in requested:
        compatible = audience == "business"
    else:
        compatible = audience in {"neutral", "personal"}

    if role == "informational":
        reasons.append("informational page rejected")
    if not compatible:
        reasons.append(f"{audience} audience mismatch")
    if compatible and role != "informational":
        reasons.extend(("certificate schema matched", f"{audience} audience matched"))
        if path == "/rates":
            authority_score += 2.0
            reasons.append("known rate path tie-breaker")

    accepted = compatible and role != "informational"
    return CertificateSourceAssessment(
        accepted=accepted,
        audience=audience,
        role=role,
        authority_score=authority_score if accepted else 0.0,
        row_name=row_name,
        reasons=tuple(reasons),
    )


def tokenize(text: str) -> list[str]:
    """Normalize aliases and written month terms for indexing and querying."""

    raw = WORD.findall(text.lower().replace("-", " "))
    tokens: list[str] = []
    position = 0
    while position < len(raw):
        token = raw[position]

        # Convert "ten month" and "twenty five month" to the same form as
        # "10-month" and "25-month" without changing unrelated number words.
        if token in TENS_NUMBER and position + 2 < len(raw):
            next_token = raw[position + 1]
            month_token = TOKEN_ALIAS.get(raw[position + 2], raw[position + 2])
            if next_token in WORD_NUMBER and month_token == "month":
                tokens.extend(
                    [str(TENS_NUMBER[token] + WORD_NUMBER[next_token]), "month"]
                )
                position += 3
                continue
        if token in WORD_NUMBER and position + 1 < len(raw):
            month_token = TOKEN_ALIAS.get(raw[position + 1], raw[position + 1])
            if month_token == "month":
                tokens.extend([str(WORD_NUMBER[token]), "month"])
                position += 2
                continue

        tokens.append(TOKEN_ALIAS.get(token, token))
        position += 1
    return tokens


def _month_terms(tokens: list[str]) -> tuple[int, ...]:
    terms = {
        int(token)
        for token, next_token in zip(tokens, tokens[1:])
        if token.isdigit() and next_token == "month"
    }
    return tuple(sorted(terms))


def _model_year_ranges(text: str) -> tuple[tuple[int, int], ...]:
    """Extract model-year ranges such as 2024-27 and Model Year 2026-23."""

    ranges = set()
    for pattern in MODEL_YEAR_RANGE:
        for match in pattern.finditer(text):
            first = int(match.group(1))
            second_text = match.group(2)
            second = (
                int(second_text)
                if len(second_text) == 4
                else (first // 100) * 100 + int(second_text)
            )
            ranges.add((min(first, second), max(first, second)))
    return tuple(sorted(ranges))


def parse_query_qualifiers(question: str) -> QueryQualifiers:
    """Extract only qualifiers that are safe and useful for candidate checks."""

    tokens = tokenize(question)
    token_set = set(tokens)
    products = tuple(
        product
        for product in (
            "certificate",
            "savings",
            "checking",
            "loan",
            "mortgage",
            "routing",
            "branch",
        )
        if product in token_set
    )
    audiences = tuple(
        audience
        for audience in ("roth", "traditional", "ira", "business", "personal")
        if audience in token_set
    )

    tier = None
    if "jumbo" in token_set:
        tier = "jumbo"
    elif "standard" in token_set or "nonjumbo" in token_set:
        tier = "standard"

    metrics = []
    if "apy" in token_set:
        metrics.append("apy")
    if "apr" in token_set:
        metrics.append("apr")
    if "dividend" in token_set and "rate" in token_set:
        metrics.append("dividend_rate")

    balances = {
        value.replace(",", "")
        for value in re.findall(r"\$\s*([0-9][0-9,]*(?:\.\d+)?)", question)
    }
    balances.update(
        value.replace(",", "")
        for value in re.findall(
            r"\b([0-9][0-9,]{2,}(?:\.\d+)?)\s+(?:minimum|balance)\b",
            question,
            re.IGNORECASE,
        )
    )

    wants_list = bool(
        re.search(
            r"\b(?:all|every|list|read back|provide current)\b",
            question,
            re.IGNORECASE,
        )
    )
    model_year_match = MODEL_YEAR_QUESTION.search(question)
    model_year = None
    if model_year_match:
        model_year = int(model_year_match.group(1) or model_year_match.group(2))
    return QueryQualifiers(
        products=products,
        audiences=audiences,
        term_months=_month_terms(tokens),
        tier=tier,
        metrics=tuple(metrics),
        balances=tuple(sorted(balances)),
        wants_list=wants_list,
        model_year=model_year,
    )


def validate_candidate(
    qualifiers: QueryQualifiers,
    hit: SearchHit,
) -> CandidateValidation:
    """Reject clear contradictions and score useful matches conservatively."""

    candidate_text = f"{hit.chunk.url}\n{hit.chunk.search_text}"
    candidate_tokens = tokenize(candidate_text)
    candidate_set = set(candidate_tokens)
    candidate_terms = set(_month_terms(candidate_tokens))
    reasons: list[str] = []
    bonus = 0.0

    if qualifiers.term_months:
        if not candidate_terms.intersection(qualifiers.term_months):
            return CandidateValidation(False, 0.0, ("term mismatch",))
        bonus += 6.0
        reasons.append("term matched")

    for audience in qualifiers.audiences:
        if audience == "ira":
            if not candidate_set.intersection({"ira", "roth", "traditional"}):
                return CandidateValidation(False, 0.0, ("IRA audience mismatch",))
        elif audience == "personal":
            if "business" in candidate_set and "personal" not in candidate_set:
                return CandidateValidation(False, 0.0, ("personal/business mismatch",))
        elif audience not in candidate_set:
            return CandidateValidation(False, 0.0, (f"{audience} audience mismatch",))
        bonus += 6.0
        reasons.append(f"{audience} audience matched")

    if qualifiers.tier == "jumbo":
        if "jumbo" not in candidate_set:
            return CandidateValidation(False, 0.0, ("jumbo tier mismatch",))
        bonus += 5.0
        reasons.append("jumbo tier matched")
    elif qualifiers.tier == "standard":
        if "jumbo" in candidate_set:
            return CandidateValidation(False, 0.0, ("standard tier mismatch",))
        bonus += 5.0
        reasons.append("standard tier matched")

    matched_products = candidate_set.intersection(qualifiers.products)
    if matched_products:
        reasons.append("product matched")

    for metric in qualifiers.metrics:
        metric_tokens = metric.split("_")
        if all(token in candidate_set for token in metric_tokens):
            bonus += 1.0
            reasons.append(f"{metric} matched")

    digits_only = re.sub(r"[^0-9.]", "", candidate_text)
    if qualifiers.balances and any(balance in digits_only for balance in qualifiers.balances):
        bonus += 3.0
        reasons.append("balance matched")

    if qualifiers.model_year is not None:
        ranges = _model_year_ranges(candidate_text)
        if ranges:
            if not any(
                first <= qualifiers.model_year <= last for first, last in ranges
            ):
                return CandidateValidation(False, 0.0, ("model-year mismatch",))
            bonus += 4.0
            reasons.append("model year matched")

    path = urlparse(hit.chunk.url).path.rstrip("/").lower()
    if path == "/rates":
        bonus += 1.5
        reasons.append("direct rate sheet")
    elif hit.chunk.kind == "table_row":
        bonus += 0.5
        reasons.append("structured table row")

    return CandidateValidation(True, bonus, tuple(reasons))


def _is_calculator_page(hit: SearchHit) -> bool:
    path = urlparse(hit.chunk.url).path.lower()
    return "/calculators/" in path or "calculator" in hit.chunk.title.lower()


def classify_auto_source(hit: SearchHit) -> AutoSourceAssessment:
    """Classify structured auto-rate evidence without requiring a fixed URL."""

    path = urlparse(hit.chunk.url).path.rstrip("/").lower()
    context = "\n".join(
        (
            hit.chunk.url,
            hit.chunk.title,
            *hit.chunk.section_path,
            hit.chunk.heading,
            hit.chunk.parent_text,
        )
    ).lower()
    if _is_calculator_page(hit):
        return AutoSourceAssessment(
            False,
            "unknown",
            "calculator",
            0.0,
            None,
            ("calculator context rejected as answer source",),
        )

    lines = [line.strip() for line in hit.chunk.text.splitlines() if line.strip()]
    if len(lines) < 2:
        return AutoSourceAssessment(
            False, "unknown", "informational", 0.0, None, ("no auto-rate row",)
        )
    schema = normalize_table_header(lines[0])
    values = [cell.strip() for cell in lines[-1].strip("|").split("|")]
    if len(schema.columns) != len(values):
        return AutoSourceAssessment(
            False,
            "unknown",
            "invalid_schema",
            0.0,
            None,
            ("header and row width mismatch",),
        )

    program_index = schema.unique_index("loan_program")
    term_index = schema.unique_index("term")
    apr_index = schema.unique_index("apr")
    if program_index is None or term_index is None or apr_index is None:
        return AutoSourceAssessment(
            False,
            "unknown",
            "invalid_schema",
            0.0,
            None,
            ("ambiguous or incomplete auto-rate schema",),
        )

    program_name = values[program_index]
    if not _model_year_ranges(program_name):
        return AutoSourceAssessment(
            False,
            "unknown",
            "non_auto_row",
            0.0,
            program_name,
            ("row has no model-year range",),
        )

    audience = "business" if "business" in context else "personal"
    reasons = ["auto-rate schema matched"]
    if audience == "business":
        return AutoSourceAssessment(
            False,
            audience,
            "business_auto",
            0.0,
            program_name,
            tuple(reasons + ["business audience mismatch"]),
        )

    title = hit.chunk.title.lower()
    if (
        any(token in path for token in ("auto-loans", "auto-motorcycle", "vehicle-financing"))
        or ("auto" in title and "loan" in title)
        or ("motorcycle" in title and "loan" in title)
        or "vehicle financing" in title
    ):
        role = "auto_product"
        authority_score = 40.0
    elif path == "/rates" or "loan rates" in title:
        role = "rate_sheet"
        authority_score = 32.0
    elif "switch-and-earn" in path or "refinance" in title:
        role = "promotion"
        authority_score = 20.0
    else:
        role = "embedded_rates"
        authority_score = 10.0

    if path.endswith("/auto-motorcycle-loans"):
        authority_score += 2.0
        reasons.append("known auto-product path tie-breaker")
    reasons.extend(("personal audience matched", f"{role} authority"))
    return AutoSourceAssessment(
        True,
        audience,
        role,
        authority_score,
        program_name,
        tuple(reasons),
    )


def _is_official_auto_loan_page(hit: SearchHit) -> bool:
    """Recognize structured official pages that publish auto-loan rates."""

    assessment = classify_auto_source(hit)
    return assessment.accepted and assessment.role in {
        "auto_product",
        "rate_sheet",
        "promotion",
    }


def _percentage_values(hit: SearchHit) -> frozenset[float]:
    return frozenset(
        round(float(value), 4) for value in PERCENT_VALUE.findall(hit.chunk.text)
    )


def _calculator_conflict_adjustments(
    question: str,
    qualifiers: QueryQualifiers,
    accepted: list[tuple[float, SearchHit]],
    *,
    current_question: bool,
) -> dict[SearchHit, float]:
    """Prefer matching product rates only when calculator evidence conflicts.

    This is intentionally narrow: it applies to current auto-loan rate questions,
    compares candidates from the already selected snapshot, and makes no global
    assumption that calculator pages are stale.
    """

    question_tokens = set(tokenize(question))
    asks_for_rate = bool(qualifiers.metrics) or "rate" in question_tokens
    asks_about_auto_loan = (
        question_tokens.intersection({"auto", "vehicle"})
        and "loan" in question_tokens
    )
    if not current_question or not asks_for_rate or not asks_about_auto_loan:
        return {}

    official_hits = [
        hit
        for _, hit in accepted
        if _is_official_auto_loan_page(hit) and _percentage_values(hit)
    ]
    calculator_hits = [
        hit for _, hit in accepted if _is_calculator_page(hit) and _percentage_values(hit)
    ]
    if not official_hits or not calculator_hits:
        return {}

    adjustments: dict[SearchHit, float] = {}
    supported_official_hits = set()
    for calculator_hit in calculator_hits:
        calculator_values = _percentage_values(calculator_hit)
        conflicting_official = [
            hit
            for hit in official_hits
            if _percentage_values(hit) != calculator_values
        ]
        if not conflicting_official:
            continue
        adjustments[calculator_hit] = -15.0
        supported_official_hits.update(conflicting_official)

    for hit in supported_official_hits:
        assessment = classify_auto_source(hit)
        canonical_bonus = 4.0 if assessment.role == "auto_product" else 3.0
        adjustments[hit] = adjustments.get(hit, 0.0) + canonical_bonus
    return adjustments


def rerank_with_qualifiers(
    question: str,
    hits: Iterable[SearchHit],
    *,
    current_question: bool = False,
) -> list[SearchHit]:
    """Apply explicit constraints, then combine BM25 with small match bonuses."""

    qualifiers = parse_query_qualifiers(question)
    accepted: list[tuple[float, SearchHit]] = []
    for hit in hits:
        validation = validate_candidate(qualifiers, hit)
        if validation.accepted:
            accepted.append((hit.score + validation.bonus, hit))
    adjustments = _calculator_conflict_adjustments(
        question,
        qualifiers,
        accepted,
        current_question=current_question,
    )
    accepted = [
        (score + adjustments.get(hit, 0.0), hit) for score, hit in accepted
    ]
    accepted.sort(
        key=lambda item: (
            -item[0],
            item[1].chunk.url,
            item[1].chunk.ordinal,
            item[1].chunk.text,
        )
    )
    return [hit for _, hit in accepted]


class BM25Index:
    """Small, deterministic, standard-library BM25 search index."""

    def __init__(
        self,
        chunks: Iterable[Chunk],
        *,
        k1: float = 1.5,
        b: float = 0.75,
    ) -> None:
        self.chunks = tuple(chunks)
        self.k1 = k1
        self.b = b
        self._tokens = tuple(tokenize(chunk.search_text) for chunk in self.chunks)
        self._term_frequencies = tuple(Counter(tokens) for tokens in self._tokens)

        self._document_frequency: Counter[str] = Counter()
        for tokens in self._tokens:
            self._document_frequency.update(set(tokens))

        token_count = sum(len(tokens) for tokens in self._tokens)
        self._average_length = token_count / len(self._tokens) if self._tokens else 0.0

    def __len__(self) -> int:
        return len(self.chunks)

    def search(self, query: str, *, limit: int = 10) -> list[SearchHit]:
        """Return the highest-scoring chunks for a question.

        Ties are broken by URL and chunk ordinal so repeated calls always produce
        the same ordering.
        """

        if limit <= 0 or not self.chunks:
            return []

        query_terms = tuple(sorted(set(tokenize(query))))
        if not query_terms:
            return []

        hits = []
        document_count = len(self.chunks)

        for chunk, tokens, term_frequency in zip(
            self.chunks,
            self._tokens,
            self._term_frequencies,
        ):
            matched_terms = tuple(term for term in query_terms if term_frequency[term])
            if not matched_terms:
                continue

            length_ratio = len(tokens) / self._average_length if self._average_length else 0.0
            score = 0.0
            for term in matched_terms:
                frequency = term_frequency[term]
                document_frequency = self._document_frequency[term]
                inverse_document_frequency = math.log(
                    1
                    + (document_count - document_frequency + 0.5)
                    / (document_frequency + 0.5)
                )
                numerator = frequency * (self.k1 + 1)
                denominator = frequency + self.k1 * (
                    1 - self.b + self.b * length_ratio
                )
                score += inverse_document_frequency * numerator / denominator

            hits.append(
                SearchHit(
                    score=score,
                    matched_terms=matched_terms,
                    chunk=chunk,
                )
            )

        hits.sort(
            key=lambda hit: (
                -hit.score,
                hit.chunk.url,
                hit.chunk.ordinal,
                hit.chunk.text,
            )
        )
        return hits[:limit]


@dataclass(frozen=True)
class SnapshotIndex:
    """The searchable knowledge available at one snapshot date."""

    snapshot_id: str
    taken_on: date
    index: BM25Index


def date_mentioned_in(question: str) -> date | None:
    """Extract a supported calendar date from a member question.

    A month-and-year request is interpreted as the end of that month so every
    snapshot available during the requested month is eligible.
    """

    iso_match = ISO_DATE.search(question)
    if iso_match:
        try:
            return date.fromisoformat(iso_match.group(1))
        except ValueError:
            return None

    month_match = MONTH_DATE.search(question)
    if not month_match:
        return None

    month = MONTH_NUMBER[month_match.group(1).lower()]
    year = int(month_match.group(3))
    if month_match.group(2):
        day = int(month_match.group(2))
    else:
        day = calendar.monthrange(year, month)[1]

    try:
        return date(year, month, day)
    except ValueError:
        return None


def asks_for_previous_value(question: str) -> bool:
    """Detect an explicit request for the immediately preceding value."""

    return PREVIOUS_VALUE.search(question) is not None


class SnapshotRouter:
    """Route current and historical questions to one independent BM25 index."""

    def __init__(self) -> None:
        self._snapshots: list[SnapshotIndex] = []

    @property
    def snapshots(self) -> tuple[SnapshotIndex, ...]:
        return tuple(self._snapshots)

    def add_snapshot(
        self,
        *,
        snapshot_id: str,
        taken_on: str,
        chunks: Iterable[Chunk],
    ) -> None:
        """Build and store the independent index for one ingested snapshot."""

        parsed_date = date.fromisoformat(taken_on)
        replacement = SnapshotIndex(
            snapshot_id=snapshot_id,
            taken_on=parsed_date,
            index=BM25Index(chunks),
        )
        self._snapshots = [
            snapshot
            for snapshot in self._snapshots
            if snapshot.snapshot_id != snapshot_id
        ]
        self._snapshots.append(replacement)
        self._snapshots.sort(key=lambda snapshot: (snapshot.taken_on, snapshot.snapshot_id))

    def select(
        self,
        question: str,
        *,
        as_of: str | None = None,
    ) -> SnapshotIndex | None:
        """Select the latest eligible snapshot using the approved routing rules."""

        if not self._snapshots:
            return None

        if as_of is not None:
            try:
                target_date = date.fromisoformat(as_of)
            except ValueError:
                return None
        else:
            target_date = date_mentioned_in(question)

        if target_date is None:
            eligible = list(self._snapshots)
        else:
            eligible = [
                snapshot
                for snapshot in self._snapshots
                if snapshot.taken_on <= target_date
            ]

        if not eligible:
            return None

        selected_position = len(eligible) - 1
        if asks_for_previous_value(question):
            selected_position -= 1
        if selected_position < 0:
            return None
        return eligible[selected_position]

    def search(
        self,
        question: str,
        *,
        as_of: str | None = None,
        limit: int = 10,
    ) -> tuple[SnapshotIndex | None, list[SearchHit]]:
        """Select the correct snapshot and search only within that snapshot."""

        selected = self.select(question, as_of=as_of)
        if selected is None:
            return None, []
        # Keep a wider lexical pool so an explicitly qualified match is not lost
        # merely because a generic row had a slightly higher BM25 score.
        lexical_hits = selected.index.search(question, limit=max(50, limit * 5))
        current_question = (
            as_of is None
            and date_mentioned_in(question) is None
            and not asks_for_previous_value(question)
        )
        qualified_hits = rerank_with_qualifiers(
            question,
            lexical_hits,
            current_question=current_question,
        )
        return selected, qualified_hits[:limit]


def _blocks(markdown: str) -> list[str]:
    """Split Markdown at blank lines while preserving lines inside each block."""

    normalized = markdown.replace("\r\n", "\n").replace("\r", "\n")
    return [block.strip() for block in re.split(r"\n\s*\n", normalized) if block.strip()]


def _is_table(block: str) -> bool:
    lines = [line.strip() for line in block.splitlines() if line.strip()]
    return len(lines) >= 2 and all(line.startswith("|") for line in lines)


def _table_rows(block: str) -> tuple[str, list[str]]:
    """Return a Markdown table header and its data rows."""

    lines = [line.strip() for line in block.splitlines() if line.strip()]
    if not lines:
        return "", []

    header = lines[0]
    rows = []
    for line in lines[1:]:
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if cells and all(TABLE_SEPARATOR_CELL.fullmatch(cell) for cell in cells):
            continue
        rows.append(line)
    return header, rows


def chunk_markdown(
    *,
    markdown: str,
    snapshot_id: str,
    taken_on: str,
    url: str,
    title: str,
) -> list[Chunk]:
    """Create parent-enriched child chunks from one Markdown page.

    Markdown headings form the normal section hierarchy. Standalone or leading
    bold labels are treated as pseudo-headings because the supplied pages often
    encode labels such as ``**Lobby**`` and ``**Drive-up**`` this way.
    """

    chunks: list[Chunk] = []
    section_stack: list[tuple[int, str]] = []
    recent_context = ""
    active_label = ""
    active_parent = ""
    active_blocks: list[str] = []
    ordinal = 0

    def section_path() -> tuple[str, ...]:
        return tuple(text for _, text in section_stack)

    def add_chunk(
        *,
        text: str,
        kind: str,
        heading: str = "",
        parent_text: str = "",
    ) -> None:
        nonlocal ordinal
        cleaned = text.strip()
        if not cleaned:
            return
        chunks.append(
            Chunk(
                snapshot_id=snapshot_id,
                taken_on=taken_on,
                url=url,
                title=title,
                section_path=section_path(),
                heading=heading,
                parent_text=parent_text,
                text=cleaned,
                kind=kind,
                ordinal=ordinal,
            )
        )
        ordinal += 1

    def flush_active() -> None:
        nonlocal active_blocks
        if active_label and active_blocks:
            add_chunk(
                text="\n".join(active_blocks),
                kind="section",
                heading=active_label,
                parent_text=active_parent,
            )
        active_blocks = []

    for block in _blocks(markdown):
        lines = block.splitlines()
        first_line = lines[0].strip()
        heading_match = MARKDOWN_HEADING.match(first_line)
        bold_match = BOLD_HEADING.match(first_line)

        if heading_match:
            flush_active()
            active_label = ""
            active_parent = ""
            level = len(heading_match.group(1))
            label = heading_match.group(2).strip()
            section_stack = [item for item in section_stack if item[0] < level]
            section_stack.append((level, label))
            recent_context = ""
            remainder = "\n".join(lines[1:]).strip()
            if remainder:
                add_chunk(text=remainder, kind="text", heading=label)
                if len(remainder) <= 240:
                    recent_context = remainder
            continue

        if bold_match:
            flush_active()
            active_label = bold_match.group(1).strip()
            active_parent = recent_context
            inline_text = bold_match.group(2).strip()
            active_blocks = [inline_text] if inline_text else []
            continue

        if _is_table(block):
            flush_active()
            header, rows = _table_rows(block)
            row_heading = active_label or (section_stack[-1][1] if section_stack else "")
            row_parent = active_parent or recent_context
            for row in rows:
                add_chunk(
                    text=f"{header}\n{row}",
                    kind="table_row",
                    heading=row_heading,
                    parent_text=row_parent,
                )
            continue

        if active_label:
            active_blocks.append(block)
            continue

        add_chunk(
            text=block,
            kind="text",
            heading=section_stack[-1][1] if section_stack else "",
            parent_text="",
        )
        if len(block) <= 240:
            recent_context = block

    flush_active()
    return chunks
