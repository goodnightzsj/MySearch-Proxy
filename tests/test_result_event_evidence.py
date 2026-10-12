import unittest

from mysearch.research import events


class ResultEventEvidenceTests(unittest.TestCase):
    def test_award_list_uses_explicit_winner_not_first_nominee(self):
        for category, label in (("Record of the Year", "Record of the Year"),
                                ("Best Picture", "Best Picture")):
            for text in (f'## {category}\n\n“Opening Song” — Artist\n“Actual Work” — Artist | WINNER',
                         f'1. {category}\n\n● "Opening Song" by Artist\n✩ WINNER: "Actual Work" by Artist',
                         f'{category}\n“Opening Song”\n“Actual Work” WINNER'):
                with self.subTest(category=category, text=text):
                    answer = events._extract_result_event_answer(
                        query=f"2026 {category} winner",
                        results=[{"title": "2026 winners and nominees", "snippet": text}],
                    )
                    self.assertEqual(answer, f"{label} winner: Actual Work")

    def test_nominee_list_does_not_borrow_another_category_winner(self):
        for heading in ("## Album of the Year", "2. Album of the Year", "Pop solo performance"):
            for separator in ("\n", ":\n"):
                with self.subTest(heading=heading, separator=separator):
                    answer = events._extract_result_event_answer(
                        query="2026 Record of the Year winner",
                        results=[{"title": "2026 nominees", "snippet": (
                            "Record of the Year" + separator + '“Opening Song”\n\n'
                            + heading + '\n“Other Work” | WINNER'
                        )}],
                    )
                    self.assertEqual(answer, "")

    def test_record_list_work_outranks_recipient_only_statement(self):
        answer = events._extract_result_event_answer(
            query="2026 Grammy Record of the Year winner",
            results=[{"title": "2026 Grammy winners", "snippet": "Record of the Year went to Two Artists."},
                     {"title": "2026 Grammy winners", "snippet": 'Record of the Year\n"Opening Song"\n"Actual Work" | WINNER'}],
        )
        self.assertEqual(answer, "Record of the Year winner: Actual Work")

    def test_other_award_headers_do_not_promote_a_nominee(self):
        for category in ("Best Picture", "Best Actor", "Album of the Year"):
            with self.subTest(category=category):
                self.assertEqual(events._extract_result_event_answer(
                    query=f"2026 {category} winner",
                    results=[{"title": "2026 nominees", "snippet": (
                        f'{category}:\n“Opening Song”\n\nPop solo performance\n“Other Work” | WINNER'
                    )}],
                ), "")

    def test_award_list_supports_repeated_numbered_excerpt_heading(self):
        self.assertEqual(events._extract_result_event_answer(
            query="2026 Grammy Record of the Year winner",
            results=[{"title": "2026 Grammy winners", "snippet": (
                '1. Record of the Year [...] 1. Record of the Year\n'
                '● "Opening Song" by Artist\n✩ WINNER: "Actual Work" by Artist'
            )}],
        ), "Record of the Year winner: Actual Work")

    def test_best_picture_acceptance_identifies_the_film(self):
        answer = events._extract_result_event_answer(
            query="2026 Oscars Best Picture winner",
            results=[{"title": "2026 Oscars winners", "snippet": 'The producers accept the Oscar for best picture for “Actual Film.”'}],
        )
        self.assertEqual(answer, "Best Picture winner: Actual Film")

    def test_award_list_does_not_join_different_pages(self):
        self.assertEqual(events._extract_result_event_answer(
            query="2026 Record of the Year winner",
            results=[{"url": "https://a.test/record", "title": "Record of the Year", "snippet": "Record of the Year"},
                     {"url": "https://b.test/album", "title": "Album of the Year", "snippet": '"Other Work" | WINNER'}],
        ), "")

    def test_award_list_can_join_fields_of_the_same_page(self):
        self.assertEqual(events._extract_result_event_answer(
            query="2026 Record of the Year winner",
            results=[{"title": "2026 winners", "snippet": "Record of the Year",
                      "content": '"Actual Work" | WINNER'}],
        ), "Record of the Year winner: Actual Work")

    def test_box_office_does_not_extract_mid_sentence_fragments(self):
        for text in (
            'The film crossed the $1 billion milestone in just four days and became the highest-grossing release.',
            'Gen Z has also become the biggest opening weekend audience.',
        ):
            with self.subTest(text=text):
                self.assertEqual(events._extract_result_event_answer_from_text(
                    query_lower="2026 highest grossing movie opening weekend", text=text,
                ), "")

    def test_box_office_keeps_unquoted_title_and_sentence_boundary(self):
        for text in (
            'Project Hail Mary becomes the biggest opening weekend story.',
            'News update. Project Hail Mary becomes the biggest opening weekend story.',
            'News update\nProject Hail Mary becomes the biggest opening weekend story.',
            'News update\r\nProject Hail Mary becomes the biggest opening weekend story.',
        ):
            with self.subTest(text=text):
                self.assertEqual(events._extract_result_event_answer_from_text(
                    query_lower="2026 highest grossing movie opening weekend", text=text,
                ), "Top opening-weekend title: Project Hail Mary")

    def test_box_office_keeps_title_numbers_and_connectors(self):
        for title in ("Toy Story 5", "Solar Harbor - Final Journey", "Thunder & Lightning"):
            with self.subTest(title=title):
                self.assertEqual(events._extract_result_event_answer_from_text(
                    query_lower="2026 highest grossing movie opening weekend",
                    text=f"{title} becomes the biggest opening weekend story.",
                ), f"Top opening-weekend title: {title}")

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

    def test_best_actor_does_not_promote_people_without_winner_statement(self):
        for text in (
            "Best actor competitors Avery Cole and Casey Morgan greeted each other.",
            "Best actor candidates Avery Cole and Casey Morgan attended the ceremony.",
            "Best actor presenters Avery Cole and Casey Morgan took the stage.",
        ):
            with self.subTest(text=text):
                self.assertEqual(events._extract_result_event_answer(
                    query="2026 Oscars best actor winner",
                    results=[{"title": "2026 Oscars ceremony", "snippet": text}],
                ), "")

    def test_best_actor_keeps_explicit_winner_and_category_labels(self):
        for text in (
            "Best actor winner Casey Morgan.", "Best actor is Casey Morgan.",
            "Best actor goes to Casey Morgan.", "Best actor: Casey Morgan.",
        ):
            with self.subTest(text=text):
                self.assertEqual(events._extract_result_event_answer(
                    query="2026 Oscars best actor winner",
                    results=[{"title": "2026 Oscars ceremony", "snippet": text}],
                ), "Best Actor winner: Casey Morgan")

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
