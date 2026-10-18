from collections import deque


class ScriptedStructuredModel:
    def __init__(self, *results, model_name="test-model"):
        self.model_name = model_name
        self.results = deque(results)
        self.calls = []

    async def parse(self, *, prompt, output_type, operation):
        self.calls.append({"prompt": prompt, "output_type": output_type, "operation": operation})
        result = self.results.popleft()
        if isinstance(result, Exception):
            raise result
        return result

    def assert_complete(self):
        if self.results:
            raise AssertionError(f"{len(self.results)} scripted model result(s) unused")
