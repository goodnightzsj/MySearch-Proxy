import unittest

from mysearch.research import events


class ResultEventEvidenceTests(unittest.TestCase):
    def test_best_actor_uses_person_not_quoted_film(self):
        answer = events._extract_result_event_answer(
            query="2026 Oscars best actor winner",
            results=[{"url": "https://apnews.com/article/2026-oscars-best-actor",
                      "title": "2026 Oscars best actor winner",
                      "snippet": "Michael B. Jordan wins best actor for ‘Sinners,’ taking home his first Oscar."}],
        )
        self.assertEqual(answer, "Best Actor winner: Michael B. Jordan")

    def test_best_actor_does_not_promote_film_without_person(self):
        answer = events._extract_result_event_answer(
            query="2026 Oscars best actor winner",
            results=[{"url": "https://apnews.com/article/2026-oscars-best-actor",
                      "title": "2026 Oscars best actor winner",
                      "snippet": "Wins best actor for ‘Sinners.’"}],
        )
        self.assertEqual(answer, "")

    def test_record_prefers_explicit_work_over_earlier_performers(self):
        answer = events._extract_result_event_answer(
            query="2026 Grammy Record of the Year winner",
            results=[{"url": "https://www.grammy.com/news/2026-grammys-winners-list",
                      "title": "2026 Grammy winners list",
                      "snippet": "Record of the Year, which went to Kendrick Lamar and SZA."},
                     {"url": "https://apnews.com/article/2026-grammy-winners-list",
                      "title": "2026 Grammy winners list",
                      "snippet": 'The artists accept the award for record of the year for "Luther".'}],
        )
        self.assertEqual(answer, "Record of the Year winner: Luther")

    def test_record_keeps_performer_when_no_work_is_identified(self):
        answer = events._extract_result_event_answer(
            query="2026 Grammy Record of the Year winner",
            results=[{"url": "https://www.grammy.com/news/2026-grammys-winners-list",
                      "title": "2026 Grammy winners list",
                      "snippet": "Record of the Year, which went to Kendrick Lamar and SZA."}],
        )
        self.assertEqual(answer, "Record of the Year winner: Kendrick Lamar and SZA")

    def test_record_keeps_explicit_quoted_award_statement_in_first_pass(self):
        answer = events._extract_result_event_answer(
            query="2026 Grammy Record of the Year winner",
            results=[{"url": "https://www.grammy.com/news/2026-grammys-winners-list",
                      "title": "2026 Grammy winners list",
                      "snippet": 'Record of the Year was awarded to "First Song".'},
                     {"url": "https://apnews.com/article/2026-grammy-winners-list",
                      "title": "2026 Grammy winners list",
                      "snippet": "Record of the Year winner: Second Song."}],
        )
        self.assertEqual(answer, "Record of the Year winner: First Song")

    def test_record_nomination_is_not_an_award_acceptance(self):
        answer = events._extract_result_event_answer(
            query="2026 Grammy Record of the Year winner",
            results=[{"url": "https://www.grammy.com/news/2026-grammys-nominees",
                      "title": "2026 Grammy nominations",
                      "snippet": 'Nominated for record of the year for "Other Song".'}],
        )
        self.assertEqual(answer, "")


if __name__ == "__main__":
    unittest.main()
