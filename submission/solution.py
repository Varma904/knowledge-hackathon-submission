"""Baseline submission copied from the provided starter.

This file intentionally preserves the starter behavior so later score changes can
be attributed to our implementation work. The required contract is build() plus
ingest(), answer(), and conflicts() on the returned object.
"""

import re
import sys
from pathlib import Path

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
from contract import Answer, Snapshot, Source  # noqa: E402


WORD = re.compile(r"[a-z0-9$%.]+")


class NaiveKnowledge:
    """Store only the newest page copies and retrieve by word overlap."""

    def __init__(self):
        self.pages = {}

    def ingest(self, snapshot: Snapshot) -> None:
        for page in snapshot.pages:
            self.pages[page.url] = (snapshot.snapshot_id, page.markdown)

    def answer(self, question, as_of=None):
        question_words = set(WORD.findall(question.lower()))
        best = None
        best_score = 0

        for url, (snapshot_id, markdown) in self.pages.items():
            for paragraph in markdown.split("\n\n"):
                score = len(question_words & set(WORD.findall(paragraph.lower())))
                if score > best_score:
                    best = (url, snapshot_id, paragraph.strip())
                    best_score = score

        if not best:
            return Answer(text="I don't know.", confident=False)

        url, snapshot_id, paragraph = best
        return Answer(
            text=paragraph[:600],
            sources=[Source(url=url, snapshot_id=snapshot_id)],
        )

    def conflicts(self):
        return []


def build():
    return NaiveKnowledge()
