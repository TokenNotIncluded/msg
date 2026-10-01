import unittest
from datetime import UTC, datetime, timedelta

from msg_algorithm import Candidate, rank

NOW = datetime(2026, 10, 1, tzinfo=UTC)


class RankingTests(unittest.TestCase):
    def test_explicit_follow_and_interests_win_without_history(self):
        fresh = Candidate('fresh', 'other', NOW)
        useful = Candidate('useful', 'followed', NOW - timedelta(days=1), ('Python',))
        result = rank([fresh, useful], now=NOW, following={'followed'}, interests={'python'})
        self.assertEqual(result[0].candidate.id, 'useful')
        self.assertEqual(result[0].score, 5.5)
        self.assertEqual(result[0].reasons, ('recent', 'followed_author', 'interest_match'))

    def test_cold_start_and_input_order_are_deterministic(self):
        candidates = [Candidate('b', 'b', NOW), Candidate('a', 'a', NOW)]
        self.assertEqual(rank(candidates, now=NOW), rank(candidates[::-1], now=NOW))
        self.assertEqual(rank(candidates, now=NOW)[0].candidate.id, 'a')

    def test_diversity_does_not_change_raw_scores(self):
        candidates = [
            Candidate('a', 'one', NOW),
            Candidate('b', 'one', NOW),
            Candidate('c', 'two', NOW),
        ]
        result = rank(candidates, now=NOW)
        self.assertEqual([item.candidate.id for item in result], ['a', 'c', 'b'])
        self.assertTrue(all(item.score == 1 for item in result))

    def test_tag_stuffing_does_not_boost_interest_fraction(self):
        plain = Candidate('a', 'a', NOW, ('python',))
        stuffed = Candidate('b', 'b', NOW, ('python', 'random', 'other'))
        result = rank([plain, stuffed], now=NOW, interests={'python', 'rust'})
        self.assertEqual(result[0].score, result[1].score)

    def test_limits_duplicates_and_naive_time_are_rejected(self):
        item = Candidate('a', 'a', NOW)
        for args in ({'limit': 101}, {'limit': True}, {'now': NOW.replace(tzinfo=None)}):
            with self.assertRaises(ValueError):
                rank([item], **({'now': NOW} | args))
        with self.assertRaises(ValueError):
            rank([item, item], now=NOW)
        with self.assertRaises(ValueError):
            rank([Candidate(str(i), 'a', NOW) for i in range(257)], now=NOW)


if __name__ == '__main__':
    unittest.main()
