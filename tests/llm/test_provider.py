import asyncio
import unittest
from types import SimpleNamespace

from agent2.llm.provider import OpenAICompatibleChatModel
from agent2.llm.schemas import WrittenAnswer


class FakeCompletions:
    def __init__(self, content=None, parsed=None):
        self.content = content
        self.parsed = parsed
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(("create", kwargs))
        message = SimpleNamespace(content=self.content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    async def parse(self, **kwargs):
        self.calls.append(("parse", kwargs))
        message = SimpleNamespace(parsed=self.parsed)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeClient:
    def __init__(self, completions):
        self.chat = SimpleNamespace(completions=completions)
        self.closed = False

    async def close(self):
        self.closed = True


class FakeClientFactory:
    def __init__(self, completions):
        self.completions = completions
        self.clients = []

    def __call__(self, **options):
        client = FakeClient(self.completions)
        client.options = options
        self.clients.append(client)
        return client

class ProviderTests(unittest.TestCase):
    def model(self, mode, completions):
        factory = FakeClientFactory(completions)
        model = OpenAICompatibleChatModel(
            model_name="test", api_key="not-a-real-key", base_url="https://example.com/v1",
            structured_mode=mode, extra_body={"enable_thinking": False},
            client_factory=factory)
        return model, factory

    def test_json_object_is_validated_by_pydantic(self):
        completions = FakeCompletions('{"summary":"ok","steps":[]}')
        model, factory = self.model("json_object", completions)
        result = asyncio.run(model.parse(
            prompt="Return JSON", output_type=WrittenAnswer, operation="test"))
        self.assertEqual("ok", result.summary)
        self.assertEqual("json_object", completions.calls[0][1]["response_format"]["type"])
        self.assertTrue(factory.clients[0].closed)

    def test_json_schema_uses_sdk_parse(self):
        expected = WrittenAnswer(summary="ok")
        completions = FakeCompletions(parsed=expected)
        model, factory = self.model("json_schema", completions)
        result = asyncio.run(model.parse(
            prompt="Return JSON", output_type=WrittenAnswer, operation="test"))
        self.assertEqual(expected, result)
        self.assertIs(WrittenAnswer, completions.calls[0][1]["response_format"])
        self.assertTrue(factory.clients[0].closed)

    def test_repeated_sync_calls_use_clients_owned_by_each_event_loop(self):
        completions = FakeCompletions(parsed=WrittenAnswer(summary="ok"))
        model, factory = self.model("json_schema", completions)

        first = asyncio.run(model.parse(
            prompt="one", output_type=WrittenAnswer, operation="first"))
        second = asyncio.run(model.parse(
            prompt="two", output_type=WrittenAnswer, operation="second"))

        self.assertEqual("ok", first.summary)
        self.assertEqual("ok", second.summary)
        self.assertEqual(2, len(factory.clients))
        self.assertTrue(all(client.closed for client in factory.clients))
