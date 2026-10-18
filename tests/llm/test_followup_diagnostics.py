import unittest

from scripts.test_agent2_followups import RecordingWriter, safe_error_message


class FakeWriter:
    model_version = "fake-model"
    prompt_version = "fake-prompt"

    def __init__(self, error=None):
        self.error = error

    async def write(self, *, request, verified_answer):
        if self.error:
            raise self.error
        return "written"


class FollowupDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_records_writer_timeout_before_workflow_fallback(self):
        records = []
        writer = RecordingWriter(FakeWriter(TimeoutError("request timed out")), records)

        with self.assertRaises(TimeoutError):
            await writer.write(request=None, verified_answer=None)

        self.assertEqual("answer_writer", records[0].operation)
        self.assertEqual("TimeoutError", records[0].error_type)
        self.assertEqual("request timed out", records[0].error_message)
        self.assertFalse(records[0].ok)

    async def test_records_success(self):
        records = []
        writer = RecordingWriter(FakeWriter(), records)

        result = await writer.write(request=None, verified_answer=None)

        self.assertEqual("written", result)
        self.assertTrue(records[0].ok)
        self.assertIsNone(records[0].error_type)

    def test_redacts_authentication_values(self):
        error = RuntimeError("Authorization: Bearer secret-token api_key=secret-key")

        message = safe_error_message(error)

        self.assertNotIn("secret-token", message)
        self.assertNotIn("secret-key", message)
        self.assertIn("[REDACTED]", message)


if __name__ == "__main__":
    unittest.main()
