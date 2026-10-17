"""M06 缺失条件判断与追问：11.3.1 指标与条件表规则。"""
import unittest

from agent2 import clarify, eval_m06
from agent2.models import Profile, ProfileField


def profile(version=1, **confirmed):
    return Profile(profile_id="p", user_id="u", version=version,
                   fields={k: ProfileField(value=v, status="confirmed") for k, v in confirmed.items()})


class M06MetricsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = eval_m06.run(repeat=10)

    def test_targets_on_fixed_regression_set(self):
        metrics = self.report["metrics"]
        self.assertEqual(metrics["s1_recall"], 1.0)
        self.assertLessEqual(metrics["extra_question_rate"], eval_m06.TARGETS["extra_question_rate"])
        self.assertLessEqual(metrics["rule_p95_ms"], eval_m06.TARGETS["rule_p95_ms"])
        self.assertEqual(metrics["errors"], dict.fromkeys(eval_m06.ERROR_KINDS, 0))
        self.assertEqual(metrics["spurious_missing"], 0)
        self.assertTrue(self.report["ok"])

    def test_every_record_is_traceable(self):
        for record in self.report["records"]:
            self.assertEqual(record["fixture_version"], "m06-cases-v1")
            self.assertTrue(record["trace_id"])
            self.assertIn("expected", record)
            self.assertIn("actual", record)
            self.assertIn("stage_ms", record)
            for question in record["actual"]["questions"]:
                self.assertTrue(question["question_code"] and question["rule"])
                self.assertIsInstance(question["profile_version"], int)


class ConditionTableTests(unittest.TestCase):
    def test_never_more_than_two_questions_but_all_missing_are_identified(self):
        result = clarify.assess("credit_card", profile())
        self.assertEqual(len(result.missing_fields), 4)
        self.assertEqual(len(result.questions), clarify.MAX_QUESTIONS_PER_ROUND)

    def test_unknown_status_value_is_never_used(self):
        p = Profile(profile_id="p", user_id="u", version=1,
                    fields={"destination_country": ProfileField(value="UK", status="unknown")})
        result = clarify.assess("visa_documents", p)
        self.assertNotIn("destination_country", result.facts)
        self.assertIn("destination_country", result.missing_fields)

    def test_conversation_condition_is_temporary_not_confirmed(self):
        result = clarify.assess("visa_documents", profile(), {"destination_country": "UK",
                                                              "visa_category": "student_visa"})
        self.assertTrue(result.ready)
        self.assertEqual({f.source for f in result.facts.values()}, {"conversation"})

    def test_nice_to_have_is_an_assumption_gap_not_a_question(self):
        result = clarify.assess("visa_documents", profile(destination_country="UK", visa_category="student_visa"))
        self.assertEqual(result.questions, [])
        self.assertEqual(result.assumption_gaps, ["school", "planned_start_date"])

    def test_model_suggestions_are_rechecked_against_the_table(self):
        def suggest(topic, eligible):
            return ["nationality_secret", "funding_source"]

        result = clarify.assess("visa_documents", profile(destination_country="UK", visa_category="student_visa"),
                                condition_fields=["applicant_age", "funding_source", "tb_listed_country_resident"],
                                suggest=suggest)
        self.assertEqual([q.field for q in result.questions], ["funding_source", "applicant_age"])
        self.assertIn("suggestion_rejected", [n.code for n in result.notices])
        self.assertGreaterEqual(result.model_ms, 0)

    def test_conflict_and_version_change_are_explained(self):
        result = clarify.assess("visa_documents", profile(version=4, destination_country="UK"),
                                {"destination_country": "US", "visa_category": "student_visa"},
                                expected_profile_version=3)
        self.assertEqual(result.state, "field_conflict")
        self.assertIn("UK", result.explanation)
        self.assertEqual(result.missing_fields, [])
        self.assertEqual([q.rule for q in result.questions], ["conflict.destination_country"])
        self.assertIn("profile_version_changed", [n.code for n in result.notices])

    def test_unsupported_topic_has_no_conclusion(self):
        self.assertEqual(clarify.assess("invest", profile()).state, "unsupported_topic")


if __name__ == "__main__":
    unittest.main()
