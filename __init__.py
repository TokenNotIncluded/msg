"""msg for bot need: a small, independently implemented recommendation library."""

from .ranker import ALGORITHM_VERSION, Candidate, Recommendation, rank

__all__ = ['ALGORITHM_VERSION', 'Candidate', 'Recommendation', 'rank']
