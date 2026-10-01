"""Explicit follows, explicit interests and recency. No tracking or models."""

import math
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

ALGORITHM_VERSION = '0.1.0'
MAX_CANDIDATES = 256
FOLLOW_WEIGHT = 3.0
INTEREST_WEIGHT = 2.0
HALF_LIFE_HOURS = 24.0


@dataclass(frozen=True)
class Candidate:
    id: str
    author: str
    created_at: datetime
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Recommendation:
    candidate: Candidate
    score: float
    reasons: tuple[str, ...]


def rank(candidates, *, now, following=(), interests=(), limit=20):
    """Rank already-visible candidates; the caller owns all access checks.

    Raw score = 3 * followed + 2 * interest overlap + 24-hour freshness.
    Selection divides this by 1 + earlier items by the same author. Ties use ID.
    Interest overlap is the fraction of the viewer's requested tags matched.
    """
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('limit must be between 1 and 100')
    if now.utcoffset() is None:
        raise ValueError('now must have a timezone')
    candidates = list(candidates)
    if len(candidates) > MAX_CANDIDATES:
        raise ValueError('too many candidates')
    if len({item.id for item in candidates}) != len(candidates):
        raise ValueError('duplicate candidate ID')
    following = frozenset(following)
    interests = frozenset(tag.casefold() for tag in interests)
    pending = []
    for item in candidates:
        if item.created_at.utcoffset() is None:
            raise ValueError('candidate time must have a timezone')
        age_hours = max(0.0, (now - item.created_at).total_seconds() / 3600)
        score = math.exp2(-age_hours / HALF_LIFE_HOURS)
        reasons = ['recent']
        if item.author in following:
            score += FOLLOW_WEIGHT
            reasons.append('followed_author')
        matches = interests & {tag.casefold() for tag in item.tags}
        if matches:
            score += INTEREST_WEIGHT * len(matches) / len(interests)
            reasons.append('interest_match')
        pending.append(Recommendation(item, score, tuple(reasons)))
    selected, counts = [], Counter()
    while pending and len(selected) < limit:
        best = min(
            pending,
            key=lambda item: (
                -item.score / (1 + counts[item.candidate.author]),
                item.candidate.id,
            ),
        )
        pending.remove(best)
        selected.append(best)
        counts[best.candidate.author] += 1
    return selected
