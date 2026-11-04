"""直接注入标准/自定义 completions 资源时保留客户端路由和行为."""

import json
import os
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import Mock, patch

import httpx2
from langchain_core.exceptions import ModelTimeoutError
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from openai import AsyncOpenAI, OpenAI
from pydantic import PrivateAttr

from shader_deep.experiments._parameter_model import configure_parameter_model
from shader_deep.experiments.parameter_agent import build_parameter_agent
from tests.unit_tests.agents.test_parameter_execution import reply


class ParameterClientTests(TestCase):
    def test_guard_copy_does_not_recreate_private_resources_or_run_post_init(self) -> None:
        resources = []
        initialized = []

        def allocate() -> object:
            resource = object()
            resources.append(resource)
            return resource

        class ResourceModel(ChatOpenAI):
            _resource: object = PrivateAttr(default_factory=allocate)

            def model_post_init(self, context: object) -> None:
                super().model_post_init(context)
                initialized.append(id(self))

        with patch.dict(os.environ, {}, clear=True):
            model = ResourceModel(api_key="offline", model="offline-model")
            guarded = configure_parameter_model(model)
            configured_again = configure_parameter_model(guarded)
        self.assertEqual(resources, [model._resource])
        self.assertEqual(initialized, [id(model)])
        self.assertIs(guarded._resource, model._resource)
        self.assertIs(configured_again._resource, model._resource)
        self.assertIsInstance(guarded, ResourceModel)

    def test_injected_sync_resource_preserves_transport_and_configuration(self) -> None:
        requests = []

        def send(_client: httpx2.Client, request: httpx2.Request, **_kwargs: object) -> httpx2.Response:
            requests.append(request)
            return reply(request)

        with (
            patch.dict(os.environ, {}, clear=True),
            OpenAI(
                api_key="sdk-secret", base_url="https://injected.example/v1", default_headers={"x-custom": "kept"}, timeout=7, max_retries=5
            ) as sdk,
            patch("httpx2.Client.send", autospec=True, side_effect=send),
        ):
            model = ChatOpenAI(api_key="model-secret", model="offline-model", client=sdk.chat.completions, max_tokens=12000, use_responses_api=False)
            self.assertIsNone(model.root_client)
            guarded = configure_parameter_model(model)
            self.assertIs(guarded.root_client._client, sdk._client)
            self.assertEqual(guarded.root_client.max_retries, 0)
            self.assertEqual(sdk.max_retries, 5)
            result = build_parameter_agent(model=guarded).invoke({"messages": [HumanMessage(content="hello")]})
            self.assertEqual(result["messages"][-1].content, "done")
            self.assertIs(model.client, sdk.chat.completions)
            self.assertIsNone(model.root_client)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url.host, "injected.example")
        self.assertEqual(requests[0].headers["authorization"], "Bearer sdk-secret")
        self.assertEqual(requests[0].headers["x-custom"], "kept")
        self.assertEqual(requests[0].extensions["timeout"]["read"], 7)
        self.assertEqual(json.loads(requests[0].content)["max_completion_tokens"], 12000)

    def test_injected_sdk_resource_cannot_add_hidden_retries(self) -> None:
        def send(_client: httpx2.Client, request: httpx2.Request, **_kwargs: object) -> httpx2.Response:
            msg = "TLS handshake"
            raise httpx2.ConnectTimeout(msg, request=request)

        with (
            patch.dict(os.environ, {}, clear=True),
            OpenAI(api_key="offline", max_retries=5) as sdk,
            patch("httpx2.Client.send", autospec=True, side_effect=send) as transport,
            patch("langchain.agents.middleware.model_retry.time.sleep"),
            self.assertRaises(ModelTimeoutError),
        ):
            model = ChatOpenAI(api_key="offline", model="offline-model", client=sdk.chat.completions, use_responses_api=False)
            build_parameter_agent(model=model).invoke({"messages": [HumanMessage(content="hello")]})
        self.assertEqual(transport.call_count, 2)

    def test_opaque_resource_is_preserved_without_new_root_or_guessed_options(self) -> None:
        opaque = Mock()
        opaque.with_raw_response.create.return_value.parse.return_value = {
            "id": "opaque",
            "created": 0,
            "model": "offline-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "opaque reply"}, "finish_reason": "stop"}],
        }
        with patch.dict(os.environ, {}, clear=True):
            model = ChatOpenAI(api_key="offline", model="offline-model", client=opaque, use_responses_api=False, max_retries=4)
            guarded = configure_parameter_model(model)
            self.assertIs(guarded.client, opaque)
            self.assertIsNone(guarded.root_client)
            self.assertEqual(guarded.max_retries, 4)
            result = build_parameter_agent(model=model).invoke({"messages": [HumanMessage(content="hello")]})
        self.assertEqual(result["messages"][-1].content, "opaque reply")
        opaque.with_raw_response.create.assert_called_once()
        opaque.with_options.assert_not_called()


class AsyncParameterClientTests(IsolatedAsyncioTestCase):
    async def test_injected_async_resource_preserves_transport_and_configuration(self) -> None:
        requests = []

        async def send(_client: httpx2.AsyncClient, request: httpx2.Request, **_kwargs: object) -> httpx2.Response:
            requests.append(request)
            return reply(request)

        with patch.dict(os.environ, {}, clear=True), patch("httpx2.AsyncClient.send", autospec=True, side_effect=send):
            async with AsyncOpenAI(api_key="sdk-secret", base_url="https://injected.example/v1", timeout=9, max_retries=6) as sdk:
                model = ChatOpenAI(api_key="offline", model="offline-model", async_client=sdk.chat.completions, use_responses_api=False)
                self.assertIsNone(model.root_async_client)
                guarded = configure_parameter_model(model)
                self.assertIs(guarded.root_async_client._client, sdk._client)
                self.assertEqual(guarded.root_async_client.max_retries, 0)
                self.assertEqual(sdk.max_retries, 6)
                result = await build_parameter_agent(model=guarded).ainvoke({"messages": [HumanMessage(content="hello")]})
                self.assertEqual(result["messages"][-1].content, "done")
                self.assertIsNone(model.root_async_client)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url.host, "injected.example")
        self.assertEqual(requests[0].headers["authorization"], "Bearer sdk-secret")
        self.assertEqual(requests[0].extensions["timeout"]["read"], 9)
