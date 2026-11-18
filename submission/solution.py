"""Deterministic, snapshot-aware knowledge-system submission."""

import re
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(
    0,
    str(
        next(
            d / "harness"
            for d in Path(__file__).resolve().parents
            if (d / "harness" / "contract.py").exists()
        )
    ),
)
from contract import Answer, Conflict, Snapshot, Source  # noqa: E402

try:
    from .retrieval import (
        SearchHit,
        SnapshotRouter,
        asks_for_previous_value,
        chunk_markdown,
        classify_auto_source,
        classify_certificate_source,
        date_mentioned_in,
        normalize_table_header,
        parse_query_qualifiers,
    )
except ImportError:  # The harness loads this file directly from the submission folder.
    from retrieval import (
        SearchHit,
        SnapshotRouter,
        asks_for_previous_value,
        chunk_markdown,
        classify_auto_source,
        classify_certificate_source,
        date_mentioned_in,
        normalize_table_header,
        parse_query_qualifiers,
    )


WORD = re.compile(r"[a-z0-9$%.]+")
RATE_METRIC_LABELS = {
    "apr": "APR",
    "apy": "APY",
    "dividend_rate": "dividend rate",
}


class SnapshotKnowledge:
    """Local evidence router with narrow, validated answer projections."""

    def __init__(self):
        self.router = SnapshotRouter()
        self._snapshot_pages = {}
        self._auto_conflicts_cache = None

    def ingest(self, snapshot: Snapshot) -> None:
        chunks = []
        self._snapshot_pages[snapshot.snapshot_id] = tuple(snapshot.pages)
        self._auto_conflicts_cache = None
        for page in snapshot.pages:
            chunks.extend(
                chunk_markdown(
                    markdown=page.markdown,
                    snapshot_id=snapshot.snapshot_id,
                    taken_on=snapshot.taken_on,
                    url=page.url,
                    title=page.title,
                )
            )
        self.router.add_snapshot(
            snapshot_id=snapshot.snapshot_id,
            taken_on=snapshot.taken_on,
            chunks=chunks,
        )

    def retrieve_candidates(self, question, as_of=None, limit=10):
        """Return the routed snapshot and transparent ranked search hits."""

        return self.router.search(question, as_of=as_of, limit=limit)

    @staticmethod
    def _requested_rate_metrics(qualifiers, default_metric, supported_metrics):
        """Select only requested fields and reject metrics this product lacks."""

        requested = qualifiers.metrics or (default_metric,)
        if any(metric not in supported_metrics for metric in requested):
            return None
        return requested

    @staticmethod
    def _project_rate_values(record, requested_metrics):
        """Render only the requested metrics from a fully parsed evidence record."""

        projected = []
        for metric in requested_metrics:
            value = record.get(metric)
            if value is None:
                return None
            projected.append(f"{value} {RATE_METRIC_LABELS[metric]}")
        return ", ".join(projected)

    @staticmethod
    def _is_current_model_year_auto_rate_question(question, as_of=None):
        """Recognize only the current auto-rate case covered by our evidence."""

        words = set(WORD.findall(question.lower()))
        qualifiers = parse_query_qualifiers(question)
        asks_for_rate = bool({"apr", "apy", "rate"} & words) or (
            "interest" in words and "rate" in words
        )
        return (
            as_of is None
            and date_mentioned_in(question) is None
            and not asks_for_previous_value(question)
            and bool({"auto", "vehicle"} & words)
            and "loan" in words
            and asks_for_rate
            and bool(qualifiers.term_months)
            and qualifiers.model_year is not None
        )

    def _answer_current_model_year_auto_rate(self, question, as_of=None):
        if not self._is_current_model_year_auto_rate_question(question, as_of):
            return None

        qualifiers = parse_query_qualifiers(question)
        requested_metrics = self._requested_rate_metrics(
            qualifiers,
            default_metric="apr",
            supported_metrics={"apr"},
        )
        if requested_metrics is None:
            return Answer(
                text="I couldn't confirm the requested rate metric for this loan.",
                confident=False,
            )

        selected, hits = self.retrieve_candidates(question, as_of=as_of, limit=15)
        if selected is None or not hits:
            return None

        accepted_hits = []
        pages = {
            page.url: page
            for page in self._snapshot_pages.get(selected.snapshot_id, ())
        }
        role_rank = {
            "auto_product": 3,
            "rate_sheet": 2,
            "promotion": 1,
            "embedded_rates": 0,
        }
        for hit in hits:
            assessment = classify_auto_source(hit)
            if not assessment.accepted:
                continue
            parsed = self._parse_auto_rate_row(hit.chunk.text)
            if parsed is None:
                continue
            page = pages.get(hit.chunk.url)
            effective_date = (
                self._page_effective_date(page.markdown) if page else None
            )
            accepted_hits.append(
                (
                    role_rank.get(assessment.role, -1),
                    effective_date or date.min,
                    assessment.authority_score,
                    hit.score,
                    hit,
                    parsed,
                )
            )
        if not accepted_hits:
            return None

        _, _, _, _, hit, parsed = max(
            accepted_hits,
            key=lambda item: (item[0], item[1], item[2], item[3], item[4].chunk.url),
        )
        rate_text = self._project_rate_values(
            {"apr": parsed["apr"]}, requested_metrics
        )
        if rate_text is None:
            return None

        program = re.sub(
            r"\s+Model Year\b", "", parsed["program"], flags=re.IGNORECASE
        )
        term = parsed["term"].lower()
        answer_text = f"{rate_text} for {term} ({program} model-year tier)."
        matching_conflict = next(
            (
                conflict
                for conflict in self._latest_auto_rate_conflicts()
                if parsed["apr"] == conflict.resolution
                and f'{qualifiers.term_months[0]}-month' in conflict.topic
            ),
            None,
        )
        return Answer(
            text=answer_text,
            sources=[
                Source(
                    url=hit.chunk.url,
                    snapshot_id=selected.snapshot_id,
                    quote=hit.chunk.text[:300],
                )
            ],
            conflict_note=(
                "A calculator example disagrees with the newer official auto-product "
                "rate; the official rate was selected."
                if matching_conflict
                else None
            ),
        )

    @staticmethod
    def _parse_auto_rate_row(text):
        """Map one auto-rate row through its normalized semantic schema."""

        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) < 2:
            return None
        values = [cell.strip() for cell in lines[-1].strip("|").split("|")]
        schema = normalize_table_header(lines[0])
        if len(schema.columns) != len(values):
            return None

        program_index = schema.unique_index("loan_program")
        term_index = schema.unique_index("term")
        apr_index = schema.unique_index("apr")
        if program_index is None or term_index is None or apr_index is None:
            return None

        apr_match = re.search(r"\b\d+(?:\.\d+)?%", values[apr_index])
        if apr_match is None:
            return None
        return {
            "program": values[program_index],
            "term": values[term_index],
            "apr": apr_match.group(0),
        }

    @staticmethod
    def _parse_flexible_auto_rate_row(text):
        """Read a model-year/term/APR row even if a calculator mislabels headers."""

        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) < 2:
            return None
        values = [cell.strip() for cell in lines[-1].strip("|").split("|")]
        if len(values) < 3:
            return None

        range_match = re.search(
            r"\b(20\d{2})\s*[-\u2013\u2014]\s*(\d{2}|20\d{2})\s+Model Year\b",
            values[0],
            re.IGNORECASE,
        )
        term_match = next(
            (
                match
                for value in values
                if (match := re.search(r"\b(\d+)\s*Months?\b", value, re.IGNORECASE))
            ),
            None,
        )
        apr_match = next(
            (
                match
                for value in values
                if (
                    match := re.search(
                        r"\b\d+(?:\.\d+)?%\s*APR(?:\d+)?\b",
                        value,
                        re.IGNORECASE,
                    )
                )
            ),
            None,
        )
        if range_match is None or term_match is None or apr_match is None:
            return None

        first = int(range_match.group(1))
        second_text = range_match.group(2)
        second = (
            int(second_text)
            if len(second_text) == 4
            else (first // 100) * 100 + int(second_text)
        )
        return {
            "model_years": (min(first, second), max(first, second)),
            "term_months": int(term_match.group(1)),
            "apr": re.search(r"\d+(?:\.\d+)?%", apr_match.group(0)).group(0),
        }

    @staticmethod
    def _page_effective_date(markdown):
        """Return the newest explicit rate-effective date printed on a page."""

        dates = []
        for month, day, year in re.findall(
            r"Rates are effective as of\s+(\d{1,2})/(\d{1,2})/(\d{4})",
            markdown,
            re.IGNORECASE,
        ):
            try:
                dates.append(date(int(year), int(month), int(day)))
            except ValueError:
                continue
        return max(dates) if dates else None

    def _latest_auto_rate_conflicts(self):
        """Find calculator/product APR disagreements in the latest snapshot."""

        if self._auto_conflicts_cache is not None:
            return list(self._auto_conflicts_cache)
        if not self.router.snapshots:
            return []
        latest = self.router.snapshots[-1]
        pages = {
            page.url: page
            for page in self._snapshot_pages.get(latest.snapshot_id, ())
        }
        official = []
        calculators = []

        for chunk in latest.index.chunks:
            parsed = self._parse_flexible_auto_rate_row(chunk.text)
            if parsed is None:
                continue
            hit = SearchHit(score=0.0, matched_terms=(), chunk=chunk)
            path = urlparse(chunk.url).path.lower()
            is_calculator = (
                "/calculators/" in path or "calculator" in chunk.title.lower()
            )
            page = pages.get(chunk.url)
            fact = {
                **parsed,
                "url": chunk.url,
                "quote": chunk.text[:500],
                "effective": self._page_effective_date(page.markdown) if page else None,
            }
            if is_calculator:
                calculators.append(fact)
                continue

            assessment = classify_auto_source(hit)
            if assessment.accepted and assessment.role == "auto_product":
                fact["authority"] = assessment.authority_score
                official.append(fact)

        conflicts = []
        seen = set()
        for calculator in calculators:
            calc_start, calc_end = calculator["model_years"]
            matching = []
            for product in official:
                prod_start, prod_end = product["model_years"]
                overlap_start = max(calc_start, prod_start)
                overlap_end = min(calc_end, prod_end)
                if (
                    calculator["term_months"] == product["term_months"]
                    and overlap_start <= overlap_end
                    and calculator["apr"] != product["apr"]
                    and calculator["effective"] is not None
                    and product["effective"] is not None
                    and product["effective"] > calculator["effective"]
                ):
                    matching.append((product, overlap_start, overlap_end))
            if not matching:
                continue

            product, overlap_start, overlap_end = max(
                matching,
                key=lambda item: (
                    item[0].get("effective") or date.min,
                    item[0]["authority"],
                    item[0]["url"],
                ),
            )
            key = (
                calculator["term_months"],
                overlap_start,
                overlap_end,
                calculator["apr"],
                product["apr"],
            )
            if key in seen:
                continue
            seen.add(key)

            calc_date = (
                calculator["effective"].isoformat()
                if calculator["effective"]
                else "unknown"
            )
            product_date = (
                product["effective"].isoformat()
                if product["effective"]
                else "unknown"
            )
            conflicts.append(
                Conflict(
                    topic=(
                        f'{calculator["term_months"]}-month auto loan APR for '
                        f'{overlap_start}-{str(overlap_end)[-2:]} model years'
                    ),
                    values=[calculator["apr"], product["apr"]],
                    sources=[
                        Source(
                            url=calculator["url"],
                            snapshot_id=latest.snapshot_id,
                            quote=calculator["quote"],
                        ),
                        Source(
                            url=product["url"],
                            snapshot_id=latest.snapshot_id,
                            quote=product["quote"],
                        ),
                    ],
                    resolution=product["apr"],
                    reason=(
                        "The matching official auto-product table is newer "
                        f"({product_date}) than the calculator table ({calc_date})."
                    ),
                )
            )
        conflicts.sort(key=lambda conflict: conflict.topic)
        self._auto_conflicts_cache = tuple(conflicts)
        return list(self._auto_conflicts_cache)

    @staticmethod
    def _parse_certificate_row(text):
        """Map one certificate row through its normalized semantic schema."""

        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) < 2:
            return None

        headers = [cell.strip() for cell in lines[0].strip("|").split("|")]
        values = [cell.strip() for cell in lines[-1].strip("|").split("|")]
        if len(headers) != len(values):
            return None

        schema = normalize_table_header(lines[0])
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
            return None

        name = values[name_indexes[0]]
        dividend_rate = values[dividend_index]
        apy = values[apy_index]
        minimum = values[minimum_indexes[0]]

        # The rate sheet appends footnote numbers directly to some percentages.
        dividend_rate = re.sub(r"(%)\d+$", r"\1", dividend_rate)
        apy = re.sub(r"(%)\d+$", r"\1", apy)
        return {
            "name": name,
            "dividend_rate": dividend_rate,
            "apy": apy,
            "minimum": minimum,
        }

    def _answer_certificate_rate(self, question, as_of=None):
        """Answer only term-specific standard/jumbo certificate rate questions."""

        qualifiers = parse_query_qualifiers(question)
        if (
            "certificate" not in qualifiers.products
            or not qualifiers.term_months
            or qualifiers.audiences
        ):
            return None

        requested_metrics = self._requested_rate_metrics(
            qualifiers,
            default_metric="apy",
            supported_metrics={"apy", "dividend_rate"},
        )
        if requested_metrics is None:
            return Answer(
                text="I couldn't confirm the requested rate metric for this certificate.",
                confident=False,
            )

        selected, hits = self.retrieve_candidates(question, as_of=as_of, limit=25)
        if selected is None or not hits:
            return None

        candidates_by_url = {}
        for hit in hits:
            assessment = classify_certificate_source(
                hit,
                requested_audiences=qualifiers.audiences,
            )
            if not assessment.accepted:
                continue
            parsed = self._parse_certificate_row(hit.chunk.text)
            if parsed is None:
                continue

            name_lower = parsed["name"].lower()
            if "certificate" not in name_lower:
                continue
            is_jumbo = "jumbo" in name_lower
            if qualifiers.tier == "jumbo" and not is_jumbo:
                continue
            if qualifiers.tier == "standard" and is_jumbo:
                continue

            source_rows = candidates_by_url.setdefault(
                hit.chunk.url,
                {
                    "authority": assessment.authority_score,
                    "best_search_score": hit.score,
                    "rows": {},
                },
            )
            source_rows["authority"] = max(
                source_rows["authority"], assessment.authority_score
            )
            source_rows["best_search_score"] = max(
                source_rows["best_search_score"], hit.score
            )
            source_rows["rows"].setdefault(
                parsed["name"],
                (parsed, hit.chunk.text),
            )

        if not candidates_by_url:
            return None

        def source_rank(item):
            _, evidence = item
            parsed_rows = [row for row, _ in evidence["rows"].values()]
            if qualifiers.tier is not None:
                complete = bool(parsed_rows)
            else:
                tiers = {
                    "jumbo" if "jumbo" in row["name"].lower() else "standard"
                    for row in parsed_rows
                }
                complete = {"standard", "jumbo"}.issubset(tiers)
            return (
                int(complete),
                evidence["authority"],
                evidence["best_search_score"],
            )

        source_url, selected_evidence = max(
            candidates_by_url.items(),
            key=source_rank,
        )
        selected_rows = list(selected_evidence["rows"].values())
        rows = [row for row, _ in selected_rows]
        supporting_quotes = [quote for _, quote in selected_rows]

        rows.sort(key=lambda row: ("jumbo" in row["name"].lower(), row["name"]))
        if qualifiers.tier is not None:
            rows = rows[:1]
            supporting_quotes = supporting_quotes[:1]
        else:
            rows = rows[:2]
            supporting_quotes = supporting_quotes[:2]

        statements = []
        for row in rows:
            rate_text = self._project_rate_values(row, requested_metrics)
            if rate_text is None:
                return None
            statements.append(f'{row["name"]}: {rate_text}.')
        historical = as_of is not None or date_mentioned_in(question) is not None
        prefix = f"As of the {selected.taken_on} snapshot, " if historical else ""
        return Answer(
            text=prefix + " ".join(statements),
            sources=[
                Source(
                    url=source_url,
                    snapshot_id=selected.snapshot_id,
                    quote="\n\n".join(supporting_quotes)[:1000],
                )
            ],
        )

    def answer(self, question, as_of=None):
        auto_rate_answer = self._answer_current_model_year_auto_rate(question, as_of)
        if auto_rate_answer is not None:
            return auto_rate_answer

        certificate_answer = self._answer_certificate_rate(question, as_of)
        if certificate_answer is not None:
            return certificate_answer

        # Retrieved text is evidence, not automatically a safe answer. Unsupported
        # questions refuse instead of returning stale, unrelated, or extra content.
        return Answer(
            text="I couldn't verify that answer from matching evidence.",
            confident=False,
        )

    def conflicts(self):
        return self._latest_auto_rate_conflicts()


def build():
    return SnapshotKnowledge()
