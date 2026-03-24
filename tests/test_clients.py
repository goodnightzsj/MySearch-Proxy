from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mysearch.clients import MySearchClient, MySearchError, MySearchHTTPError, RouteDecision


class _FakeResponse:
    def __init__(self, text: str, status: int = 200) -> None:
        self._text = text
        self.status = status

    def read(self) -> bytes:
        return self._text.encode("utf-8")

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class MySearchClientTests(unittest.TestCase):
    def test_parse_result_timestamp_supports_rfc822(self) -> None:
        client = MySearchClient()

        parsed = client._parse_result_timestamp("Sun, 22 Mar 2026 20:43:36 GMT")

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.isoformat(), "2026-03-22T20:43:36+00:00")

    def test_news_rerank_prefers_newer_rfc822_result(self) -> None:
        client = MySearchClient()
        results = [
            {
                "provider": "exa",
                "title": "Older entertainment hit",
                "url": "https://deadline.com/2026/03/older-entertainment-hit",
                "published_date": "2026-03-09T00:00:00.000Z",
            },
            {
                "provider": "tavily",
                "title": "Newer entertainment hit",
                "url": "https://fortune.com/2026/03/22/newer-entertainment-hit/",
                "published_date": "Sun, 22 Mar 2026 20:43:36 GMT",
                "snippet": "Fresh box office update",
            },
        ]

        ranked = client._rerank_general_results(
            query="2026 highest grossing movie opening weekend",
            result_profile="news",
            results=results,
            include_domains=None,
        )

        self.assertEqual(ranked[0]["provider"], "tavily")
        self.assertEqual(ranked[0]["title"], "Newer entertainment hit")

    def test_news_rerank_prefers_query_relevant_story_over_generic_newer_article(self) -> None:
        client = MySearchClient()
        results = [
            {
                "provider": "tavily",
                "title": "Report: Giants Tried To Trade For Notable Veteran Linebacker",
                "url": "https://nfltraderumors.co/report-giants-tried-to-trade-for-notable-veteran-linebacker/",
                "published_date": "Mon, 23 Mar 2026 13:00:00 GMT",
                "snippet": "NFL trade rumor roundup.",
            },
            {
                "provider": "tavily",
                "title": "Barry Keoghan Reveals He Hid From Online Hate After Sabrina Carpenter Split",
                "url": "https://www.tmz.com/2026/03/21/barry-keoghan-talks-online-haters/",
                "published_date": "Sat, 21 Mar 2026 15:54:42 GMT",
                "snippet": "The actor addressed breakup rumors after the split.",
            },
        ]

        ranked = client._rerank_general_results(
            query="latest celebrity breakup rumors 2026",
            result_profile="news",
            results=results,
            include_domains=None,
        )

        self.assertIn("Split", ranked[0]["title"])

    def test_resolve_intent_treats_webhooks_official_as_resource(self) -> None:
        client = MySearchClient()

        result = client._resolve_intent(
            query="OpenAI webhooks official",
            mode="auto",
            intent="auto",
            sources=["web"],
        )

        self.assertEqual(result, "resource")

    def test_resolve_intent_treats_debugging_query_as_tutorial(self) -> None:
        client = MySearchClient()

        result = client._resolve_intent(
            query="Playwright strict mode violation fix",
            mode="auto",
            intent="auto",
            sources=["web"],
        )

        self.assertEqual(result, "tutorial")

    def test_resolve_intent_treats_award_winner_query_as_news(self) -> None:
        client = MySearchClient()

        result = client._resolve_intent(
            query="2026 Oscars best picture winner",
            mode="auto",
            intent="auto",
            sources=["web"],
        )

        self.assertEqual(result, "news")

    def test_news_route_policy_prefers_exa_rescue_without_firecrawl_search(self) -> None:
        client = MySearchClient()

        policy = client._route_policy_for_request(
            query="2026 Oscars best picture winner",
            mode="news",
            intent="news",
            include_content=False,
        )

        self.assertEqual(policy.key, "news")
        self.assertEqual(policy.provider, "tavily")
        self.assertEqual(policy.fallback_chain, ("exa",))

    def test_news_verify_does_not_enable_tavily_firecrawl_blend(self) -> None:
        client = MySearchClient()
        client._provider_is_live_ok = lambda provider: True  # type: ignore[method-assign]

        should_blend = client._should_blend_web_providers(
            query="2026 Oscars best picture winner",
            requested_provider="auto",
            decision=RouteDecision(provider="tavily", reason="test", result_profile="news"),
            sources=["web"],
            strategy="verify",
            mode="news",
            intent="news",
            include_domains=None,
        )

        self.assertFalse(should_blend)

    def test_dispatch_single_provider_for_news_result_query_requests_content_in_verify(self) -> None:
        client = MySearchClient()
        captured: dict[str, object] = {}

        def fake_search_tavily(**kwargs):  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            return {
                "provider": "tavily",
                "results": [],
                "citations": [],
                "answer": "",
            }

        client._search_tavily = fake_search_tavily  # type: ignore[method-assign]

        client._dispatch_single_provider(
            provider_name="tavily",
            query="2026 Oscars best picture winner",
            max_results=3,
            mode="news",
            intent="news",
            decision=RouteDecision(
                provider="tavily",
                reason="test",
                tavily_topic="news",
                result_profile="news",
            ),
            include_answer=True,
            include_content=False,
            include_domains=None,
            exclude_domains=None,
            strategy="verify",
        )

        self.assertTrue(captured["include_content"])

    def test_news_rerank_prefers_award_winners_page_over_nominations_page(self) -> None:
        client = MySearchClient()

        ranked = client._rerank_general_results(
            query="2026 Grammy Album of the Year winner",
            result_profile="news",
            results=[
                {
                    "provider": "tavily",
                    "title": "2026 GRAMMYS Nominations: Album Of The Year Nominees | GRAMMY.com",
                    "url": "https://www.grammy.com/news/2026-grammys-nominations-album-of-the-year",
                    "snippet": "",
                    "content": "",
                },
                {
                    "provider": "tavily",
                    "title": "The complete list of 2026 Grammy winners and nominees : NPR",
                    "url": "https://www.npr.org/2026/02/01/nx-s1-5693046/2026-grammy-awards-full-list-winners-nominees",
                    "snippet": "",
                    "content": "",
                },
            ],
            include_domains=None,
        )

        self.assertIn("winners", ranked[0]["title"].lower())

    def test_news_rerank_downranks_predictions_and_year_mismatch_for_award_results(self) -> None:
        client = MySearchClient()

        ranked = client._rerank_general_results(
            query="2026 Oscars best actor winner",
            result_profile="news",
            results=[
                {
                    "provider": "tavily",
                    "title": "Oscars 2027 early prediction: who will win next year",
                    "url": "https://www.theguardian.com/film/2026/mar/18/oscars-2027-early-prediction-wins",
                    "snippet": "",
                    "content": "",
                },
                {
                    "provider": "tavily",
                    "title": "Oscars 2026 winners list: Best Actor goes to Colman Domingo",
                    "url": "https://www.hollywoodreporter.com/lists/oscars-2026-winners-list-best-actor",
                    "snippet": "",
                    "content": "",
                },
            ],
            include_domains=None,
        )

        self.assertIn("best actor", ranked[0]["title"].lower())

    def test_news_rerank_prefers_category_match_over_generic_awards_article(self) -> None:
        client = MySearchClient()

        ranked = client._rerank_general_results(
            query="2026 Grammy Record of the Year winner",
            result_profile="news",
            results=[
                {
                    "provider": "tavily",
                    "title": "Norah Jones, Ray Charles to be honored at 2026 Grammy Hall of Fame Gala",
                    "url": "https://www.billboard.com/music/awards/norah-jones-ray-charles-award-2026-grammy-hall-of-fame-gala-1236201595/",
                    "snippet": "",
                    "content": "",
                },
                {
                    "provider": "tavily",
                    "title": "The complete list of 2026 Grammy winners and nominees",
                    "url": "https://www.npr.org/2026/02/01/nx-s1-5693046/2026-grammy-awards-full-list-winners-nominees",
                    "snippet": "Record of the Year — Not Like Us",
                    "content": "",
                },
            ],
            include_domains=None,
        )

        self.assertIn("winners", ranked[0]["title"].lower())

    def test_apply_result_event_answer_override_extracts_best_picture_winner(self) -> None:
        client = MySearchClient()

        result = client._apply_result_event_answer_override(
            query="2026 Oscars best picture winner",
            mode="news",
            intent="news",
            strategy="verify",
            result={
                "answer": "Not yet determined",
                "results": [
                    {
                        "title": "Oscars 2026 winners list",
                        "url": "https://example.com/oscars-2026",
                        "snippet": "Best Picture — One Battle After Another",
                        "content": "",
                    }
                ],
                "evidence": {},
            },
        )

        self.assertEqual(result["answer"], "Best Picture winner: One Battle After Another")
        self.assertEqual(result["evidence"]["answer_source"], "result-event-extraction")

    def test_apply_result_event_answer_override_extracts_best_actor_winner(self) -> None:
        client = MySearchClient()

        result = client._apply_result_event_answer_override(
            query="2026 Oscars best actor winner",
            mode="news",
            intent="news",
            strategy="verify",
            result={
                "answer": "",
                "results": [
                    {
                        "title": "Oscars 2026 winners list",
                        "url": "https://example.com/oscars-2026",
                        "snippet": "Best Actor — Colman Domingo",
                        "content": "",
                    }
                ],
                "evidence": {},
            },
        )

        self.assertEqual(result["answer"], "Best Actor winner: Colman Domingo")
        self.assertEqual(result["evidence"]["answer_source"], "result-event-extraction")

    def test_apply_result_event_answer_override_extracts_best_picture_from_headline_style_result(self) -> None:
        client = MySearchClient()

        result = client._apply_result_event_answer_override(
            query="2026 Oscars best picture winner",
            mode="news",
            intent="news",
            strategy="verify",
            result={
                "answer": "",
                "results": [
                    {
                        "title": "'One Battle After Another' is the 2026 Best Picture winner at the Academy Awards",
                        "url": "https://example.com/oscars-best-picture",
                        "snippet": "",
                        "content": "",
                    }
                ],
                "evidence": {},
            },
        )

        self.assertEqual(result["answer"], "Best Picture winner: One Battle After Another")

    def test_apply_result_event_answer_override_extracts_best_actor_from_headline_style_result(self) -> None:
        client = MySearchClient()

        result = client._apply_result_event_answer_override(
            query="2026 Oscars best actor winner",
            mode="news",
            intent="news",
            strategy="verify",
            result={
                "answer": "",
                "results": [
                    {
                        "title": "Michael B. Jordan wins Best Actor at Oscars 2026",
                        "url": "https://example.com/oscars-best-actor",
                        "snippet": "",
                        "content": "",
                    }
                ],
                "evidence": {},
            },
        )

        self.assertEqual(result["answer"], "Best Actor winner: Michael B. Jordan")

    def test_apply_result_event_answer_override_extracts_box_office_title(self) -> None:
        client = MySearchClient()

        result = client._apply_result_event_answer_override(
            query="2026 highest grossing movie opening weekend",
            mode="news",
            intent="news",
            strategy="verify",
            result={
                "answer": "",
                "results": [
                    {
                        "title": "‘Project Hail Mary’ becomes Amazon’s highest-grossing film debut",
                        "url": "https://fortune.com/project-hail-mary",
                        "snippet": "",
                        "content": "",
                    }
                ],
                "evidence": {},
            },
        )

        self.assertEqual(result["answer"], "Top opening-weekend title: Project Hail Mary")
        self.assertEqual(result["evidence"]["answer_source"], "result-event-extraction")

    def test_firecrawl_news_search_omits_unsupported_news_category(self) -> None:
        client = MySearchClient()
        captured: dict[str, object] = {}
        client._get_key_or_raise = lambda provider: type(  # type: ignore[method-assign]
            "Record",
            (),
            {"key": "firecrawl-key", "source": "env"},
        )()

        def fake_request_json(**kwargs):  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            return {"data": {"news": [], "web": []}}

        client._request_json = fake_request_json  # type: ignore[method-assign]

        client._search_firecrawl_once(
            query="2026 Oscars best picture winner",
            max_results=3,
            categories=["news"],
            include_content=True,
        )

        payload = captured["payload"]
        assert isinstance(payload, dict)
        self.assertNotIn("categories", payload)
        self.assertNotIn("scrapeOptions", payload)

    def test_apply_result_event_answer_override_extracts_album_of_the_year_from_page_content(self) -> None:
        client = MySearchClient()

        client.extract_url = lambda **kwargs: {  # type: ignore[method-assign]
            "content": (
                "Bad Bunny won album of the year for his album "
                "_DeBÍ TiRAR MáS FOToS_, marking the first time a primarily "
                "Spanish-language album has won album of the year."
            )
        }

        result = client._apply_result_event_answer_override(
            query="2026 Grammy Album of the Year winner",
            mode="news",
            intent="news",
            strategy="verify",
            result={
                "answer": "",
                "results": [
                    {
                        "title": "The complete list of 2026 Grammy winners and nominees",
                        "url": "https://www.npr.org/2026/02/01/grammys",
                        "snippet": "",
                        "content": "",
                    }
                ],
                "evidence": {},
            },
        )

        self.assertEqual(
            result["answer"],
            "Album of the Year winner: DeBÍ TiRAR MáS FOToS by Bad Bunny",
        )
        self.assertEqual(result["evidence"]["answer_source"], "result-event-extraction")

    def test_apply_result_event_answer_override_extracts_record_of_the_year_from_page_content(self) -> None:
        client = MySearchClient()

        client.extract_url = lambda **kwargs: {  # type: ignore[method-assign]
            "content": (
                "\"Not Like Us\" won Record of the Year, while other categories "
                "included additional performances later in the ceremony."
            )
        }

        result = client._apply_result_event_answer_override(
            query="2026 Grammy Record of the Year winner",
            mode="news",
            intent="news",
            strategy="verify",
            result={
                "answer": "",
                "results": [
                    {
                        "title": "The complete list of 2026 Grammy winners and nominees",
                        "url": "https://www.npr.org/2026/02/01/grammys",
                        "snippet": "",
                        "content": "",
                    }
                ],
                "evidence": {},
            },
        )

        self.assertEqual(result["answer"], "Record of the Year winner: Not Like Us")
        self.assertEqual(result["evidence"]["answer_source"], "result-event-extraction")

    def test_apply_result_event_answer_override_extracts_record_of_the_year_from_headline_style_result(self) -> None:
        client = MySearchClient()

        result = client._apply_result_event_answer_override(
            query="2026 Grammy Record of the Year winner",
            mode="news",
            intent="news",
            strategy="verify",
            result={
                "answer": "",
                "results": [
                    {
                        "title": '"Not Like Us" wins Record of the Year',
                        "url": "https://example.com/grammys-record-year",
                        "snippet": "",
                        "content": "",
                    }
                ],
                "evidence": {},
            },
        )

        self.assertEqual(result["answer"], "Record of the Year winner: Not Like Us")

    def test_apply_result_event_answer_override_tries_multiple_top_pages(self) -> None:
        client = MySearchClient()
        page_payloads = {
            "https://example.com/first": {"content": ""},
            "https://example.com/second": {"content": 'Record of the Year: "Not Like Us", Kendrick Lamar'},
        }
        client.extract_url = lambda **kwargs: page_payloads[kwargs["url"]]  # type: ignore[method-assign]

        result = client._apply_result_event_answer_override(
            query="2026 Grammy Record of the Year winner",
            mode="news",
            intent="news",
            strategy="verify",
            result={
                "answer": "",
                "results": [
                    {
                        "title": "The complete list of 2026 Grammy winners and nominees",
                        "url": "https://example.com/first",
                        "snippet": "",
                        "content": "",
                    },
                    {
                        "title": "2026 Grammys: See The Full Winners & Nominees List",
                        "url": "https://example.com/second",
                        "snippet": "",
                        "content": "",
                    },
                ],
                "evidence": {},
            },
        )

        self.assertEqual(result["answer"], "Record of the Year winner: Not Like Us")

    def test_apply_result_event_answer_override_uses_page_extraction_for_uncertain_balanced_answer(self) -> None:
        client = MySearchClient()
        client.extract_url = lambda **kwargs: {  # type: ignore[method-assign]
            "content": (
                "Bad Bunny won album of the year for his album "
                "_DeBÍ TiRAR MáS FOToS_."
            )
        }

        result = client._apply_result_event_answer_override(
            query="2026 Grammy Album of the Year winner",
            mode="news",
            intent="news",
            strategy="balanced",
            result={
                "answer": "The winner cannot be determined from the provided data.",
                "results": [
                    {
                        "title": "Grammy Awards winners list: See which nominees are taking home golden gramophones",
                        "url": "https://example.com/grammys",
                        "snippet": "",
                        "content": "",
                    }
                ],
                "evidence": {},
            },
        )

        self.assertEqual(
            result["answer"],
            "Album of the Year winner: DeBÍ TiRAR MáS FOToS by Bad Bunny",
        )
        self.assertEqual(result["evidence"]["answer_source"], "result-event-extraction")

    def test_extract_result_event_answer_trims_trailing_list_noise(self) -> None:
        client = MySearchClient()

        answer = client._extract_result_event_answer(
            query="2026 Oscars best picture winner",
            results=[
                {
                    "title": "Oscars 2026 winners list",
                    "snippet": 'Best Picture winner: One Battle After Another, "Sinners," and "Dune: Messiah" also won major prizes.',
                    "content": "",
                }
            ],
        )

        self.assertEqual(answer, "Best Picture winner: One Battle After Another")

    def test_extract_album_of_the_year_answer_trims_trailing_explanatory_clause(self) -> None:
        client = MySearchClient()

        answer = client._extract_result_event_answer(
            query="2026 Grammy Album of the Year winner",
            results=[
                {
                    "title": "Grammy Awards winners list",
                    "snippet": "",
                    "content": (
                        "Bad Bunny won album of the year for his album "
                        "_DeBÍ TiRAR MáS FOToS_, marking the first time a primarily "
                        "Spanish-language album has won album of the year."
                    ),
                }
            ],
        )

        self.assertEqual(
            answer,
            "Album of the Year winner: DeBÍ TiRAR MáS FOToS by Bad Bunny",
        )

    def test_result_set_looks_weak_for_exa_rescue_for_award_prediction_results(self) -> None:
        client = MySearchClient()

        weak = client._result_set_looks_weak_for_exa_rescue(
            query="2026 Oscars best actor winner",
            mode="news",
            result={
                "results": [
                    {
                        "title": "Oscars 2027 early prediction: who will win next year",
                        "url": "https://www.theguardian.com/film/2026/mar/18/oscars-2027-early-prediction-wins",
                        "snippet": "",
                        "content": "",
                    }
                ]
            },
        )

        self.assertTrue(weak)

    def test_apply_result_event_answer_override_does_not_extract_from_weak_award_mentions(self) -> None:
        client = MySearchClient()

        result = client._apply_result_event_answer_override(
            query="2026 Oscars best picture winner",
            mode="news",
            intent="news",
            strategy="verify",
            result={
                "answer": "",
                "results": [
                    {
                        "title": "Sean Penn Receives Mock Oscar in Ukraine After Skipping Academy Awards",
                        "url": "https://variety.com/2026/awards/news/sean-penn-mock-oscar-ukraine-skipping-academy-awards-1236692213/",
                        "snippet": (
                            "Sean Penn won best supporting actor for his performance "
                            "in Paul Thomas Anderson's best picture-winning One Battle After Another."
                        ),
                        "content": "",
                    }
                ],
                "evidence": {},
            },
        )

        self.assertEqual(result["answer"], "")

    def test_pdf_rerank_prefers_primary_named_paper_over_derivative_variants(self) -> None:
        client = MySearchClient()
        results = [
            {
                "provider": "tavily",
                "title": "DeepSeek-R1 Thoughtology: Let's think about LLM ...",
                "url": "https://arxiv.org/abs/2504.07128",
            },
            {
                "provider": "tavily",
                "title": "DeepSeek-R1: Incentivizing Reasoning Capability in LLMs ...",
                "url": "https://arxiv.org/html/2501.12948v1",
            },
        ]

        ranked = client._rerank_resource_results(
            query="DeepSeek R1 paper pdf",
            mode="pdf",
            results=results,
            include_domains=None,
        )

        self.assertIn("Incentivizing Reasoning Capability", ranked[0]["title"])

    def test_pdf_rerank_prefers_non_derivative_abs_page_over_survey_title(self) -> None:
        client = MySearchClient()
        results = [
            {
                "provider": "exa",
                "title": "[2505.00551] 100 Days After DeepSeek-R1: A Survey on Replication Studies and More Directions for Reasoning Language Models",
                "url": "https://arxiv.org/abs/2505.00551",
            },
            {
                "provider": "exa",
                "title": "Computer Science > Computation and Language",
                "url": "https://arxiv.org/abs/2501.12948?sfnsn=scwspmo",
            },
        ]

        ranked = client._rerank_resource_results(
            query="DeepSeek R1 paper pdf",
            mode="pdf",
            results=results,
            include_domains=["arxiv.org"],
        )

        self.assertEqual(ranked[0]["url"], "https://arxiv.org/abs/2501.12948")

    def test_canonical_result_url_rewrites_arxiv_html_variant_to_abs(self) -> None:
        client = MySearchClient()

        self.assertEqual(
            client._canonical_result_url("https://arxiv.org/html/2501.12948v1"),
            "https://arxiv.org/abs/2501.12948",
        )
        self.assertEqual(
            client._canonical_result_url("https://arxiv.gg/abs/2501.12948"),
            "https://arxiv.org/abs/2501.12948",
        )

    def test_pdf_query_tokenization_keeps_short_model_suffix(self) -> None:
        client = MySearchClient()

        brand_tokens = client._query_brand_tokens("DeepSeek R1 paper pdf")
        precision_tokens = client._query_precision_tokens("DeepSeek R1 paper pdf")
        subject_tokens = client._paper_query_subject_tokens(
            query="DeepSeek R1 paper pdf",
            query_tokens=brand_tokens,
            precision_tokens=precision_tokens,
        )

        self.assertNotIn("r1", brand_tokens)
        self.assertNotIn("r1", precision_tokens)
        self.assertEqual(subject_tokens[:2], ["deepseek", "r1"])

    def test_derivative_paper_title_with_named_prefix_does_not_block_exa_rescue(self) -> None:
        client = MySearchClient()

        strong_match = client._has_strong_pdf_match(
            query="DeepSeek R1 paper pdf",
            results=[
                {
                    "title": "DeepSeek-R1 Thoughtology: Let's think about LLM reasoning",
                    "url": "https://arxiv.org/pdf/2504.07128",
                }
            ],
        )

        self.assertFalse(strong_match)

    def test_pricing_keywords_alone_do_not_trigger_docs_mode(self) -> None:
        client = MySearchClient()

        self.assertFalse(client._looks_like_docs_query("openai pricing"))
        self.assertFalse(client._looks_like_docs_query("苹果 m4 macbook air 价格"))
        self.assertEqual(
            client._resolve_intent(
                query="苹果 M4 MacBook Air 国行价格 官方",
                mode="auto",
                intent="auto",
                sources=["web"],
            ),
            "factual",
        )

    def test_docs_tutorial_query_prefers_docs_policy_with_explicit_docs_mode(self) -> None:
        client = MySearchClient()
        query = "Playwright test.step tutorial example"

        resolved_intent = client._resolve_intent(
            query=query,
            mode="docs",
            intent="auto",
            sources=["web"],
        )
        policy = client._route_policy_for_request(
            query=query,
            mode="docs",
            intent=resolved_intent,
            include_content=True,
        )

        self.assertEqual(resolved_intent, "tutorial")
        self.assertEqual(policy.key, "content")
        self.assertEqual(policy.provider, "firecrawl")
        self.assertTrue(
            client._should_use_strict_resource_policy(
                query=query,
                mode="docs",
                intent=resolved_intent,
                include_domains=None,
            )
        )
        self.assertTrue(client._should_rerank_resource_results(mode="docs", intent=resolved_intent))

    def test_auto_tutorial_query_still_uses_tutorial_policy(self) -> None:
        client = MySearchClient()
        query = "Playwright test.step tutorial example"

        resolved_intent = client._resolve_intent(
            query=query,
            mode="auto",
            intent="auto",
            sources=["web"],
        )
        policy = client._route_policy_for_request(
            query=query,
            mode="auto",
            intent=resolved_intent,
            include_content=False,
        )

        self.assertEqual(resolved_intent, "tutorial")
        self.assertEqual(policy.key, "tutorial")
        self.assertEqual(policy.provider, "tavily")

    def test_rerank_resource_results_prefers_exact_tutorial_api_page_over_generic_docs(self) -> None:
        client = MySearchClient()

        ranked = client._rerank_resource_results(
            query="Playwright test.step tutorial example",
            mode="docs",
            results=[
                {
                    "provider": "firecrawl",
                    "title": "Running and debugging tests | Playwright",
                    "url": "https://playwright.dev/docs/running-tests",
                    "snippet": "Run tests and debug them in UI mode.",
                },
                {
                    "provider": "firecrawl",
                    "title": "TestStep | Playwright",
                    "url": "https://playwright.dev/docs/api/class-teststep",
                    "snippet": "API reference for test.step.",
                },
            ],
            include_domains=None,
        )

        self.assertEqual(
            ranked[0]["url"],
            "https://playwright.dev/docs/api/class-teststep",
        )

    def test_exa_category_does_not_treat_tutorial_query_as_research_paper(self) -> None:
        client = MySearchClient()

        self.assertEqual(client._exa_category("docs", "tutorial"), "")
        self.assertEqual(client._exa_category("pdf", "tutorial"), "research paper")

    def test_changelog_query_uses_tavily_news_policy(self) -> None:
        client = MySearchClient()

        policy = client._route_policy_for_request(
            query="Next.js 16 release notes official",
            mode="docs",
            intent="resource",
            include_content=True,
        )

        self.assertEqual(policy.key, "changelog")
        self.assertEqual(policy.provider, "tavily")
        self.assertEqual(policy.tavily_topic, "news")
        self.assertEqual(policy.firecrawl_categories, ("research",))
        self.assertTrue(policy.allow_exa_rescue)

    def test_tutorial_tavily_dispatch_disables_content_fetch(self) -> None:
        client = MySearchClient()
        captured = {}

        def fake_search_tavily(**kwargs):  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            return {"provider": "tavily", "results": [], "citations": []}

        client._search_tavily = fake_search_tavily  # type: ignore[method-assign]

        client._dispatch_single_provider(
            provider_name="tavily",
            query="Playwright test.step tutorial example",
            max_results=5,
            mode="docs",
            intent="tutorial",
            decision=RouteDecision(provider="tavily", reason="test", tavily_topic="general"),
            include_answer=False,
            include_content=True,
            include_domains=None,
            exclude_domains=None,
            strategy="balanced",
            from_date=None,
        )

        self.assertFalse(captured["include_content"])

    def test_life_query_skips_tavily_firecrawl_blend(self) -> None:
        client = MySearchClient()
        client._provider_is_live_ok = lambda provider: True  # type: ignore[method-assign]

        should_blend = client._should_blend_web_providers(
            query="上海 2026 春季赏花攻略",
            requested_provider="auto",
            decision=RouteDecision(provider="tavily", reason="test", result_profile="web"),
            sources=["web"],
            strategy="balanced",
            mode="web",
            intent="factual",
            include_domains=None,
        )

        self.assertFalse(should_blend)

    def test_strict_changelog_query_allows_tavily_firecrawl_blend(self) -> None:
        client = MySearchClient()
        client._provider_is_live_ok = lambda provider: True  # type: ignore[method-assign]

        should_blend = client._should_blend_web_providers(
            query="Next.js 16 release notes official",
            requested_provider="auto",
            decision=RouteDecision(provider="tavily", reason="test", result_profile="resource"),
            sources=["web"],
            strategy="verify",
            mode="docs",
            intent="resource",
            include_domains=["nextjs.org"],
        )

        self.assertTrue(should_blend)

    def test_request_json_auth_error_mentions_rejected_key(self) -> None:
        client = MySearchClient()
        provider = client.config.tavily
        error = HTTPError(
            url="https://example.com/search",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=io.BytesIO(
                b'{"error":"The account associated with this API key has been deactivated."}'
            ),
        )

        with patch("mysearch.clients.urlopen", side_effect=error):
            with self.assertRaises(MySearchHTTPError) as ctx:
                client._request_json(
                    provider=provider,
                    method="POST",
                    path=provider.path("search"),
                    payload={"query": "openai"},
                    key="test-key",
                )

        self.assertEqual(ctx.exception.provider, "tavily")
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertIn("configured but the API key was rejected", str(ctx.exception))
        self.assertIn("deactivated", str(ctx.exception))

    def test_health_reports_live_auth_error(self) -> None:
        client = MySearchClient()
        client._probe_provider_status = lambda provider, key_count: {  # type: ignore[method-assign]
            "status": "auth_error" if provider.name == "tavily" else "ok",
            "error": "tavily is configured but the API key was rejected (HTTP 401): deactivated"
            if provider.name == "tavily"
            else "",
            "checked_at": "2026-03-20T00:00:00+00:00",
        }

        payload = client.health()

        self.assertEqual(payload["providers"]["tavily"]["live_status"], "auth_error")
        self.assertIn("deactivated", payload["providers"]["tavily"]["live_error"])
        self.assertEqual(payload["providers"]["firecrawl"]["live_status"], "ok")

    def test_xai_compatible_health_probe_uses_root_health_endpoint(self) -> None:
        client = MySearchClient()
        provider = client.config.xai
        provider.search_mode = "compatible"
        provider.default_paths["social_search"] = "/social/search"
        provider.default_paths["social_health"] = "/social/health"
        provider.alternate_base_urls["social_search"] = "http://gateway.example/v1"
        provider.alternate_base_urls["social_health"] = "http://gateway.example/v1"
        calls: list[dict[str, object]] = []

        def fake_request_json(**kwargs):  # type: ignore[no-untyped-def]
            calls.append(kwargs)
            return {"status": "ok"}

        client._request_json = fake_request_json  # type: ignore[method-assign]

        client._probe_provider_request(provider, "gateway-token")

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["method"], "GET")
        self.assertEqual(calls[0]["path"], "/health")
        self.assertEqual(calls[0]["base_url"], "http://gateway.example")

    def test_xai_compatible_health_probe_falls_back_when_root_health_missing(self) -> None:
        client = MySearchClient()
        provider = client.config.xai
        provider.search_mode = "compatible"
        provider.default_paths["social_search"] = "/social/search"
        provider.default_paths["social_health"] = "/social/health"
        provider.alternate_base_urls["social_search"] = "http://gateway.example/admin?foo=1"
        provider.alternate_base_urls["social_health"] = "http://gateway.example/admin?foo=1"
        calls: list[dict[str, object]] = []

        def fake_request_json(**kwargs):  # type: ignore[no-untyped-def]
            calls.append(kwargs)
            if kwargs["path"] == "/health":
                raise MySearchHTTPError(
                    provider="xai",
                    status_code=404,
                    detail="not found",
                    url="http://gateway.example/health",
                )
            return {"provider": "custom_social", "results": [{"url": "https://x.com/openai/status/1"}]}

        client._request_json = fake_request_json  # type: ignore[method-assign]

        client._probe_provider_request(provider, "gateway-token")

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["method"], "GET")
        self.assertEqual(calls[0]["path"], "/health")
        self.assertEqual(calls[1]["method"], "POST")
        self.assertEqual(calls[1]["path"], "/social/search")
        self.assertEqual(calls[1]["payload"]["max_results"], 1)
        self.assertEqual(calls[1]["payload"]["model"], "grok-4.1-fast")

    def test_xai_official_health_probe_uses_status_page(self) -> None:
        client = MySearchClient()
        provider = client.config.xai
        provider.search_mode = "official"
        calls: list[dict[str, object]] = []

        def fake_request_text(**kwargs):  # type: ignore[no-untyped-def]
            calls.append(kwargs)
            return 200, "API (us-east-1.api.x.ai) available"

        client._request_text = fake_request_text  # type: ignore[method-assign]

        client._probe_provider_request(provider, "official-key")

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["url"], "https://status.x.ai/")

    def test_xai_official_health_probe_falls_back_to_fast_responses_when_status_check_fails(self) -> None:
        client = MySearchClient()
        provider = client.config.xai
        provider.search_mode = "official"
        provider.default_paths["responses"] = "/responses"
        text_calls: list[dict[str, object]] = []
        json_calls: list[dict[str, object]] = []

        def fake_request_text(**kwargs):  # type: ignore[no-untyped-def]
            text_calls.append(kwargs)
            raise MySearchError("unable to determine xAI API status from status.x.ai")

        def fake_request_json(**kwargs):  # type: ignore[no-untyped-def]
            json_calls.append(kwargs)
            return {"id": "resp_123", "status": "completed"}

        client._request_text = fake_request_text  # type: ignore[method-assign]
        client._request_json = fake_request_json  # type: ignore[method-assign]

        client._probe_provider_request(provider, "official-key")

        self.assertEqual(len(text_calls), 1)
        self.assertEqual(len(json_calls), 1)
        self.assertEqual(json_calls[0]["path"], "/responses")
        self.assertEqual(json_calls[0]["payload"]["model"], "grok-4.1-fast")

    def test_xai_compatible_search_timeout_falls_back_to_tavily_x_results(self) -> None:
        client = MySearchClient()
        provider = client.config.xai
        provider.search_mode = "compatible"
        provider.default_paths["social_search"] = "/social/search"
        provider.alternate_base_urls["social_search"] = "http://gateway.example/v1"
        client._get_key_or_raise = lambda provider: type(  # type: ignore[method-assign]
            "Record",
            (),
            {"key": "gateway-token", "source": "env"},
        )()
        calls: list[dict[str, object]] = []

        def fake_request_json(**kwargs):  # type: ignore[no-untyped-def]
            calls.append(kwargs)
            raise MySearchError("xai request timeout after 45s: http://127.0.0.1:9874/social/search")

        client._request_json = fake_request_json  # type: ignore[method-assign]
        client._search_tavily = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "tavily",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "Fallback social summary",
            "results": [
                {
                    "provider": "tavily",
                    "source": "web",
                    "title": "OpenAI on X",
                    "url": "https://x.com/OpenAI/status/123",
                    "snippet": "Latest OpenAI post",
                    "content": "",
                }
            ],
            "citations": [{"title": "OpenAI on X", "url": "https://x.com/OpenAI/status/123"}],
        }

        result = client._search_xai_compatible(
            query="latest OpenAI X posts GPT-5",
            sources=["x"],
            max_results=5,
            allowed_x_handles=None,
            excluded_x_handles=None,
            from_date=None,
            to_date=None,
            include_x_images=False,
            include_x_videos=False,
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["timeout_seconds"], 10)
        self.assertEqual(result["provider"], "tavily_social_fallback")
        self.assertEqual(result["results"][0]["url"], "https://x.com/OpenAI/status/123")
        self.assertEqual(result["fallback"]["from"], "xai_compatible")

    def test_github_blob_raw_urls_try_common_branch_aliases(self) -> None:
        client = MySearchClient()

        raw_urls = client._github_blob_raw_urls(
            "https://github.com/openai/openai-node/blob/main/README.md"
        )

        self.assertEqual(
            raw_urls,
            [
                "https://raw.githubusercontent.com/openai/openai-node/main/README.md",
                "https://raw.githubusercontent.com/openai/openai-node/master/README.md",
            ],
        )

    def test_extract_github_blob_raw_falls_back_to_master(self) -> None:
        client = MySearchClient()

        def fake_urlopen(request, timeout):
            if request.full_url.endswith("/main/README.md"):
                raise ValueError("404")
            return _FakeResponse("# OpenAI Node README")

        with patch("mysearch.clients.urlopen", side_effect=fake_urlopen):
            result = client._extract_github_blob_raw(
                url="https://github.com/openai/openai-node/blob/main/README.md"
            )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["provider"], "github_raw")
        self.assertEqual(
            result["metadata"]["raw_url"],
            "https://raw.githubusercontent.com/openai/openai-node/master/README.md",
        )

    def test_firecrawl_domain_filtered_search_falls_back_to_tavily(self) -> None:
        client = MySearchClient()
        client.keyring.has_provider = lambda provider: provider == "tavily"  # type: ignore[method-assign]
        client._probe_provider_status = lambda provider, key_count: {  # type: ignore[method-assign]
            "status": "ok",
            "error": "",
            "checked_at": "2026-03-20T00:00:00+00:00",
        }
        client._search_firecrawl_once = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "firecrawl",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [],
            "citations": [],
        }
        client._search_tavily = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "tavily",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "tavily",
                    "source": "web",
                    "title": "Responses | OpenAI API Reference",
                    "url": "https://platform.openai.com/docs/api-reference/responses",
                    "snippet": "OpenAI Responses API docs",
                    "content": "",
                }
            ],
            "citations": [
                {
                    "title": "Responses | OpenAI API Reference",
                    "url": "https://platform.openai.com/docs/api-reference/responses",
                }
            ],
        }

        result = client._search_firecrawl(
            query="OpenAI Responses API docs",
            max_results=5,
            categories=["technical"],
            include_content=False,
            include_domains=["openai.com"],
            exclude_domains=None,
        )

        self.assertEqual(result["provider"], "hybrid")
        self.assertEqual(result["route_selected"], "firecrawl+tavily")
        self.assertEqual(result["fallback"]["from"], "firecrawl")
        self.assertEqual(result["fallback"]["to"], "tavily")
        self.assertEqual(len(result["results"]), 1)

    def test_firecrawl_domain_filtered_search_retries_without_site_filter(self) -> None:
        client = MySearchClient()
        client.keyring.has_provider = lambda provider: False  # type: ignore[method-assign]

        def fake_search_firecrawl_once(**kwargs):  # type: ignore[no-untyped-def]
            query = kwargs["query"]
            if query.startswith("site:docs.firecrawl.dev "):
                return {
                    "provider": "firecrawl",
                    "transport": "env",
                    "query": query,
                    "answer": "",
                    "results": [],
                    "citations": [],
                }
            return {
                "provider": "firecrawl",
                "transport": "env",
                "query": query,
                "answer": "",
                "results": [
                    {
                        "provider": "firecrawl",
                        "source": "web",
                        "title": "Scrape - Firecrawl Docs",
                        "url": "https://docs.firecrawl.dev/api-reference/endpoint/scrape",
                        "snippet": "Official Firecrawl docs",
                        "content": "",
                    },
                    {
                        "provider": "firecrawl",
                        "source": "web",
                        "title": "Firecrawl tutorial recap",
                        "url": "https://example.com/firecrawl-scrape-guide",
                        "snippet": "Third-party recap",
                        "content": "",
                    },
                ],
                "citations": [
                    {
                        "title": "Scrape - Firecrawl Docs",
                        "url": "https://docs.firecrawl.dev/api-reference/endpoint/scrape",
                    },
                    {
                        "title": "Firecrawl tutorial recap",
                        "url": "https://example.com/firecrawl-scrape-guide",
                    },
                ],
            }

        client._search_firecrawl_once = fake_search_firecrawl_once  # type: ignore[method-assign]

        result = client._search_firecrawl(
            query="Firecrawl docs scrape api",
            max_results=5,
            categories=["technical"],
            include_content=False,
            include_domains=["docs.firecrawl.dev"],
            exclude_domains=None,
        )

        self.assertEqual(result["provider"], "firecrawl")
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(
            result["results"][0]["url"],
            "https://docs.firecrawl.dev/api-reference/endpoint/scrape",
        )
        self.assertEqual(
            result["route_debug"]["domain_filter_mode"],
            "client_filter_retry",
        )
        self.assertEqual(
            result["route_debug"]["retried_include_domains"],
            ["docs.firecrawl.dev"],
        )

    def test_firecrawl_domain_filtered_search_skips_tavily_auth_error_fallback(self) -> None:
        client = MySearchClient()
        client.keyring.has_provider = lambda provider: provider in {"tavily", "firecrawl"}  # type: ignore[method-assign]
        client._probe_provider_status = lambda provider, key_count: {  # type: ignore[method-assign]
            "status": "auth_error" if provider.name == "tavily" else "ok",
            "error": "tavily rejected" if provider.name == "tavily" else "",
            "checked_at": "2026-03-20T00:00:00+00:00",
        }
        client._search_firecrawl_once = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "firecrawl",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [],
            "citations": [],
        }
        client._search_tavily = lambda **kwargs: (_ for _ in ()).throw(AssertionError("should not call tavily"))  # type: ignore[method-assign]

        result = client._search_firecrawl(
            query="Firecrawl docs scrape api",
            max_results=5,
            categories=["technical"],
            include_content=False,
            include_domains=["docs.firecrawl.dev"],
            exclude_domains=None,
        )

        self.assertEqual(result["provider"], "firecrawl")
        self.assertEqual(result["results"], [])

    def test_tavily_domain_filtered_search_retries_with_site_query(self) -> None:
        client = MySearchClient()
        calls: list[dict[str, object]] = []

        def fake_search_tavily_once(**kwargs):  # type: ignore[no-untyped-def]
            calls.append(dict(kwargs))
            if kwargs["query"] == "OpenAI Responses API docs":
                return {
                    "provider": "tavily",
                    "transport": "env",
                    "query": kwargs["query"],
                    "answer": "",
                    "results": [],
                    "citations": [],
                }
            return {
                "provider": "tavily",
                "transport": "env",
                "query": kwargs["query"],
                "answer": "",
                "results": [
                    {
                        "provider": "tavily",
                        "source": "web",
                        "title": "Responses | OpenAI API Reference",
                        "url": "https://platform.openai.com/docs/api-reference/responses",
                        "snippet": "Official OpenAI docs",
                        "content": "",
                    },
                    {
                        "provider": "tavily",
                        "source": "web",
                        "title": "Community recap",
                        "url": "https://example.com/openai-responses-guide",
                        "snippet": "Third-party article",
                        "content": "",
                    }
                ],
                "citations": [
                    {
                        "title": "Responses | OpenAI API Reference",
                        "url": "https://platform.openai.com/docs/api-reference/responses",
                    },
                    {
                        "title": "Community recap",
                        "url": "https://example.com/openai-responses-guide",
                    }
                ],
            }

        client._search_tavily_once = fake_search_tavily_once  # type: ignore[method-assign]

        result = client._search_tavily(
            query="OpenAI Responses API docs",
            max_results=5,
            topic="general",
            include_answer=False,
            include_content=False,
            include_domains=["openai.com"],
            exclude_domains=None,
        )

        self.assertEqual(
            result["results"][0]["url"],
            "https://platform.openai.com/docs/api-reference/responses",
        )
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["route_debug"]["domain_filter_mode"], "site_query_retry")
        self.assertEqual(result["route_debug"]["retried_include_domains"], ["openai.com"])
        self.assertEqual(calls[1]["query"], "site:openai.com OpenAI Responses API docs")
        self.assertIsNone(calls[1]["include_domains"])

    def test_tavily_domain_filtered_search_falls_back_to_firecrawl(self) -> None:
        client = MySearchClient()
        client.keyring.has_provider = lambda provider: provider == "firecrawl"  # type: ignore[method-assign]
        client._probe_provider_status = lambda provider, key_count: {  # type: ignore[method-assign]
            "status": "ok",
            "error": "",
            "checked_at": "2026-03-20T00:00:00+00:00",
        }
        client._search_tavily_once = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "tavily",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [],
            "citations": [],
        }
        client._search_firecrawl_once = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "firecrawl",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "firecrawl",
                    "source": "web",
                    "title": "Responses | OpenAI API Reference",
                    "url": "https://platform.openai.com/docs/api-reference/responses",
                    "snippet": "Official OpenAI docs",
                    "content": "",
                }
            ],
            "citations": [
                {
                    "title": "Responses | OpenAI API Reference",
                    "url": "https://platform.openai.com/docs/api-reference/responses",
                }
            ],
        }

        result = client._search_tavily(
            query="OpenAI Responses API docs",
            max_results=5,
            topic="general",
            include_answer=False,
            include_content=False,
            include_domains=["openai.com"],
            exclude_domains=None,
        )

        self.assertEqual(result["provider"], "hybrid")
        self.assertEqual(result["route_selected"], "tavily+firecrawl")
        self.assertEqual(result["fallback"]["from"], "tavily")
        self.assertEqual(result["fallback"]["to"], "firecrawl")
        self.assertEqual(len(result["results"]), 1)

    def test_docs_blended_search_reranks_official_results_ahead_of_third_party(self) -> None:
        client = MySearchClient()
        official_url = "https://platform.openai.com/docs/api-reference/responses"
        reddit_url = "https://www.reddit.com/r/OpenAI/comments/example"
        arxiv_url = "https://arxiv.org/abs/2401.00001"

        client._search_tavily = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "tavily",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "tavily",
                    "source": "web",
                    "title": "OpenAI Responses API docs discussion",
                    "url": reddit_url,
                    "snippet": "Reddit thread about the Responses API",
                    "content": "",
                },
                {
                    "provider": "tavily",
                    "source": "web",
                    "title": "Responses | OpenAI API Reference",
                    "url": official_url,
                    "snippet": "Official OpenAI Responses API reference",
                    "content": "",
                },
            ],
            "citations": [
                {"title": "OpenAI Responses API docs discussion", "url": reddit_url},
                {"title": "Responses | OpenAI API Reference", "url": official_url},
            ],
        }
        client._search_firecrawl = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "firecrawl",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "firecrawl",
                    "source": "web",
                    "title": "Attention Is All You Need for OpenAI responses",
                    "url": arxiv_url,
                    "snippet": "Paper result that should not outrank official docs",
                    "content": "",
                },
                {
                    "provider": "firecrawl",
                    "source": "web",
                    "title": "Responses | OpenAI API Reference",
                    "url": official_url,
                    "snippet": "Official OpenAI Responses API reference",
                    "content": "Request and response schema details",
                },
            ],
            "citations": [
                {"title": "Attention Is All You Need for OpenAI responses", "url": arxiv_url},
                {"title": "Responses | OpenAI API Reference", "url": official_url},
            ],
        }

        result = client._search_web_blended(
            query="OpenAI Responses API docs",
            mode="docs",
            intent="resource",
            strategy="balanced",
            decision=RouteDecision(provider="tavily", reason="test", tavily_topic="general"),
            max_results=5,
            include_content=False,
            include_answer=False,
            include_domains=None,
            exclude_domains=None,
        )

        self.assertEqual(result["results"][0]["url"], official_url)
        self.assertEqual(result["citations"][0]["url"], official_url)
        self.assertIn(reddit_url, [item["url"] for item in result["results"][1:]])
        self.assertIn(arxiv_url, [item["url"] for item in result["results"][1:]])

    def test_docs_blended_search_prioritizes_include_domains(self) -> None:
        client = MySearchClient()
        official_url = "https://docs.anthropic.com/en/docs/build-with-claude/prompt-engineering"
        medium_url = "https://medium.com/@writer/anthropic-prompting-notes"
        youtube_url = "https://www.youtube.com/watch?v=anthropic-docs"

        client._search_tavily = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "tavily",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "tavily",
                    "source": "web",
                    "title": "Anthropic prompt engineering notes",
                    "url": medium_url,
                    "snippet": "Third-party write-up",
                    "content": "",
                },
                {
                    "provider": "tavily",
                    "source": "web",
                    "title": "Prompt engineering - Anthropic",
                    "url": official_url,
                    "snippet": "Official Anthropic docs",
                    "content": "",
                },
            ],
            "citations": [
                {"title": "Anthropic prompt engineering notes", "url": medium_url},
                {"title": "Prompt engineering - Anthropic", "url": official_url},
            ],
        }
        client._search_firecrawl = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "firecrawl",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "firecrawl",
                    "source": "web",
                    "title": "Anthropic docs overview video",
                    "url": youtube_url,
                    "snippet": "Third-party video recap",
                    "content": "",
                },
                {
                    "provider": "firecrawl",
                    "source": "web",
                    "title": "Prompt engineering - Anthropic",
                    "url": official_url,
                    "snippet": "Official Anthropic docs",
                    "content": "Official prompt engineering guidance",
                },
            ],
            "citations": [
                {"title": "Anthropic docs overview video", "url": youtube_url},
                {"title": "Prompt engineering - Anthropic", "url": official_url},
            ],
        }

        result = client._search_web_blended(
            query="Anthropic prompt engineering docs",
            mode="docs",
            intent="resource",
            strategy="balanced",
            decision=RouteDecision(provider="tavily", reason="test", tavily_topic="general"),
            max_results=5,
            include_content=False,
            include_answer=False,
            include_domains=["anthropic.com"],
            exclude_domains=None,
        )

        urls = [item["url"] for item in result["results"]]
        first_non_anthropic = next(
            index for index, url in enumerate(urls) if "anthropic.com" not in url
        )
        first_anthropic = next(
            index for index, url in enumerate(urls) if "anthropic.com" in url
        )

        self.assertEqual(result["results"][0]["url"], official_url)
        self.assertEqual(result["citations"][0]["url"], official_url)

    def test_search_route_reason_surfaces_secondary_provider_auth_error(self) -> None:
        client = MySearchClient()
        client.keyring.has_provider = lambda provider: provider in {"tavily", "firecrawl"}  # type: ignore[method-assign]
        client._probe_provider_status = lambda provider, key_count: {  # type: ignore[method-assign]
            "status": "ok",
            "error": "",
            "checked_at": "2026-03-20T00:00:00+00:00",
        }
        client._route_search = lambda **kwargs: RouteDecision(  # type: ignore[method-assign]
            provider="tavily",
            reason="普通网页检索默认走 Tavily",
            tavily_topic="general",
        )
        client._search_tavily = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "tavily",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "tavily",
                    "source": "web",
                    "title": "Official docs",
                    "url": "https://docs.example.com/page",
                    "snippet": "Official docs",
                    "content": "",
                }
            ],
            "citations": [{"title": "Official docs", "url": "https://docs.example.com/page"}],
        }

        def fail_firecrawl(**kwargs):  # type: ignore[no-untyped-def]
            raise MySearchHTTPError(
                provider="firecrawl",
                status_code=401,
                detail="The account associated with this API key has been deactivated.",
                url="https://example.com/search",
            )

        client._search_firecrawl = fail_firecrawl  # type: ignore[method-assign]

        result = client.search(
            query="example search",
            mode="auto",
            strategy="balanced",
            provider="auto",
            include_answer=False,
        )

        self.assertEqual(result["provider"], "tavily")
        self.assertIn("secondary provider issue", result["route"]["reason"])
        self.assertIn("configured but the API key was rejected", result["route"]["reason"])

    def test_docs_route_skips_tavily_when_live_probe_reports_auth_error(self) -> None:
        client = MySearchClient()
        client.keyring.has_provider = lambda provider: provider in {"tavily", "firecrawl"}  # type: ignore[method-assign]
        client._probe_provider_status = lambda provider, key_count: {  # type: ignore[method-assign]
            "status": "auth_error" if provider.name == "tavily" else "ok",
            "error": "tavily rejected" if provider.name == "tavily" else "",
            "checked_at": "2026-03-20T00:00:00+00:00",
        }

        decision = client._route_search(
            query="newapi cache 官方文档",
            mode="docs",
            intent="resource",
            provider="auto",
            sources=["web"],
            include_content=False,
            include_domains=None,
            allowed_x_handles=None,
            excluded_x_handles=None,
        )

        self.assertEqual(decision.provider, "firecrawl")

    def test_web_route_prefers_exa_when_tavily_probe_is_degraded(self) -> None:
        client = MySearchClient()
        client.keyring.has_provider = lambda provider: provider in {"tavily", "firecrawl", "exa"}  # type: ignore[method-assign]

        def fake_probe(provider, key_count):  # type: ignore[no-untyped-def]
            if provider.name == "tavily":
                return {
                    "status": "http_error",
                    "error": "proxy_error",
                    "checked_at": "2026-03-24T00:00:00+00:00",
                }
            return {
                "status": "ok",
                "error": "",
                "checked_at": "2026-03-24T00:00:00+00:00",
            }

        client._probe_provider_status = fake_probe  # type: ignore[method-assign]

        decision = client._route_search(
            query="best model context protocol server 2026",
            mode="web",
            intent="factual",
            provider="auto",
            sources=["web"],
            include_content=False,
            include_domains=None,
            allowed_x_handles=None,
            excluded_x_handles=None,
        )

        self.assertEqual(decision.provider, "exa")
        self.assertEqual(decision.fallback_chain, ["firecrawl", "tavily"])

    def test_blended_search_requires_live_ok_providers(self) -> None:
        client = MySearchClient()
        client.keyring.has_provider = lambda provider: provider in {"tavily", "firecrawl"}  # type: ignore[method-assign]

        def fake_probe(provider, key_count):  # type: ignore[no-untyped-def]
            return {
                "status": "http_error" if provider.name == "tavily" else "ok",
                "error": "proxy_error" if provider.name == "tavily" else "",
                "checked_at": "2026-03-24T00:00:00+00:00",
            }

        client._probe_provider_status = fake_probe  # type: ignore[method-assign]

        self.assertFalse(
            client._should_blend_web_providers(
                query="example query",
                requested_provider="auto",
                decision=RouteDecision(provider="firecrawl", reason="fallback", result_profile="web"),
                sources=["web"],
                strategy="balanced",
                mode="web",
                intent="factual",
                include_domains=None,
            )
        )

    def test_search_reranks_direct_docs_results_to_official_first(self) -> None:
        client = MySearchClient()
        client._search_tavily = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "tavily",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "tavily",
                    "source": "web",
                    "title": "Playwright test.step Guide",
                    "url": "https://www.checklyhq.com/blog/playwright-test-step-guide/",
                    "snippet": "Third-party guide",
                    "content": "",
                },
                {
                    "provider": "tavily",
                    "source": "web",
                    "title": "test.step | Playwright",
                    "url": "https://playwright.dev/docs/api/class-test",
                    "snippet": "Official Playwright docs",
                    "content": "",
                },
            ],
            "citations": [
                {
                    "title": "Playwright test.step Guide",
                    "url": "https://www.checklyhq.com/blog/playwright-test-step-guide/",
                },
                {
                    "title": "test.step | Playwright",
                    "url": "https://playwright.dev/docs/api/class-test",
                },
            ],
        }

        result = client.search(
            query="Playwright test.step docs",
            mode="docs",
            strategy="fast",
            provider="tavily",
            include_answer=False,
        )

        self.assertEqual(result["results"][0]["url"], "https://playwright.dev/docs/api/class-test")
        self.assertEqual(result["citations"][0]["url"], "https://playwright.dev/docs/api/class-test")
        self.assertEqual(result["evidence"]["official_source_count"], 1)
        self.assertEqual(result["evidence"]["confidence"], "high")
        self.assertNotIn("mixed-official-and-third-party", result["evidence"]["conflicts"])

    def test_search_strict_official_mode_filters_to_official_results(self) -> None:
        client = MySearchClient()
        client._search_tavily = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "tavily",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "tavily",
                    "source": "web",
                    "title": "Playwright test.step Guide",
                    "url": "https://www.checklyhq.com/blog/playwright-test-step-guide/",
                    "snippet": "Third-party guide",
                    "content": "",
                },
                {
                    "provider": "tavily",
                    "source": "web",
                    "title": "test.step | Playwright",
                    "url": "https://playwright.dev/docs/api/class-test",
                    "snippet": "Official Playwright docs",
                    "content": "",
                },
            ],
            "citations": [
                {
                    "title": "Playwright test.step Guide",
                    "url": "https://www.checklyhq.com/blog/playwright-test-step-guide/",
                },
                {
                    "title": "test.step | Playwright",
                    "url": "https://playwright.dev/docs/api/class-test",
                },
            ],
        }

        result = client.search(
            query="Playwright test.step official docs",
            mode="docs",
            strategy="fast",
            provider="tavily",
            include_domains=["playwright.dev"],
            include_answer=False,
        )

        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["url"], "https://playwright.dev/docs/api/class-test")
        self.assertEqual(result["evidence"]["official_mode"], "strict")
        self.assertTrue(result["evidence"]["official_filter_applied"])
        self.assertEqual(result["evidence"]["official_source_count"], 1)
        self.assertNotIn("mixed-official-and-third-party", result["evidence"]["conflicts"])

    def test_search_strict_official_mode_keeps_results_but_flags_unmet(self) -> None:
        client = MySearchClient()
        client._search_tavily = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "tavily",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "tavily",
                    "source": "web",
                    "title": "OpenAI API Pricing Guide",
                    "url": "https://apidog.com/blog/openai-api-pricing/",
                    "snippet": "Third-party pricing guide",
                    "content": "",
                },
            ],
            "citations": [
                {
                    "title": "OpenAI API Pricing Guide",
                    "url": "https://apidog.com/blog/openai-api-pricing/",
                },
            ],
        }

        result = client.search(
            query="OpenAI pricing official",
            mode="web",
            strategy="fast",
            provider="tavily",
            include_answer=False,
        )

        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["evidence"]["official_mode"], "strict")
        self.assertFalse(result["evidence"]["official_filter_applied"])
        self.assertIn("strict-official-unmet", result["evidence"]["conflicts"])
        self.assertEqual(result["evidence"]["confidence"], "low")

    def test_search_strict_official_mode_counts_official_hits_for_web_queries(self) -> None:
        client = MySearchClient()
        client._search_tavily = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "tavily",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "tavily",
                    "source": "web",
                    "title": "API Pricing | OpenAI",
                    "url": "https://openai.com/api/pricing/",
                    "snippet": "Official pricing page",
                    "content": "",
                },
                {
                    "provider": "tavily",
                    "source": "web",
                    "title": "OpenAI API Pricing Guide",
                    "url": "https://apidog.com/blog/openai-api-pricing/",
                    "snippet": "Third-party pricing guide",
                    "content": "",
                },
            ],
            "citations": [
                {"title": "API Pricing | OpenAI", "url": "https://openai.com/api/pricing/"},
                {
                    "title": "OpenAI API Pricing Guide",
                    "url": "https://apidog.com/blog/openai-api-pricing/",
                },
            ],
        }

        result = client.search(
            query="OpenAI pricing official",
            mode="web",
            strategy="fast",
            provider="tavily",
            include_answer=False,
        )

        self.assertEqual(result["results"][0]["url"], "https://openai.com/api/pricing/")
        self.assertEqual(result["evidence"]["official_mode"], "strict")
        self.assertEqual(result["evidence"]["official_source_count"], 1)
        self.assertTrue(result["evidence"]["official_filter_applied"])
        self.assertNotIn("strict-official-unmet", result["evidence"]["conflicts"])
        self.assertEqual(
            result["summary"],
            "Top official match: API Pricing | OpenAI (openai.com)",
        )
        self.assertTrue(result["evidence"]["official_filter_reduced"])

    def test_search_strict_official_mode_marks_filter_applied_when_all_results_are_official(self) -> None:
        client = MySearchClient()
        client._search_tavily = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "tavily",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "tavily",
                    "source": "web",
                    "title": "Background mode | OpenAI API",
                    "url": "https://developers.openai.com/api/docs/guides/background/",
                    "snippet": "Official guide",
                    "content": "",
                },
                {
                    "provider": "tavily",
                    "source": "web",
                    "title": "Create a model response | OpenAI API Reference",
                    "url": "https://developers.openai.com/api/reference/resources/responses/methods/create/",
                    "snippet": "Official reference",
                    "content": "",
                },
            ],
            "citations": [
                {"title": "Background mode | OpenAI API", "url": "https://developers.openai.com/api/docs/guides/background/"},
                {"title": "Create a model response | OpenAI API Reference", "url": "https://developers.openai.com/api/reference/resources/responses/methods/create/"},
            ],
        }

        result = client.search(
            query="OpenAI Responses API background mode official docs",
            mode="docs",
            strategy="verify",
            provider="tavily",
            include_answer=False,
        )

        self.assertTrue(result["evidence"]["official_filter_applied"])
        self.assertFalse(result["evidence"]["official_filter_reduced"])

    def test_search_docs_summary_includes_excerpt_for_top_official_match(self) -> None:
        client = MySearchClient()
        client._search_tavily = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "tavily",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "tavily",
                    "source": "web",
                    "title": "useEffectEvent – React 中文文档",
                    "url": "https://zh-hans.react.dev/reference/react/useEffectEvent",
                    "snippet": "useEffectEvent 是一个 React Hook，它让你把事件逻辑从 Effect 中分离出来。",
                    "content": "",
                }
            ],
            "citations": [
                {
                    "title": "useEffectEvent – React 中文文档",
                    "url": "https://zh-hans.react.dev/reference/react/useEffectEvent",
                }
            ],
        }

        result = client.search(
            query="React useEffectEvent 中文文档",
            mode="docs",
            strategy="verify",
            provider="tavily",
            include_answer=False,
        )

        self.assertIn("Top official match: useEffectEvent", result["summary"])
        self.assertIn("React Hook", result["summary"])

    def test_search_strict_official_mode_reranks_locale_variant_below_canonical_page(self) -> None:
        client = MySearchClient()
        client._search_exa = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "exa",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "exa",
                    "source": "web",
                    "title": "Precios de la API - OpenAI",
                    "url": "https://openai.com/es-419/api/pricing/",
                    "snippet": "Localized pricing page",
                    "content": "",
                },
                {
                    "provider": "exa",
                    "source": "web",
                    "title": "API Pricing - OpenAI",
                    "url": "https://openai.com/api/pricing/",
                    "snippet": "Canonical pricing page",
                    "content": "",
                },
            ],
            "citations": [
                {"title": "Precios de la API - OpenAI", "url": "https://openai.com/es-419/api/pricing/"},
                {"title": "API Pricing - OpenAI", "url": "https://openai.com/api/pricing/"},
            ],
        }

        result = client.search(
            query="OpenAI API pricing official",
            mode="web",
            strategy="verify",
            provider="exa",
            include_answer=False,
        )

        self.assertEqual(result["results"][0]["url"], "https://openai.com/api/pricing/")
        self.assertEqual(
            result["summary"],
            "Top official match: API Pricing - OpenAI (openai.com)",
        )

    def test_search_strict_official_mode_prefers_exact_official_docs_page_over_generic_brand_pages(self) -> None:
        client = MySearchClient()
        client._search_tavily = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "tavily",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "tavily",
                    "source": "web",
                    "title": "ChatGPT Pricing | OpenAI",
                    "url": "https://openai.com/business/chatgpt-pricing/",
                    "snippet": "Generic pricing landing page",
                    "content": "",
                },
                {
                    "provider": "tavily",
                    "source": "web",
                    "title": "Webhooks | OpenAI API Reference",
                    "url": "https://platform.openai.com/docs/api-reference/webhooks",
                    "snippet": "Official webhook reference",
                    "content": "",
                },
                {
                    "provider": "tavily",
                    "source": "web",
                    "title": "OpenAI Developer Docs",
                    "url": "https://developers.openai.com/",
                    "snippet": "Generic docs landing",
                    "content": "",
                },
            ],
            "citations": [
                {"title": "ChatGPT Pricing | OpenAI", "url": "https://openai.com/business/chatgpt-pricing/"},
                {"title": "Webhooks | OpenAI API Reference", "url": "https://platform.openai.com/docs/api-reference/webhooks"},
                {"title": "OpenAI Developer Docs", "url": "https://developers.openai.com/"},
            ],
        }

        result = client.search(
            query="OpenAI webhooks official docs",
            mode="web",
            strategy="verify",
            provider="tavily",
            include_answer=False,
        )

        self.assertEqual(result["results"][0]["url"], "https://platform.openai.com/docs/api-reference/webhooks")
        self.assertEqual(result["evidence"]["official_mode"], "strict")

    def test_search_strict_official_mode_prefers_topic_specific_docs_page_over_generic_reference(self) -> None:
        client = MySearchClient()
        client._search_tavily = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "tavily",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "tavily",
                    "source": "web",
                    "title": "Create a model response | OpenAI API Reference",
                    "url": "https://developers.openai.com/api/reference/resources/responses/methods/create/",
                    "snippet": "Generic response create reference",
                    "content": "",
                },
                {
                    "provider": "tavily",
                    "source": "web",
                    "title": "Background mode | OpenAI API",
                    "url": "https://developers.openai.com/api/docs/guides/background/",
                    "snippet": "Specific background mode guide",
                    "content": "",
                },
            ],
            "citations": [
                {"title": "Create a model response | OpenAI API Reference", "url": "https://developers.openai.com/api/reference/resources/responses/methods/create/"},
                {"title": "Background mode | OpenAI API", "url": "https://developers.openai.com/api/docs/guides/background/"},
            ],
        }

        result = client.search(
            query="OpenAI Responses API background mode official docs",
            mode="docs",
            strategy="verify",
            provider="tavily",
            include_answer=False,
        )

        self.assertEqual(
            result["results"][0]["url"],
            "https://developers.openai.com/api/docs/guides/background/",
        )

    def test_web_rerank_prefers_canonical_status_root_over_status_api_endpoint(self) -> None:
        client = MySearchClient()

        ranked = client._rerank_general_results(
            query="Cloudflare status official",
            result_profile="web",
            results=[
                {
                    "provider": "tavily",
                    "title": "API - Cloudflare Status",
                    "url": "https://www.cloudflarestatus.com/api",
                    "snippet": "",
                    "content": "",
                },
                {
                    "provider": "tavily",
                    "title": "Cloudflare Status",
                    "url": "https://www.cloudflarestatus.com/",
                    "snippet": "",
                    "content": "",
                },
            ],
            include_domains=["cloudflarestatus.com"],
        )

        self.assertEqual(ranked[0]["url"], "https://www.cloudflarestatus.com/")

    def test_resource_rerank_prefers_canonical_status_root_over_status_api_endpoint(self) -> None:
        client = MySearchClient()

        ranked = client._rerank_resource_results(
            query="Cloudflare status official",
            mode="docs",
            results=[
                {
                    "provider": "tavily",
                    "title": "API - Cloudflare Status",
                    "url": "https://www.cloudflarestatus.com/api",
                    "snippet": "",
                    "content": "",
                },
                {
                    "provider": "tavily",
                    "title": "Cloudflare Status",
                    "url": "https://www.cloudflarestatus.com/",
                    "snippet": "",
                    "content": "",
                },
            ],
            include_domains=["cloudflarestatus.com"],
        )

        self.assertEqual(ranked[0]["url"], "https://www.cloudflarestatus.com/")

    def test_docs_mode_enters_strict_resource_policy(self) -> None:
        client = MySearchClient()

        mode = client._resolve_official_result_mode(
            query="Next.js generateMetadata",
            mode="docs",
            intent="resource",
            include_domains=None,
        )

        self.assertEqual(mode, "strict")

    def test_pdf_rerank_prefers_exact_named_paper_title_for_single_token_subject(self) -> None:
        client = MySearchClient()

        ranked = client._rerank_resource_results(
            query="HeterMoE pdf",
            mode="pdf",
            results=[
                {
                    "provider": "tavily",
                    "title": "[PDF] Simulating LLM training workloads for heterogeneous compute and memory systems",
                    "url": "https://arxiv.org/abs/2508.05370",
                    "snippet": "",
                    "content": "",
                },
                {
                    "provider": "tavily",
                    "title": "HeterMoE: Efficient Training of Mixture-of-Experts Models on Heterogeneous GPUs - arXiv",
                    "url": "https://arxiv.org/abs/2504.03871",
                    "snippet": "",
                    "content": "",
                },
            ],
            include_domains=["arxiv.org"],
        )

        self.assertEqual(ranked[0]["url"], "https://arxiv.org/abs/2504.03871")

    def test_search_uses_exa_rescue_for_sparse_web_results(self) -> None:
        client = MySearchClient()
        client._provider_can_serve = lambda provider: True  # type: ignore[method-assign]
        client._search_tavily = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "tavily",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "tavily",
                    "source": "web",
                    "title": "Sparse result",
                    "url": "https://example.com/one",
                    "snippet": "Only one result",
                    "content": "",
                }
            ],
            "citations": [
                {"title": "Sparse result", "url": "https://example.com/one"},
            ],
        }
        client._search_exa = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "exa",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "exa",
                    "source": "web",
                    "title": "Long tail reference",
                    "url": "https://exa.example.com/two",
                    "snippet": "Recovered long-tail source",
                    "content": "",
                },
                {
                    "provider": "exa",
                    "source": "web",
                    "title": "Another result",
                    "url": "https://exa.example.com/three",
                    "snippet": "Recovered another source",
                    "content": "",
                },
            ],
            "citations": [
                {"title": "Long tail reference", "url": "https://exa.example.com/two"},
                {"title": "Another result", "url": "https://exa.example.com/three"},
            ],
        }

        result = client.search(
            query="best open source vector database comparison for offline agents",
            mode="web",
            strategy="fast",
            provider="tavily",
            max_results=3,
            include_answer=False,
        )

        self.assertEqual(result["provider"], "hybrid")
        self.assertEqual(result["fallback"]["to"], "exa")
        self.assertEqual(result["results"][0]["url"], "https://exa.example.com/two")

    def test_search_pdf_verify_uses_exa_rescue_for_weak_results(self) -> None:
        client = MySearchClient()
        client._provider_can_serve = lambda provider: provider.name in {"firecrawl", "exa"}  # type: ignore[method-assign]
        client._search_firecrawl = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "firecrawl",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "firecrawl",
                    "source": "web",
                    "title": "DeepSeek-R1 Thoughtology",
                    "url": "https://arxiv.org/pdf/2504.07128",
                    "snippet": "Related paper",
                    "content": "",
                },
                {
                    "provider": "firecrawl",
                    "source": "web",
                    "title": "Another paper",
                    "url": "https://arxiv.org/pdf/2505.12625",
                    "snippet": "Other paper",
                    "content": "",
                },
                {
                    "provider": "firecrawl",
                    "source": "web",
                    "title": "Paper three",
                    "url": "https://arxiv.org/pdf/2503.11486",
                    "snippet": "Third paper",
                    "content": "",
                },
            ],
            "citations": [
                {"title": "DeepSeek-R1 Thoughtology", "url": "https://arxiv.org/pdf/2504.07128"},
                {"title": "Another paper", "url": "https://arxiv.org/pdf/2505.12625"},
                {"title": "Paper three", "url": "https://arxiv.org/pdf/2503.11486"},
            ],
        }
        client._search_exa = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "exa",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "exa",
                    "source": "web",
                    "title": "DeepSeek-R1: Incentivizing Reasoning Capability in LLMs via Reinforcement Learning",
                    "url": "https://arxiv.org/abs/2501.12948",
                    "snippet": "Exact paper page",
                    "content": "",
                }
            ],
            "citations": [
                {
                    "title": "DeepSeek-R1: Incentivizing Reasoning Capability in LLMs via Reinforcement Learning",
                    "url": "https://arxiv.org/abs/2501.12948",
                }
            ],
        }

        result = client.search(
            query="DeepSeek R1 paper pdf",
            mode="pdf",
            strategy="verify",
            max_results=3,
            provider="auto",
            include_domains=["arxiv.org"],
            include_answer=False,
        )

        self.assertEqual(result["provider"], "hybrid")
        self.assertEqual(result["fallback"]["to"], "exa")
        self.assertEqual(result["results"][0]["url"], "https://arxiv.org/abs/2501.12948")

    def test_rerank_resource_results_prefers_exact_versioned_pdf_paper(self) -> None:
        client = MySearchClient()

        reranked = client._rerank_resource_results(
            query="Gemma 3 technical report pdf",
            mode="pdf",
            include_domains=["arxiv.org"],
            results=[
                {
                    "provider": "firecrawl",
                    "title": "Gemma: Open Models Based on Gemini Research and Technology",
                    "url": "https://arxiv.org/abs/2403.08295",
                    "snippet": "Older Gemma paper",
                    "content": "",
                },
                {
                    "provider": "firecrawl",
                    "title": "Gemma 3 Technical Report",
                    "url": "https://arxiv.org/abs/2503.19786",
                    "snippet": "Exact Gemma 3 paper",
                    "content": "",
                },
            ],
        )

        self.assertEqual(reranked[0]["url"], "https://arxiv.org/abs/2503.19786")

    def test_rerank_general_news_prefers_mainstream_article_shape(self) -> None:
        client = MySearchClient()

        reranked = client._rerank_general_results(
            query="2026 oscar winners",
            result_profile="news",
            include_domains=None,
            results=[
                {
                    "provider": "tavily",
                    "title": "Oscars 2026 winners list",
                    "url": "https://news-aggregate.example.com/oscars-winners",
                    "snippet": "aggregated summary",
                    "content": "",
                },
                {
                    "provider": "tavily",
                    "title": "Oscars 2026 winners list",
                    "url": "https://www.latimes.com/entertainment-arts/awards/story/2026-03-15/oscars-2026-winners-list-full-results",
                    "snippet": "Los Angeles Times coverage",
                    "content": "",
                    "published_date": "2026-03-15T09:00:00+00:00",
                },
            ],
        )

        self.assertEqual(
            reranked[0]["url"],
            "https://www.latimes.com/entertainment-arts/awards/story/2026-03-15/oscars-2026-winners-list-full-results",
        )

    def test_rerank_general_web_prefers_exact_official_pricing_page(self) -> None:
        client = MySearchClient()

        reranked = client._rerank_general_results(
            query="OpenAI pricing official",
            result_profile="web",
            include_domains=["openai.com"],
            results=[
                {
                    "provider": "exa",
                    "title": "Confused about OpenAI pricing",
                    "url": "https://community.openai.com/t/confused-about-openai-batch-api-gpt-4o-mini-pricing-why-are-the-total-costs-higher/936262",
                    "snippet": "Official community discussion about pricing",
                    "content": "",
                },
                {
                    "provider": "tavily",
                    "title": "Compare OpenAI API models",
                    "url": "https://developers.openai.com/api/docs/models/compare",
                    "snippet": "Compare model capabilities",
                    "content": "",
                },
                {
                    "provider": "tavily",
                    "title": "API Pricing | OpenAI",
                    "url": "https://openai.com/api/pricing/",
                    "snippet": "Official pricing page",
                    "content": "",
                },
            ],
        )

        self.assertEqual(reranked[0]["url"], "https://openai.com/api/pricing/")

    def test_rerank_general_web_prefers_canonical_buy_page_over_sku_detail(self) -> None:
        client = MySearchClient()

        reranked = client._rerank_general_results(
            query="MacBook Air M5 国行价格 官方",
            result_profile="web",
            include_domains=["apple.com.cn"],
            results=[
                {
                    "provider": "exa",
                    "title": "13 英寸 MacBook Air - 银色",
                    "url": "https://www.apple.com.cn/shop/buy-mac/macbook-air/13-%E8%8B%B1%E5%AF%B8-m5-%E8%8A%AF%E7%89%87-8-%E6%A0%B8-gpu-16gb-%E7%BB%9F%E4%B8%80%E5%86%85%E5%AD%98-256gb-ssd-%E5%AD%98%E5%82%A8%E9%93%B6%E8%89%B2",
                    "snippet": "SKU detail page",
                    "content": "",
                },
                {
                    "provider": "exa",
                    "title": "购买 MacBook Air",
                    "url": "https://www.apple.com.cn/shop/buy-mac/macbook-air",
                    "snippet": "Canonical buy page",
                    "content": "",
                },
            ],
        )

        self.assertEqual(reranked[0]["url"], "https://www.apple.com.cn/shop/buy-mac/macbook-air")

    def test_official_policy_prefers_canonical_buy_page_over_specific_sku(self) -> None:
        client = MySearchClient()

        result = client._apply_official_resource_policy(
            query="MacBook Air M5 国行价格 官方",
            mode="web",
            intent="factual",
            include_domains=["apple.com.cn"],
            result={
                "results": [
                    {
                        "provider": "exa",
                        "title": "购买MacBook Air 15 英寸(M5) - 午夜色- 24GB/4TB - Apple (中国大陆)",
                        "url": "https://www.apple.com.cn/shop/buy-mac/macbook-air/15-inch-midnight-m5-chip-10-core-cpu-10-core-gpu-24gb-memory-4tb-storage",
                        "snippet": "",
                        "content": "",
                    },
                    {
                        "provider": "exa",
                        "title": "13 英寸和15 英寸MacBook Air - Apple (中国大陆)",
                        "url": "https://www.apple.com.cn/macbook-air/",
                        "snippet": "",
                        "content": "",
                    },
                    {
                        "provider": "exa",
                        "title": "购买 MacBook Air",
                        "url": "https://www.apple.com.cn/shop/buy-mac/macbook-air",
                        "snippet": "",
                        "content": "",
                    },
                ],
                "citations": [
                    {
                        "title": "购买MacBook Air 15 英寸(M5) - 午夜色- 24GB/4TB - Apple (中国大陆)",
                        "url": "https://www.apple.com.cn/shop/buy-mac/macbook-air/15-inch-midnight-m5-chip-10-core-cpu-10-core-gpu-24gb-memory-4tb-storage",
                    },
                    {
                        "title": "13 英寸和15 英寸MacBook Air - Apple (中国大陆)",
                        "url": "https://www.apple.com.cn/macbook-air/",
                    },
                    {
                        "title": "购买 MacBook Air",
                        "url": "https://www.apple.com.cn/shop/buy-mac/macbook-air",
                    },
                ],
                "evidence": {},
            },
        )

        self.assertEqual(result["results"][0]["url"], "https://www.apple.com.cn/shop/buy-mac/macbook-air")
        self.assertEqual(result["citations"][0]["url"], "https://www.apple.com.cn/shop/buy-mac/macbook-air")

    def test_official_policy_final_rerank_prefers_specific_release_page_over_indexes(self) -> None:
        client = MySearchClient()

        result = client._apply_official_resource_policy(
            query="Next.js 16 release notes official",
            mode="docs",
            intent="resource",
            include_domains=["nextjs.org"],
            result={
                "results": [
                    {
                        "provider": "tavily",
                        "title": "The latest Next.js news",
                        "url": "https://nextjs.org/blog",
                        "snippet": "Generic blog index",
                        "content": "",
                    },
                    {
                        "provider": "tavily",
                        "title": "Guides: Next.js Docs",
                        "url": "https://nextjs.org/docs",
                        "snippet": "Generic docs index",
                        "content": "",
                    },
                    {
                        "provider": "tavily",
                        "title": "Next.js 16",
                        "url": "https://nextjs.org/blog/next-16",
                        "snippet": "Official release announcement",
                        "content": "",
                    },
                    {
                        "provider": "tavily",
                        "title": "Upgrading: Version 16",
                        "url": "https://nextjs.org/docs/app/guides/upgrading/version-16",
                        "snippet": "Migration guide",
                        "content": "",
                    },
                ],
                "citations": [
                    {"title": "The latest Next.js news", "url": "https://nextjs.org/blog"},
                    {"title": "Guides: Next.js Docs", "url": "https://nextjs.org/docs"},
                    {"title": "Next.js 16", "url": "https://nextjs.org/blog/next-16"},
                    {
                        "title": "Upgrading: Version 16",
                        "url": "https://nextjs.org/docs/app/guides/upgrading/version-16",
                    },
                ],
                "evidence": {},
            },
        )

        reranked = client._rerank_resource_results(
            query="Next.js 16 release notes official",
            mode="docs",
            results=result["results"],
            include_domains=["nextjs.org"],
        )

        self.assertEqual(reranked[0]["url"], "https://nextjs.org/blog/next-16")

    def test_official_policy_for_status_queries_uses_general_rerank(self) -> None:
        client = MySearchClient()

        result = client._apply_official_resource_policy(
            query="OpenAI background mode latest status",
            mode="news",
            intent="status",
            include_domains=["openai.com"],
            result={
                "results": [
                    {
                        "provider": "tavily",
                        "title": "Background mode requests stuck in queued status - OpenAI Developer Community",
                        "url": "https://community.openai.com/t/background-mode-requests-stuck-in-queued-status-responses-api/1372058",
                        "snippet": "",
                        "content": "",
                    },
                    {
                        "provider": "tavily",
                        "title": "Background mode guide | OpenAI",
                        "url": "https://developers.openai.com/api/docs/guides/background/",
                        "snippet": "",
                        "content": "",
                    },
                    {
                        "provider": "tavily",
                        "title": "Responses API errors when using background mode",
                        "url": "https://status.openai.com/incidents/01KKMB9HWS1B9452FT6BV6KDD6",
                        "snippet": "",
                        "content": "",
                    },
                ],
                "citations": [
                    {
                        "title": "Background mode requests stuck in queued status - OpenAI Developer Community",
                        "url": "https://community.openai.com/t/background-mode-requests-stuck-in-queued-status-responses-api/1372058",
                    },
                    {
                        "title": "Background mode guide | OpenAI",
                        "url": "https://developers.openai.com/api/docs/guides/background/",
                    },
                    {
                        "title": "Responses API errors when using background mode",
                        "url": "https://status.openai.com/incidents/01KKMB9HWS1B9452FT6BV6KDD6",
                    },
                ],
                "evidence": {},
            },
        )

        urls = [item["url"] for item in result["results"]]
        self.assertEqual(urls[0], "https://status.openai.com/incidents/01KKMB9HWS1B9452FT6BV6KDD6")
        self.assertLess(
            urls.index("https://developers.openai.com/api/docs/guides/background/"),
            urls.index("https://community.openai.com/t/background-mode-requests-stuck-in-queued-status-responses-api/1372058"),
        )

    def test_changelog_weak_results_trigger_exa_rescue_signal(self) -> None:
        client = MySearchClient()

        weak = client._result_set_looks_weak_for_exa_rescue(
            query="Next.js 16 release notes official",
            mode="docs",
            result={
                "results": [
                    {
                        "provider": "tavily",
                        "title": "Renaming Middleware to Proxy - Next.js",
                        "url": "https://nextjs.org/docs/messages/middleware-to-proxy",
                        "snippet": "",
                        "content": "",
                    },
                    {
                        "provider": "tavily",
                        "title": "Next.js Blog",
                        "url": "https://nextjs.org/blog",
                        "snippet": "",
                        "content": "",
                    },
                ]
            },
        )

        self.assertTrue(weak)

    def test_strict_changelog_query_allows_exa_rescue_for_weak_results(self) -> None:
        client = MySearchClient()
        client._provider_can_serve = lambda provider: provider.name == "exa"  # type: ignore[method-assign]

        should_rescue = client._should_attempt_exa_rescue(
            query="Next.js 16 release notes official",
            mode="docs",
            intent="resource",
            decision=RouteDecision(
                provider="tavily",
                reason="test",
                result_profile="resource",
                allow_exa_rescue=True,
            ),
            result={
                "provider": "tavily",
                "results": [
                    {
                        "title": "Renaming Middleware to Proxy - Next.js",
                        "url": "https://nextjs.org/docs/messages/middleware-to-proxy",
                        "snippet": "",
                        "content": "",
                    },
                    {
                        "title": "Next.js Blog",
                        "url": "https://nextjs.org/blog",
                        "snippet": "",
                        "content": "",
                    },
                ]
            },
            max_results=5,
            include_domains=["nextjs.org"],
        )

        self.assertTrue(should_rescue)

    def test_tutorial_results_without_brand_aligned_or_issue_sources_trigger_exa_rescue(self) -> None:
        client = MySearchClient()

        weak = client._result_set_looks_weak_for_exa_rescue(
            query="Playwright test.step tutorial example",
            mode="docs",
            result={
                "results": [
                    {
                        "provider": "tavily",
                        "title": "Playwright step - Loadmill - AI",
                        "url": "https://docs.loadmill.com/test-editor/steps/playwright-step",
                        "snippet": "",
                        "content": "",
                    },
                    {
                        "provider": "tavily",
                        "title": "Keep your Playwright tests structured with steps - Tim Deschryver",
                        "url": "https://timdeschryver.dev/blog/keep-your-playwright-tests-structured-with-steps",
                        "snippet": "",
                        "content": "",
                    },
                ]
            },
        )

        self.assertTrue(weak)

    def test_rerank_general_web_prefers_status_page_for_status_queries(self) -> None:
        client = MySearchClient()

        reranked = client._rerank_general_results(
            query="OpenAI latest status update",
            result_profile="web",
            include_domains=None,
            results=[
                {
                    "provider": "tavily",
                    "title": "Responses API errors when using background mode - IsDown",
                    "url": "https://isdown.app/status/openai/incidents/554117-responses-api-errors-when-using-background-mode",
                    "snippet": "Aggregator incident page",
                    "content": "",
                },
                {
                    "provider": "tavily",
                    "title": "Responses API errors when using background mode - OpenAI Status",
                    "url": "https://status.openai.com/incidents/abc123",
                    "snippet": "Incident details",
                    "content": "",
                },
            ],
        )

        self.assertEqual(reranked[0]["url"], "https://status.openai.com/incidents/abc123")

    def test_rerank_general_web_accepts_brand_status_domain_root_as_canonical_status(self) -> None:
        client = MySearchClient()

        reranked = client._rerank_general_results(
            query="Cloudflare status official",
            result_profile="web",
            include_domains=["cloudflare.com"],
            results=[
                {
                    "provider": "tavily",
                    "title": "Cloudflare status",
                    "url": "https://www.cloudflarestatus.com/",
                    "snippet": "Cloudflare system status",
                    "content": "",
                },
                {
                    "provider": "tavily",
                    "title": "Cloudflare Documentation",
                    "url": "https://developers.cloudflare.com/support/troubleshooting/cloudflare-errors/",
                    "snippet": "Support docs",
                    "content": "",
                },
            ],
        )

        self.assertEqual(reranked[0]["url"], "https://www.cloudflarestatus.com/")

    def test_rerank_general_web_prefers_local_guide_over_repost_for_life_queries(self) -> None:
        client = MySearchClient()

        reranked = client._rerank_general_results(
            query="上海 2026 春季赏花攻略",
            result_profile="web",
            include_domains=None,
            results=[
                {
                    "provider": "tavily",
                    "title": "2026上海春日赏花攻略，看这一篇就够了！ - 网易",
                    "url": "https://www.163.com/dy/article/KNM9TT6205179EUD.html",
                    "snippet": "转载型赏花文章",
                    "content": "",
                },
                {
                    "provider": "tavily",
                    "title": "上海赏花攻略",
                    "url": "https://m.sh.bendibao.com/tour/flowers?month=3%E6%9C%88",
                    "snippet": "本地生活导览页",
                    "content": "",
                },
            ],
        )

        self.assertEqual(reranked[0]["url"], "https://m.sh.bendibao.com/tour/flowers?month=3%E6%9C%88")

    def test_rerank_general_web_prefers_brand_aligned_tutorial_docs_over_generic_blogs(self) -> None:
        client = MySearchClient()

        reranked = client._rerank_general_results(
            query="Playwright test.step tutorial example",
            result_profile="web",
            include_domains=None,
            results=[
                {
                    "provider": "tavily",
                    "title": "Improve Your Playwright Documentation with Test Steps - Checkly",
                    "url": "https://www.checklyhq.com/blog/improve-your-playwright-documentation-with-steps/",
                    "snippet": "Third-party tutorial article",
                    "content": "",
                },
                {
                    "provider": "tavily",
                    "title": "Running and debugging tests | Playwright",
                    "url": "https://playwright.dev/docs/running-tests",
                    "snippet": "Official debugging guide",
                    "content": "",
                },
            ],
        )

        self.assertEqual(reranked[0]["url"], "https://playwright.dev/docs/running-tests")

    def test_rerank_general_web_prefers_debug_issue_sources_for_debugging_queries(self) -> None:
        client = MySearchClient()

        reranked = client._rerank_general_results(
            query="Playwright strict mode violation fix",
            result_profile="web",
            include_domains=None,
            results=[
                {
                    "provider": "tavily",
                    "title": "Writing tests | Playwright",
                    "url": "https://playwright.dev/docs/writing-tests",
                    "snippet": "Generic official docs",
                    "content": "",
                },
                {
                    "provider": "tavily",
                    "title": "strict mode violation when locator resolves to two elements",
                    "url": "https://github.com/microsoft/playwright/issues/30069",
                    "snippet": "Debugging issue thread with workaround",
                    "content": "",
                },
            ],
        )

        self.assertEqual(reranked[0]["url"], "https://github.com/microsoft/playwright/issues/30069")

    def test_rerank_general_web_prefers_canonical_local_life_guide_over_generic_local_page(self) -> None:
        client = MySearchClient()

        reranked = client._rerank_general_results(
            query="上海 2026 春季赏花攻略",
            result_profile="web",
            include_domains=None,
            results=[
                {
                    "provider": "tavily",
                    "title": "上海休闲攻略",
                    "url": "https://m.sh.bendibao.com/xiuxian/304455.html",
                    "snippet": "泛生活频道文章",
                    "content": "",
                },
                {
                    "provider": "tavily",
                    "title": "上海赏花攻略",
                    "url": "https://m.sh.bendibao.com/tour/flowers?month=3%E6%9C%88",
                    "snippet": "本地赏花专题页",
                    "content": "",
                },
            ],
        )

        self.assertEqual(reranked[0]["url"], "https://m.sh.bendibao.com/tour/flowers?month=3%E6%9C%88")

    def test_rerank_general_web_demotes_official_community_threads_for_status_queries(self) -> None:
        client = MySearchClient()

        reranked = client._rerank_general_results(
            query="OpenAI background mode latest status",
            result_profile="web",
            include_domains=None,
            results=[
                {
                    "provider": "tavily",
                    "title": "Background mode requests stuck in queued status - OpenAI Developer Community",
                    "url": "https://community.openai.com/t/background-mode-requests-stuck-in-queued-status-responses-api/1267382",
                    "snippet": "Official community thread",
                    "content": "",
                },
                {
                    "provider": "tavily",
                    "title": "Background mode guide | OpenAI",
                    "url": "https://developers.openai.com/api/docs/guides/background/",
                    "snippet": "Official developer guide",
                    "content": "",
                },
                {
                    "provider": "tavily",
                    "title": "Responses API errors when using background mode",
                    "url": "https://status.openai.com/incidents/abc123",
                    "snippet": "OpenAI status incident",
                    "content": "",
                },
            ],
        )

        urls = [item["url"] for item in reranked]
        self.assertEqual(urls[0], "https://status.openai.com/incidents/abc123")
        self.assertLess(
            urls.index("https://developers.openai.com/api/docs/guides/background/"),
            urls.index("https://community.openai.com/t/background-mode-requests-stuck-in-queued-status-responses-api/1267382"),
        )

    def test_rerank_resource_results_prefers_exact_query_token_page(self) -> None:
        client = MySearchClient()

        reranked = client._rerank_resource_results(
            query="Playwright test.step docs",
            mode="docs",
            include_domains=None,
            results=[
                {
                    "provider": "tavily",
                    "title": "Test class | Playwright",
                    "url": "https://playwright.dev/docs/api/class-test",
                    "snippet": "General test API reference",
                    "content": "",
                },
                {
                    "provider": "exa",
                    "title": "test.step | Playwright",
                    "url": "https://playwright.dev/docs/api/class-teststep",
                    "snippet": "Exact test.step API reference",
                    "content": "",
                },
                {
                    "provider": "tavily",
                    "title": "TestStepInfo class | Playwright",
                    "url": "https://playwright.dev/docs/api/class-teststepinfo",
                    "snippet": "test.step related API",
                    "content": "",
                },
            ],
        )

        self.assertEqual(reranked[0]["url"], "https://playwright.dev/docs/api/class-teststep")

    def test_rerank_resource_results_prefers_non_locale_official_variant(self) -> None:
        client = MySearchClient()

        reranked = client._rerank_resource_results(
            query="OpenAI API pricing official",
            mode="web",
            include_domains=None,
            results=[
                {
                    "provider": "exa",
                    "title": "Precios de la API - OpenAI",
                    "url": "https://openai.com/es-419/api/pricing/",
                    "snippet": "Localized pricing page",
                    "content": "",
                },
                {
                    "provider": "exa",
                    "title": "API Pricing - OpenAI",
                    "url": "https://openai.com/api/pricing/",
                    "snippet": "Canonical pricing page",
                    "content": "",
                },
                {
                    "provider": "exa",
                    "title": "Pricing | OpenAI API",
                    "url": "https://developers.openai.com/api/docs/pricing/",
                    "snippet": "Docs pricing reference",
                    "content": "",
                },
            ],
        )

        self.assertEqual(reranked[0]["url"], "https://openai.com/api/pricing/")

    def test_rerank_resource_results_demotes_official_community_pages_for_strict_web_queries(self) -> None:
        client = MySearchClient()

        reranked = client._rerank_resource_results(
            query="OpenAI API pricing official",
            mode="web",
            include_domains=["openai.com"],
            results=[
                {
                    "provider": "exa",
                    "title": "Confused about OpenAI pricing",
                    "url": "https://community.openai.com/t/confused-about-openai-pricing/12345",
                    "snippet": "Official community discussion about pricing",
                    "content": "",
                },
                {
                    "provider": "exa",
                    "title": "API Pricing - OpenAI",
                    "url": "https://openai.com/api/pricing/",
                    "snippet": "Canonical pricing page",
                    "content": "",
                },
                {
                    "provider": "exa",
                    "title": "API Pricing Guide - OpenAI Developers",
                    "url": "https://developers.openai.com/api/docs/pricing/",
                    "snippet": "Developer pricing reference",
                    "content": "",
                },
            ],
        )

        self.assertEqual(reranked[0]["url"], "https://openai.com/api/pricing/")
        self.assertGreater(
            [item["url"] for item in reranked].index("https://community.openai.com/t/confused-about-openai-pricing/12345"),
            0,
        )

    def test_rerank_resource_results_prefers_release_blog_for_changelog_query(self) -> None:
        client = MySearchClient()

        reranked = client._rerank_resource_results(
            query="Next.js 16 release notes official",
            mode="docs",
            include_domains=["nextjs.org"],
            results=[
                {
                    "provider": "tavily",
                    "title": "Guides: Next.js MCP Server",
                    "url": "https://nextjs.org/docs/app/guides/mcp",
                    "snippet": "Generic MCP guide",
                    "content": "",
                },
                {
                    "provider": "tavily",
                    "title": "Next.js 16",
                    "url": "https://nextjs.org/blog/next-16",
                    "snippet": "Official release announcement",
                    "content": "",
                },
                {
                    "provider": "tavily",
                    "title": "Upgrading: Version 16",
                    "url": "https://nextjs.org/docs/app/guides/upgrading/version-16",
                    "snippet": "Migration guide",
                    "content": "",
                },
            ],
        )

        self.assertEqual(reranked[0]["url"], "https://nextjs.org/blog/next-16")

    def test_rerank_resource_results_demotes_generic_blog_index_for_changelog_query(self) -> None:
        client = MySearchClient()

        reranked = client._rerank_resource_results(
            query="Next.js 16 release notes official",
            mode="docs",
            include_domains=["nextjs.org"],
            results=[
                {
                    "provider": "tavily",
                    "title": "The latest Next.js news",
                    "url": "https://nextjs.org/blog",
                    "snippet": "Next.js 16 is now available with major improvements.",
                    "content": "",
                },
                {
                    "provider": "tavily",
                    "title": "Next.js 16",
                    "url": "https://nextjs.org/blog/next-16",
                    "snippet": "Official release announcement",
                    "content": "",
                },
                {
                    "provider": "tavily",
                    "title": "Upgrading: Version 16",
                    "url": "https://nextjs.org/docs/app/guides/upgrading/version-16",
                    "snippet": "Migration guide",
                    "content": "",
                },
            ],
        )

        urls = [item["url"] for item in reranked]
        self.assertEqual(urls[0], "https://nextjs.org/blog/next-16")
        self.assertGreater(urls.index("https://nextjs.org/blog"), 0)

    def test_pdf_verify_blend_promotes_exact_paper_page(self) -> None:
        client = MySearchClient()
        client._provider_can_serve = lambda provider: provider.name in {"tavily", "firecrawl"}  # type: ignore[method-assign]
        client._search_firecrawl = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "firecrawl",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "firecrawl",
                    "source": "web",
                    "title": "DeepSeek-R1 Thoughtology",
                    "url": "https://arxiv.org/pdf/2504.07128",
                    "snippet": "Related paper",
                    "content": "",
                }
            ],
            "citations": [{"title": "DeepSeek-R1 Thoughtology", "url": "https://arxiv.org/pdf/2504.07128"}],
        }
        client._search_tavily = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "tavily",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "tavily",
                    "source": "web",
                    "title": "DeepSeek-R1: Incentivizing Reasoning Capability in LLMs via Reinforcement Learning",
                    "url": "https://arxiv.org/abs/2501.12948",
                    "snippet": "The exact paper page",
                    "content": "",
                }
            ],
            "citations": [
                {
                    "title": "DeepSeek-R1: Incentivizing Reasoning Capability in LLMs via Reinforcement Learning",
                    "url": "https://arxiv.org/abs/2501.12948",
                }
            ],
        }

        result = client._search_web_blended(
            query="DeepSeek R1 paper pdf",
            mode="pdf",
            intent="resource",
            strategy="verify",
            decision=RouteDecision(
                provider="firecrawl",
                reason="pdf primary",
                firecrawl_categories=("pdf",),
                result_profile="resource",
            ),
            max_results=4,
            include_content=False,
            include_answer=False,
            include_domains=None,
            exclude_domains=None,
        )

        self.assertEqual(result["results"][0]["url"], "https://arxiv.org/abs/2501.12948")

    def test_pdf_verify_blend_falls_back_to_exa_when_primary_and_secondary_fail(self) -> None:
        client = MySearchClient()
        client._provider_can_serve = lambda provider: provider.name in {"tavily", "firecrawl", "exa"}  # type: ignore[method-assign]
        client._search_firecrawl = lambda **kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
            MySearchError("firecrawl quota exhausted")
        )
        client._search_tavily = lambda **kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
            MySearchError("tavily upstream failed")
        )
        client._search_exa = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "exa",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "exa",
                    "source": "web",
                    "title": "DeepSeek-R1: Incentivizing Reasoning Capability in LLMs via Reinforcement Learning",
                    "url": "https://arxiv.org/abs/2501.12948",
                    "snippet": "Exact paper page",
                    "content": "",
                }
            ],
            "citations": [
                {
                    "title": "DeepSeek-R1: Incentivizing Reasoning Capability in LLMs via Reinforcement Learning",
                    "url": "https://arxiv.org/abs/2501.12948",
                }
            ],
            "evidence": {
                "providers_consulted": ["exa"],
                "verification": "single-provider",
            },
        }

        result = client._search_web_blended(
            query="DeepSeek R1 paper pdf",
            mode="pdf",
            intent="resource",
            strategy="verify",
            decision=RouteDecision(
                provider="firecrawl",
                reason="pdf primary",
                firecrawl_categories=("pdf",),
                result_profile="resource",
                allow_exa_rescue=True,
            ),
            max_results=4,
            include_content=False,
            include_answer=False,
            include_domains=["arxiv.org"],
            exclude_domains=None,
        )

        self.assertEqual(result["provider"], "exa")
        self.assertEqual(result["fallback"]["to"], "exa")
        self.assertEqual(result["results"][0]["url"], "https://arxiv.org/abs/2501.12948")

    def test_pdf_verify_uses_exa_boost_for_weak_firecrawl_match(self) -> None:
        client = MySearchClient()
        client._provider_can_serve = lambda provider: provider.name in {"firecrawl", "exa"}  # type: ignore[method-assign]
        client._search_firecrawl = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "firecrawl",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "firecrawl",
                    "source": "web",
                    "title": "Investigating Local Censorship in DeepSeek's R1 ...",
                    "url": "https://arxiv.org/abs/2505.12625",
                    "snippet": "Related but not the primary paper",
                    "content": "Related but not the primary paper" if kwargs.get("include_content") else "",
                }
            ],
            "citations": [
                {
                    "title": "Investigating Local Censorship in DeepSeek's R1 ...",
                    "url": "https://arxiv.org/abs/2505.12625",
                }
            ],
        }
        client._search_exa = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "exa",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "exa",
                    "source": "web",
                    "title": "DeepSeek-R1: Incentivizing Reasoning Capability in LLMs via Reinforcement Learning",
                    "url": "https://arxiv.org/abs/2501.12948",
                    "snippet": "The exact paper page",
                    "content": "",
                }
            ],
            "citations": [
                {
                    "title": "DeepSeek-R1: Incentivizing Reasoning Capability in LLMs via Reinforcement Learning",
                    "url": "https://arxiv.org/abs/2501.12948",
                }
            ],
        }

        result = client.search(
            query="DeepSeek R1 paper pdf",
            mode="pdf",
            strategy="verify",
            provider="auto",
            include_answer=False,
            include_content=True,
            include_domains=["arxiv.org"],
            max_results=5,
        )

        self.assertTrue(result["evidence"]["pdf_exa_boost"])
        self.assertEqual(result["results"][0]["url"], "https://arxiv.org/abs/2501.12948")

    def test_pdf_verify_uses_tavily_boost_when_exa_still_lacks_exact_paper_title(self) -> None:
        client = MySearchClient()
        client._provider_can_serve = lambda provider: provider.name in {"firecrawl", "exa", "tavily"}  # type: ignore[method-assign]
        client._search_firecrawl = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "firecrawl",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "firecrawl",
                    "source": "web",
                    "title": "Insights into DeepSeek-V3: Scaling Challenges and Reflections on Hardware for AI Architectures",
                    "url": "https://arxiv.org/abs/2505.09343",
                    "snippet": "Wrong but related paper",
                    "content": "Wrong but related paper" if kwargs.get("include_content") else "",
                }
            ],
            "citations": [
                {
                    "title": "Insights into DeepSeek-V3: Scaling Challenges and Reflections on Hardware for AI Architectures",
                    "url": "https://arxiv.org/abs/2505.09343",
                }
            ],
        }
        client._search_exa = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "exa",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "exa",
                    "source": "web",
                    "title": "Computer Science > Computation and Language",
                    "url": "https://arxiv.org/abs/2501.12948",
                    "snippet": "",
                    "content": "",
                }
            ],
            "citations": [
                {"title": "Computer Science > Computation and Language", "url": "https://arxiv.org/abs/2501.12948"},
            ],
        }
        client._search_tavily = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "tavily",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "tavily",
                    "source": "web",
                    "title": "DeepSeek-R1: Incentivizing Reasoning Capability in LLMs via Reinforcement Learning",
                    "url": "https://arxiv.org/html/2501.12948v1",
                    "snippet": "Exact paper page",
                    "content": "",
                }
            ],
            "citations": [
                {
                    "title": "DeepSeek-R1: Incentivizing Reasoning Capability in LLMs via Reinforcement Learning",
                    "url": "https://arxiv.org/html/2501.12948v1",
                }
            ],
        }

        result = client.search(
            query="DeepSeek R1 paper pdf",
            mode="pdf",
            strategy="verify",
            provider="auto",
            include_answer=False,
            include_content=True,
            include_domains=["arxiv.org"],
            max_results=5,
        )

        self.assertTrue(result["evidence"]["pdf_exa_boost"])
        self.assertTrue(result["evidence"]["pdf_tavily_boost"])
        self.assertEqual(result["results"][0]["url"], "https://arxiv.org/abs/2501.12948")
        self.assertIn("DeepSeek-R1", result["summary"])

    def test_pdf_title_enrichment_promotes_generic_arxiv_entry(self) -> None:
        client = MySearchClient()
        client._provider_can_serve = lambda provider: provider.name == "exa"  # type: ignore[method-assign]
        client._search_exa = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "exa",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "exa",
                    "source": "web",
                    "title": "Insights into DeepSeek-V3: Scaling Challenges and Reflections on Hardware for AI Architectures",
                    "url": "https://arxiv.org/abs/2505.09343",
                    "snippet": "Wrong but related paper",
                    "content": "",
                },
                {
                    "provider": "exa",
                    "source": "web",
                    "title": "Computer Science > Computation and Language",
                    "url": "https://arxiv.org/abs/2501.12948",
                    "snippet": "",
                    "content": "",
                },
            ],
            "citations": [
                {
                    "title": "Insights into DeepSeek-V3: Scaling Challenges and Reflections on Hardware for AI Architectures",
                    "url": "https://arxiv.org/abs/2505.09343",
                },
                {
                    "title": "Computer Science > Computation and Language",
                    "url": "https://arxiv.org/abs/2501.12948",
                },
            ],
        }
        client._fetch_arxiv_title = lambda url: (  # type: ignore[method-assign]
            "DeepSeek-R1: Incentivizing Reasoning Capability in LLMs via Reinforcement Learning"
            if "2501.12948" in url
            else ""
        )

        result = client.search(
            query="DeepSeek R1 paper pdf",
            mode="pdf",
            strategy="verify",
            provider="exa",
            include_answer=False,
            include_domains=["arxiv.org"],
            max_results=5,
        )

        self.assertEqual(result["results"][0]["url"], "https://arxiv.org/abs/2501.12948")
        self.assertTrue(result["evidence"]["pdf_title_enrichment"])
        self.assertIn("DeepSeek-R1", result["summary"])

    def test_resolve_research_plan_adapts_docs_and_news_budgets(self) -> None:
        client = MySearchClient()

        docs_plan = client._resolve_research_plan(
            query="OpenAI pricing official",
            mode="docs",
            intent="resource",
            strategy="balanced",
            web_max_results=5,
            social_max_results=5,
            scrape_top_n=4,
            include_social=True,
            include_domains=None,
        )
        news_plan = client._resolve_research_plan(
            query="2026 oscars winners",
            mode="news",
            intent="news",
            strategy="deep",
            web_max_results=5,
            social_max_results=2,
            scrape_top_n=3,
            include_social=True,
            include_domains=None,
        )

        self.assertEqual(docs_plan["web_mode"], "docs")
        self.assertEqual(docs_plan["scrape_top_n"], 2)
        self.assertGreaterEqual(news_plan["web_max_results"], 6)
        self.assertGreaterEqual(news_plan["social_max_results"], 4)
        self.assertGreaterEqual(news_plan["scrape_top_n"], 4)

    def test_firecrawl_primary_verify_requests_content(self) -> None:
        client = MySearchClient()
        firecrawl_calls: list[dict[str, object]] = []

        def fake_firecrawl(**kwargs):  # type: ignore[no-untyped-def]
            firecrawl_calls.append(kwargs)
            return {
                "provider": "firecrawl",
                "transport": "env",
                "query": kwargs["query"],
                "answer": "",
                "results": [
                    {
                        "provider": "firecrawl",
                        "source": "web",
                        "title": "OpenAI Pricing",
                        "url": "https://openai.com/api/pricing/",
                        "snippet": "Official pricing",
                        "content": "Official pricing content" if kwargs["include_content"] else "",
                    }
                ],
                "citations": [
                    {"title": "OpenAI Pricing", "url": "https://openai.com/api/pricing/"}
                ],
            }

        client._search_firecrawl = fake_firecrawl  # type: ignore[method-assign]
        client._search_tavily = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "tavily",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [],
            "citations": [],
        }

        client._search_web_blended(
            query="OpenAI pricing official",
            mode="docs",
            intent="resource",
            strategy="verify",
            decision=RouteDecision(
                provider="firecrawl",
                reason="docs primary",
                firecrawl_categories=("research",),
                result_profile="resource",
            ),
            max_results=3,
            include_content=False,
            include_answer=False,
            include_domains=["openai.com"],
            exclude_domains=None,
        )

        self.assertTrue(firecrawl_calls)
        self.assertTrue(firecrawl_calls[0]["include_content"])

    def test_exa_search_uses_keyword_mode_for_official_pricing_query(self) -> None:
        client = MySearchClient()
        request_payloads: list[dict[str, object]] = []

        client._get_key_or_raise = lambda provider: type(  # type: ignore[method-assign]
            "FakeKey",
            (),
            {"key": "test-key", "source": "env"},
        )()

        def fake_request_json(**kwargs):  # type: ignore[no-untyped-def]
            request_payloads.append(dict(kwargs["payload"]))
            return {"results": []}

        client._request_json = fake_request_json  # type: ignore[method-assign]

        client._search_exa(
            query="MacBook Air M5 国行价格 官方",
            max_results=5,
            include_domains=["apple.com.cn"],
            exclude_domains=None,
            include_content=False,
            mode="web",
            intent="factual",
        )

        self.assertEqual(request_payloads[0]["type"], "keyword")

    def test_exa_search_uses_research_paper_category_for_pdf_mode(self) -> None:
        client = MySearchClient()
        request_payloads: list[dict[str, object]] = []

        client._get_key_or_raise = lambda provider: type(  # type: ignore[method-assign]
            "FakeKey",
            (),
            {"key": "test-key", "source": "env"},
        )()

        def fake_request_json(**kwargs):  # type: ignore[no-untyped-def]
            request_payloads.append(dict(kwargs["payload"]))
            return {"results": []}

        client._request_json = fake_request_json  # type: ignore[method-assign]

        client._search_exa(
            query="DeepSeek R1 paper pdf",
            max_results=5,
            include_domains=["arxiv.org"],
            exclude_domains=None,
            include_content=False,
            mode="pdf",
            intent="resource",
        )

        self.assertEqual(request_payloads[0]["category"], "research paper")

    def test_search_verify_conflicts_trigger_xai_arbitration(self) -> None:
        client = MySearchClient()
        client._provider_can_serve = lambda provider: True  # type: ignore[method-assign]
        client.keyring.has_provider = lambda provider: True  # type: ignore[method-assign]
        client._probe_provider_status = lambda provider, key_count: {  # type: ignore[method-assign]
            "status": "ok",
            "error": "",
            "checked_at": "2026-03-24T00:00:00+00:00",
        }
        client.config.xai.search_mode = "official"
        client._search_tavily = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "tavily",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "tavily",
                    "source": "web",
                    "title": "Update note",
                    "url": "https://updates.example.com/post-a",
                    "snippet": "Provider A",
                    "content": "",
                }
            ],
            "citations": [{"title": "Update note", "url": "https://updates.example.com/post-a"}],
        }
        client._search_firecrawl = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "firecrawl",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "",
            "results": [
                {
                    "provider": "firecrawl",
                    "source": "web",
                    "title": "Update note mirror",
                    "url": "https://blog.updates.example.com/post-b",
                    "snippet": "Provider B",
                    "content": "Mirror content" if kwargs.get("include_content") else "",
                }
            ],
            "citations": [
                {"title": "Update note mirror", "url": "https://blog.updates.example.com/post-b"}
            ],
        }
        client._search_xai = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "xai",
            "transport": "env",
            "query": kwargs["query"],
            "answer": "The mirror post is newer but both sources describe the same rollout.",
            "results": [
                {
                    "provider": "xai",
                    "source": "web",
                    "title": "Arbitration source",
                    "url": "https://news.example.com/rollout",
                    "snippet": "",
                    "content": "",
                }
            ],
            "citations": [
                {"title": "Arbitration source", "url": "https://news.example.com/rollout"},
                {"title": "Second source", "url": "https://another.example.com/rollout"},
            ],
        }

        result = client.search(
            query="vendor rollout discrepancy",
            mode="web",
            strategy="verify",
            provider="auto",
            include_answer=False,
            max_results=5,
        )

        self.assertEqual(result["evidence"]["arbitration_source"], "xai")
        self.assertEqual(
            result["evidence"]["xai_arbitration_summary"],
            "The mirror post is newer but both sources describe the same rollout.",
        )
        self.assertEqual(result["evidence"]["xai_arbitration_confidence"], "high")
        self.assertEqual(result["evidence"]["answer_source"], "xai_arbitration")
        self.assertIn("low-source-diversity", result["evidence"]["conflicts"])

    def test_research_falls_back_to_local_summary_when_xai_summary_missing(self) -> None:
        client = MySearchClient()
        client._provider_can_serve = lambda provider: False if provider.name == "xai" else True  # type: ignore[method-assign]
        client.search = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "tavily",
            "intent": "resource",
            "strategy": "deep",
            "answer": "",
            "results": [
                {
                    "title": "Primary finding",
                    "url": "https://docs.example.com/primary",
                    "snippet": "Background mode lets requests run asynchronously.",
                    "content": "",
                }
            ],
            "citations": [
                {"title": "Primary finding", "url": "https://docs.example.com/primary"},
            ],
            "evidence": {
                "providers_consulted": ["tavily"],
                "verification": "single-provider",
                "citation_count": 1,
                "source_diversity": 1,
                "source_domains": ["example.com"],
                "official_source_count": 1,
                "official_mode": "strict",
                "confidence": "high",
                "conflicts": [],
            },
        }
        client.extract_url = lambda **kwargs: {  # type: ignore[method-assign]
            "url": kwargs["url"],
            "provider": "firecrawl",
            "content": "Background mode lets requests run asynchronously and finish later without blocking the client.",
            "cache": {"extract": {"hit": False, "ttl_seconds": 300}},
        }

        result = client.research(
            query="OpenAI background mode official docs",
            mode="docs",
            strategy="deep",
            include_social=False,
            scrape_top_n=1,
        )

        self.assertIn("## Executive Summary", result["research_summary"])
        self.assertIn("## Coverage", result["research_summary"])
        self.assertIn("## Provider Contributions", result["research_summary"])
        self.assertEqual(result["summary"], result["research_summary"])
        self.assertEqual(result["report_markdown"], result["research_summary"])
        self.assertIn("executive_summary", result["report_sections"])
        self.assertIn("coverage_bits", result["report_sections"])
        self.assertEqual(result["confidence"], "high")

    def test_research_summary_fallback_uses_title_based_synthesis_for_comparison_queries(self) -> None:
        client = MySearchClient()

        summary = client._build_research_summary_fallback(
            query="best search MCP server 2026",
            web_search={"intent": "exploratory", "answer": ""},
            pages=[
                {
                    "url": "https://fast.io/resources/best-mcp-servers-search/",
                    "excerpt": "This long page starts with marketing copy that should not dominate the summary.",
                }
            ],
            citations=[
                {
                    "title": "Best MCP Servers for Search in 2026 - Top 10 Tools",
                    "url": "https://fast.io/resources/best-mcp-servers-search/",
                },
                {
                    "title": "List of Top MCP Servers for March 20, 2026",
                    "url": "https://mcpmarket.com/daily/top-mcp-server-list-march-20-2026",
                },
            ],
            social=None,
            evidence={
                "providers_consulted": ["firecrawl", "exa"],
                "citation_count": 2,
                "confidence": "medium",
                "research_plan": {"scrape_top_n": 2},
            },
        )

        self.assertIn("## Executive Summary", summary)
        self.assertIn("## Key Findings", summary)
        self.assertIn("## Coverage", summary)
        self.assertIn("## Comparison Lens", summary)
        self.assertIn("## Ranked Shortlist", summary)
        self.assertIn("## Recommendation", summary)
        self.assertIn("comparative rather than authoritative", summary)
        self.assertIn("Best MCP Servers for Search in 2026 - Top 10 Tools", summary)
        self.assertNotIn("marketing copy", summary)

    def test_research_anchors_web_discovery_to_tavily_for_factual_queries(self) -> None:
        client = MySearchClient()
        search_calls: list[dict[str, object]] = []

        def fake_search(**kwargs):  # type: ignore[no-untyped-def]
            search_calls.append(kwargs)
            return {
                "provider": "tavily",
                "intent": "exploratory",
                "strategy": "deep",
                "answer": "",
                "results": [
                    {
                        "title": "Primary result",
                        "url": "https://example.com/primary",
                        "snippet": "Primary result",
                        "content": "",
                    }
                ],
                "citations": [{"title": "Primary result", "url": "https://example.com/primary"}],
                "evidence": {
                    "providers_consulted": ["tavily"],
                    "verification": "single-provider",
                    "citation_count": 1,
                    "source_diversity": 1,
                    "source_domains": ["example.com"],
                    "official_source_count": 0,
                    "official_mode": "off",
                    "confidence": "medium",
                    "conflicts": [],
                },
            }

        client.search = fake_search  # type: ignore[method-assign]
        client.extract_url = lambda **kwargs: {  # type: ignore[method-assign]
            "url": kwargs["url"],
            "provider": "firecrawl",
            "content": "content",
            "cache": {"extract": {"hit": False, "ttl_seconds": 300}},
        }
        client.research(
            query="capital of France",
            mode="web",
            strategy="deep",
            include_social=False,
            scrape_top_n=1,
        )

        self.assertTrue(search_calls)
        self.assertEqual(search_calls[0]["mode"], "web")
        self.assertEqual(search_calls[0]["provider"], "tavily")

    def test_resolve_research_plan_uses_docs_mode_for_technical_comparison_queries(self) -> None:
        client = MySearchClient()

        plan = client._resolve_research_plan(
            query="compare OpenAI Responses API and Batch API for long-running tasks 2026",
            mode="web",
            intent="comparison",
            strategy="deep",
            web_max_results=5,
            social_max_results=5,
            scrape_top_n=3,
            include_social=False,
            include_domains=None,
        )

        self.assertEqual(plan["web_mode"], "docs")

    def test_resolve_research_plan_uses_exploratory_mode_for_generic_comparison_queries(self) -> None:
        client = MySearchClient()

        plan = client._resolve_research_plan(
            query="best search MCP server 2026",
            mode="web",
            intent="exploratory",
            strategy="deep",
            web_max_results=5,
            social_max_results=5,
            scrape_top_n=3,
            include_social=False,
            include_domains=None,
        )

        self.assertEqual(plan["web_mode"], "exploratory")

    def test_research_prioritizes_authoritative_sources_for_technical_comparison_queries(self) -> None:
        client = MySearchClient()
        client._provider_can_serve = lambda provider: provider.name != "xai"  # type: ignore[method-assign]

        def fake_search(**kwargs):  # type: ignore[no-untyped-def]
            self.assertEqual(kwargs["mode"], "docs")
            return {
                "provider": "firecrawl",
                "intent": "resource",
                "strategy": "deep",
                "answer": "",
                "results": [
                    {
                        "provider": "firecrawl",
                        "title": "A practical guide to the OpenAI Batch API",
                        "url": "https://www.eesel.ai/blog/openai-batch-api",
                        "snippet": "Third-party overview.",
                        "content": "",
                    },
                    {
                        "provider": "firecrawl",
                        "title": "OpenAI API discussion thread",
                        "url": "https://www.reddit.com/r/OpenAI/comments/example",
                        "snippet": "Community discussion.",
                        "content": "",
                    },
                ],
                "citations": [
                    {
                        "title": "A practical guide to the OpenAI Batch API",
                        "url": "https://www.eesel.ai/blog/openai-batch-api",
                    },
                    {
                        "title": "OpenAI API discussion thread",
                        "url": "https://www.reddit.com/r/OpenAI/comments/example",
                    },
                ],
                "evidence": {
                    "providers_consulted": ["firecrawl"],
                    "verification": "single-provider",
                    "citation_count": 2,
                    "source_diversity": 2,
                    "source_domains": ["eesel.ai", "reddit.com"],
                    "official_source_count": 0,
                    "official_mode": "off",
                    "confidence": "medium",
                    "conflicts": [],
                },
            }

        client.search = fake_search  # type: ignore[method-assign]
        client._search_exa = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "exa",
            "transport": "env",
            "query": kwargs["query"],
            "results": [
                {
                    "provider": "exa",
                    "title": "Background mode guide - OpenAI API",
                    "url": "https://platform.openai.com/docs/guides/background",
                    "snippet": "Official documentation for background mode.",
                    "content": "",
                },
                {
                    "provider": "exa",
                    "title": "Batch API guide - OpenAI API",
                    "url": "https://platform.openai.com/docs/guides/batch",
                    "snippet": "Official documentation for Batch API.",
                    "content": "",
                },
            ],
            "citations": [
                {
                    "title": "Background mode guide - OpenAI API",
                    "url": "https://platform.openai.com/docs/guides/background",
                },
                {
                    "title": "Batch API guide - OpenAI API",
                    "url": "https://platform.openai.com/docs/guides/batch",
                },
            ],
        }
        client.extract_url = lambda **kwargs: {  # type: ignore[method-assign]
            "url": kwargs["url"],
            "provider": "firecrawl",
            "content": f"authoritative content for {kwargs['url']}",
            "cache": {"extract": {"hit": False, "ttl_seconds": 300}},
        }

        result = client.research(
            query="compare OpenAI Responses API and Batch API for long-running tasks 2026",
            mode="web",
            strategy="deep",
            include_social=False,
            scrape_top_n=2,
        )

        self.assertIn(
            result["pages"][0]["url"],
            {
                "https://platform.openai.com/docs/guides/background",
                "https://platform.openai.com/docs/guides/batch",
            },
        )
        self.assertIn(
            result["citations"][0]["url"],
            {
                "https://platform.openai.com/docs/guides/background",
                "https://platform.openai.com/docs/guides/batch",
            },
        )
        self.assertGreaterEqual(result["evidence"]["authoritative_source_count"], 2)
        self.assertIn("Authoritative sources and corroborating analysis", result["summary"])
        self.assertIn("## Source Mix", result["summary"])
        self.assertIn("## Top Sources", result["summary"])

    def test_research_comparison_queries_downrank_community_results(self) -> None:
        client = MySearchClient()
        client._provider_can_serve = lambda provider: provider.name != "xai"  # type: ignore[method-assign]
        client.search = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "tavily",
            "intent": "exploratory",
            "strategy": "deep",
            "answer": "",
            "results": [
                {
                    "provider": "tavily",
                    "title": "Reddit thread about best MCP servers",
                    "url": "https://www.reddit.com/r/ClaudeAI/comments/example",
                    "snippet": "community discussion",
                    "content": "",
                },
                {
                    "provider": "tavily",
                    "title": "Best MCP Servers for Code Analysis in 2026 | FastMCP",
                    "url": "https://fastmcp.me/mcp-servers-for-code-analysis",
                    "snippet": "curated roundup",
                    "content": "",
                },
                {
                    "provider": "tavily",
                    "title": "best-of-mcp-servers - GitHub",
                    "url": "https://github.com/tolkonepiu/best-of-mcp-servers",
                    "snippet": "curated repo list",
                    "content": "",
                },
            ],
            "citations": [
                {
                    "title": "Reddit thread about best MCP servers",
                    "url": "https://www.reddit.com/r/ClaudeAI/comments/example",
                },
                {
                    "title": "Best MCP Servers for Code Analysis in 2026 | FastMCP",
                    "url": "https://fastmcp.me/mcp-servers-for-code-analysis",
                },
                {
                    "title": "best-of-mcp-servers - GitHub",
                    "url": "https://github.com/tolkonepiu/best-of-mcp-servers",
                },
            ],
            "evidence": {
                "providers_consulted": ["tavily"],
                "verification": "single-provider",
                "citation_count": 3,
                "source_diversity": 3,
                "source_domains": ["reddit.com", "fastmcp.me", "github.com"],
                "official_source_count": 0,
                "official_mode": "off",
                "confidence": "medium",
                "conflicts": [],
            },
        }
        client._search_exa = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "exa",
            "transport": "env",
            "query": kwargs["query"],
            "results": [],
            "citations": [],
        }
        client.extract_url = lambda **kwargs: {  # type: ignore[method-assign]
            "url": kwargs["url"],
            "provider": "firecrawl",
            "content": f"content for {kwargs['url']}",
            "cache": {"extract": {"hit": False, "ttl_seconds": 300}},
        }

        result = client.research(
            query="best search MCP server 2026",
            mode="web",
            strategy="deep",
            include_social=False,
            scrape_top_n=2,
        )

        self.assertEqual(result["pages"][0]["url"], "https://github.com/tolkonepiu/best-of-mcp-servers")
        self.assertNotEqual(result["pages"][0]["url"], "https://www.reddit.com/r/ClaudeAI/comments/example")
        self.assertEqual(result["evidence"]["community_source_count"], 1)

    def test_research_candidate_selection_prefers_curated_results_over_mcp_directories(self) -> None:
        client = MySearchClient()

        selected, meta = client._select_research_candidate_results(
            query="best search MCP server 2026",
            mode="exploratory",
            intent="exploratory",
            max_results=4,
            web_results=[
                {
                    "title": "mcp-omnisearch MCP Server",
                    "url": "https://mcp.so/server/mcp-omnisearch",
                    "snippet": "Directory listing",
                },
            ],
            docs_rescue_results=[],
            tavily_support_results=[
                {
                    "title": "The Best MCP Servers for Developers in 2026",
                    "url": "https://www.builder.io/blog/best-mcp-servers-2026",
                    "snippet": "Curated engineering comparison",
                },
                {
                    "title": "best-of-mcp-servers - GitHub",
                    "url": "https://github.com/tolkonepiu/best-of-mcp-servers",
                    "snippet": "Curated repo list",
                },
            ],
            exa_results=[
                {
                    "title": "Google Search MCP Server",
                    "url": "https://mcp-ai.org/server/google-search-mcp-server-renoscriptdev",
                    "snippet": "Directory listing",
                },
            ],
            include_domains=None,
            authoritative_preferred=False,
        )

        self.assertEqual(selected[0]["url"], "https://github.com/tolkonepiu/best-of-mcp-servers")
        self.assertEqual(selected[1]["url"], "https://www.builder.io/blog/best-mcp-servers-2026")
        self.assertIn("github.com", meta["selected_candidate_domains"])
        self.assertEqual(meta["selected_candidate_cluster_counts"]["project"], 1)
        self.assertEqual(meta["selected_candidate_cluster_counts"]["listicle"], 1)

    def test_research_falls_back_to_exa_discovery_when_web_discovery_fails(self) -> None:
        client = MySearchClient()
        client._provider_can_serve = lambda provider: provider.name == "exa"  # type: ignore[method-assign]

        def failing_search(**kwargs):  # type: ignore[no-untyped-def]
            raise MySearchError("tavily request failed (HTTP 503): upstream unavailable")

        client.search = failing_search  # type: ignore[method-assign]
        client._search_exa = lambda **kwargs: {  # type: ignore[method-assign]
            "provider": "exa",
            "transport": "env",
            "query": kwargs["query"],
            "results": [
                {
                    "provider": "exa",
                    "title": "Best MCP Servers for Search in 2026 - Top 10 Tools",
                    "url": "https://fast.io/resources/best-mcp-servers-search/",
                    "snippet": "Comparison-heavy page",
                    "content": "",
                }
            ],
            "citations": [
                {
                    "title": "Best MCP Servers for Search in 2026 - Top 10 Tools",
                    "url": "https://fast.io/resources/best-mcp-servers-search/",
                }
            ],
        }
        client.extract_url = lambda **kwargs: {  # type: ignore[method-assign]
            "url": kwargs["url"],
            "provider": "firecrawl",
            "content": "A structured comparison of MCP servers for search-focused agent workflows.",
            "cache": {"extract": {"hit": False, "ttl_seconds": 300}},
        }

        result = client.research(
            query="best search MCP server 2026",
            mode="web",
            strategy="deep",
            include_social=False,
            scrape_top_n=1,
        )

        self.assertEqual(result["web_search"]["provider"], "exa")
        self.assertEqual(result["web_search"]["fallback"]["to"], "exa")
        self.assertIn("comparative rather than authoritative", result["summary"])
        self.assertEqual(result["evidence"]["providers_consulted"], ["exa"])
        self.assertIn("## Provider Contributions", result["summary"])
        self.assertIn("Exa expanded semantic coverage", result["summary"])

    def test_research_falls_back_to_docs_rescue_when_primary_web_discovery_fails(self) -> None:
        client = MySearchClient()
        client._provider_can_serve = lambda provider: provider.name != "xai"  # type: ignore[method-assign]
        client._research_prefers_authoritative_sources = lambda **kwargs: True  # type: ignore[method-assign]
        client._resolve_research_plan = lambda **kwargs: {  # type: ignore[method-assign]
            "web_mode": "web",
            "web_max_results": kwargs["web_max_results"],
            "social_max_results": kwargs["social_max_results"],
            "scrape_top_n": kwargs["scrape_top_n"],
        }

        def fake_search(**kwargs):  # type: ignore[no-untyped-def]
            if kwargs["mode"] == "web":
                raise MySearchError("tavily request failed (HTTP 503): upstream unavailable")
            self.assertEqual(kwargs["mode"], "docs")
            return {
                "provider": "firecrawl",
                "transport": "env",
                "query": kwargs["query"],
                "answer": "",
                "results": [
                    {
                        "provider": "firecrawl",
                        "title": "API Pricing | OpenAI",
                        "url": "https://openai.com/api/pricing/",
                        "snippet": "Official pricing page",
                        "content": "",
                    }
                ],
                "citations": [
                    {"title": "API Pricing | OpenAI", "url": "https://openai.com/api/pricing/"},
                ],
                "evidence": {
                    "providers_consulted": ["firecrawl"],
                    "verification": "single-provider",
                    "citation_count": 1,
                    "source_diversity": 1,
                    "source_domains": ["openai.com"],
                    "official_source_count": 1,
                    "official_mode": "strict",
                    "confidence": "high",
                    "conflicts": [],
                },
            }

        client.search = fake_search  # type: ignore[method-assign]
        client.extract_url = lambda **kwargs: {  # type: ignore[method-assign]
            "url": kwargs["url"],
            "provider": "firecrawl",
            "content": "Pricing details for the OpenAI API.",
            "cache": {"extract": {"hit": False, "ttl_seconds": 300}},
        }

        result = client.research(
            query="OpenAI API pricing official",
            mode="web",
            strategy="deep",
            include_social=False,
            scrape_top_n=1,
        )

        self.assertEqual(result["web_search"]["provider"], "firecrawl")
        self.assertEqual(result["web_search"]["fallback"]["to"], "docs_rescue")
        self.assertEqual(result["pages"][0]["url"], "https://openai.com/api/pricing/")

    def test_dedupe_research_results_preserves_matched_providers(self) -> None:
        client = MySearchClient()

        deduped = client._dedupe_research_results_for_report(
            [
                {
                    "provider": "exa",
                    "title": "best-of-mcp-servers - GitHub",
                    "url": "https://github.com/tolkonepiu/best-of-mcp-servers",
                    "snippet": "Curated repo list",
                }
            ],
            [
                {
                    "provider": "tavily",
                    "title": "best-of-mcp-servers - GitHub",
                    "url": "https://github.com/tolkonepiu/best-of-mcp-servers",
                    "snippet": "Repository of MCP servers",
                }
            ],
        )

        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["matched_providers"], ["exa", "tavily"])

    def test_research_claim_text_prefers_clean_title_over_navigation_excerpt(self) -> None:
        client = MySearchClient()

        claim = client._research_claim_text(
            title="Migrate to the Responses API | OpenAI API",
            excerpt="## Search the API docs Search docs ### Suggested response_format Primary navigation Search docs",
            comparison_like=True,
        )

        self.assertEqual(claim, "Migrate to the Responses API")

    def test_research_claim_evidence_merges_similar_claims_by_signature(self) -> None:
        client = MySearchClient()

        claims = client._build_research_claim_evidence(
            ordered_results=[
                {
                    "provider": "exa",
                    "matched_providers": ["exa"],
                    "title": "Migrate to the Responses API | OpenAI API",
                    "url": "https://developers.openai.com/api/docs/guides/migrate-to-responses",
                    "snippet": "Migration guide for the Responses API.",
                },
                {
                    "provider": "tavily",
                    "matched_providers": ["tavily"],
                    "title": "Migrate to Responses API - OpenAI Docs",
                    "url": "https://platform.openai.com/docs/guides/responses-vs-chat-completions",
                    "snippet": "Docs about migrating to the Responses API.",
                },
            ],
            pages=[],
            citations=[
                {
                    "title": "Migrate to the Responses API | OpenAI API",
                    "url": "https://developers.openai.com/api/docs/guides/migrate-to-responses",
                },
                {
                    "title": "Migrate to Responses API - OpenAI Docs",
                    "url": "https://platform.openai.com/docs/guides/responses-vs-chat-completions",
                },
            ],
            comparison_like=True,
        )

        self.assertEqual(len(claims), 1)
        self.assertEqual(sorted(claims[0]["providers"]), ["exa", "tavily"])
        self.assertEqual(len(claims[0]["sources"]), 2)

    def test_research_report_sections_include_claim_evidence_and_source_clusters(self) -> None:
        client = MySearchClient()

        sections = client._build_research_report_sections(
            query="best search MCP server 2026",
            web_search={"intent": "exploratory", "answer": ""},
            ordered_results=[
                {
                    "provider": "exa",
                    "matched_providers": ["exa", "tavily"],
                    "title": "best-of-mcp-servers - GitHub",
                    "url": "https://github.com/tolkonepiu/best-of-mcp-servers",
                    "snippet": "Curated repo list for MCP servers.",
                },
                {
                    "provider": "tavily",
                    "title": "The Best MCP Servers for Developers in 2026",
                    "url": "https://www.builder.io/blog/best-mcp-servers-2026",
                    "snippet": "Curated engineering comparison.",
                },
            ],
            pages=[
                {
                    "url": "https://github.com/tolkonepiu/best-of-mcp-servers",
                    "excerpt": "This repository curates MCP servers for search, code analysis, and automation use cases.",
                    "content": "",
                }
            ],
            citations=[
                {
                    "title": "best-of-mcp-servers - GitHub",
                    "url": "https://github.com/tolkonepiu/best-of-mcp-servers",
                },
                {
                    "title": "The Best MCP Servers for Developers in 2026",
                    "url": "https://www.builder.io/blog/best-mcp-servers-2026",
                },
            ],
            social=None,
            evidence={
                "providers_consulted": ["exa", "tavily"],
                "citation_count": 2,
                "confidence": "medium",
                "research_plan": {"scrape_top_n": 2, "web_mode": "exploratory"},
                "selected_candidate_domains": ["github.com", "builder.io"],
                "authoritative_research": False,
            },
        )
        summary = client._render_research_report(sections)

        self.assertIn("claim_evidence", sections)
        self.assertIn("source_clusters", sections)
        self.assertIn("decision_table", sections)
        self.assertEqual(sections["claim_evidence"][0]["providers"], ["exa", "tavily"])
        self.assertEqual(sections["source_clusters"][0]["label"], "project")
        self.assertEqual(sections["source_clusters"][0]["tier"], "primary")
        self.assertGreater(sections["source_clusters"][0]["weight"], 0)
        self.assertEqual(sections["decision_table"][0]["fit"], "project-native source")
        self.assertIn("## Claim-Level Evidence", summary)
        self.assertIn("## Source Clusters", summary)
        self.assertIn("| Candidate | Cluster | Provider Support | Evidence Note |", summary)
        self.assertIn("## Decision Table", summary)


if __name__ == "__main__":
    unittest.main()
