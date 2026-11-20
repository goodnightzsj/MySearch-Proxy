import importlib.util
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock

from mysearch.clients import MySearchClient
from mysearch.errors import MySearchError
from test_provider_disabled import fixture_config


spec = importlib.util.spec_from_file_location("depth_experiment", Path(__file__).resolve().parents[1] / "scripts/compare_tavily_depths.py")
experiment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(experiment)


class TavilyDepthExperimentTests(TestCase):
    def test_paired_inputs_only_change_depth_and_restore_transport(self):
        client = MySearchClient(fixture_config())
        self.addCleanup(client.close)
        request = Mock(return_value={"results": []})
        client._request_json = request
        result = experiment.run_experiment(client, [{"id": "unseen", "query": "coral reef ecosystems"}])
        self.assertIs(client._request_json, request)
        self.assertEqual(len(result), 6)
        self.assertEqual([item["depth"] for item in result], ["fast", "ultra-fast", "ultra-fast", "fast", "fast", "ultra-fast"])
        payloads = [call.kwargs["payload"] for call in request.call_args_list]
        self.assertEqual(len({str({key: value for key, value in payload.items() if key != "search_depth"}) for payload in payloads}), 1)
        self.assertTrue(all(payload["include_usage"] and not payload["include_raw_content"] and not payload["include_answer"] for payload in payloads))

    def test_provider_errors_remain_observations_and_transport_is_restored(self):
        client = MySearchClient(fixture_config())
        self.addCleanup(client.close)
        request = Mock(side_effect=MySearchError("synthetic provider unavailable"))
        client._request_json = request
        results = experiment.run_experiment(client, [{"id": "unseen", "query": "tidal energy"}])
        self.assertIs(client._request_json, request)
        self.assertEqual(len(results), 6)
        self.assertTrue(all(row["status"] == "error" and row["error"] for row in results))
