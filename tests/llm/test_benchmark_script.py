import unittest

from scripts.benchmark_agent2_llm import Measurement, summarize


class BenchmarkScriptTests(unittest.TestCase):
    def test_summary_calculates_timings_without_credentials(self):
        records = [
            Measurement("model-a", "json_object", "understanding", 1, True, 2.0),
            Measurement("model-a", "json_object", "understanding", 2, True, 4.0),
            Measurement("model-a", "json_object", "understanding", 3, False, 1.0,
                        "TimeoutError", "timed out"),
        ]
        result = summarize(records)[0]
        self.assertEqual(2, result["successes"])
        self.assertEqual(1, result["failures"])
        self.assertEqual(3.0, result["mean_seconds"])
        self.assertNotIn("api_key", result)
