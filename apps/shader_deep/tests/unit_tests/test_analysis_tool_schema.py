"""真实传输载荷兼容要求 required 必须为数组的模型服务."""

from __future__ import annotations

from copy import deepcopy

from langchain.tools import tool
from pydantic import BaseModel

from shader_deep.analysis.transport import configure_analysis_model
from shader_deep.analysis.types import AnalysisOptions
from shader_deep.config import build_model
from tests.unit_tests._generation_fixture import GenerationFixture


class OptionalFilter(BaseModel):
    offset: int = 0


class ToolSchemaTests(GenerationFixture):
    def test_optional_and_empty_tools_remain_callable_with_strict_provider_schema(self) -> None:
        @tool
        def list_items(options: OptionalFilter, limit: int = 50) -> str:
            """Read entries."""
            return str(options.offset + limit)

        @tool
        def finish() -> str:
            """Finish."""
            return "done"

        def respond(request: dict[str, object]) -> dict[str, object]:
            parameters = {entry["function"]["name"]: entry["function"]["parameters"] for entry in request["tools"]}
            self.assertEqual(parameters["finish"]["required"], [])
            self.assertEqual(parameters["list_items"]["required"], ["options"])
            nested = parameters["list_items"]["properties"]["options"]
            self.assertEqual(nested["required"], [])
            self.assertEqual(parameters["list_items"]["properties"]["limit"]["default"], 50)
            return self.call("finish", {})

        self.response = respond
        for streaming in (False, True):
            configured = configure_analysis_model(build_model(), AnalysisOptions(stream_model_responses=streaming))
            bound = configured.bind_tools([list_items, finish])
            original = deepcopy(bound.kwargs["tools"])
            response = bound.invoke("Finish the task.")
            self.assertEqual(response.tool_calls[0]["args"], {})
            self.assertEqual(bound.kwargs["tools"], original)
        self.assertEqual(list_items.invoke({"options": {}}), "50")
