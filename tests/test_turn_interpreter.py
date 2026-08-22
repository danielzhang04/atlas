import asyncio
import inspect

import pytest

from worker.turn_interpreter import (
    StructuredToolResponse,
    TurnInterpretationError,
    TurnInterpreter,
    TurnKind,
)


def route_input(**overrides):
    value = {
        "operation": "calendar.create_event",
        "target": "event",
        "resource": None,
        "source": None,
        "app": "calendar",
        "steps": 1,
        "risk": "low",
        "cross_source": False,
        "cross_app": False,
        "research": False,
        "discovery": False,
        "iteration": False,
        "verification": False,
        "durable_artifact": False,
    }
    value.update(overrides)
    return value


class FakeClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("unexpected model call")
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class Response:
    def __init__(self, content, stop_reason="tool_use"):
        self.content = content
        self.stop_reason = stop_reason


def test_ordinary_conversation_is_plain_text_with_one_hidden_claude_delegate_tool():
    client = FakeClient(StructuredToolResponse(text="Right here."))
    result = asyncio.run(TurnInterpreter(client, persona="Sound composed.").interpret(
        "Hello?", [{"id": "calendar", "label": "Calendar"}],
    ))

    assert result.kind is TurnKind.REPLY
    assert result.text == "Right here."
    assert result.request is None
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    assert call["tools"][0]["name"] == "atlas_delegate_to_claude"
    assert "Sound composed." in call["system"]
    assert call["messages"][-1]["content"] == "Hello?"
    assert "Calendar" not in call["tools"][0]["description"]
    assert "capabilities" not in call["messages"][-1]["content"].lower()


def test_backend_catalog_never_enters_conversation_history():
    client = FakeClient(
        StructuredToolResponse(text="Yeah. Finally."),
        StructuredToolResponse(text="Hey."),
    )
    interpreter = TurnInterpreter(client)
    catalog = [{"id": "google.drive.read", "label": "Google Drive"}]

    first = asyncio.run(interpreter.interpret("Shit. Finally.", catalog))
    second = asyncio.run(interpreter.interpret("Hey, Atlas.", catalog))

    assert first.text == "Yeah. Finally."
    assert second.text == "Hey."
    for call in client.calls:
        user_messages = [message["content"] for message in call["messages"]
                         if message["role"] == "user"]
        assert all("Google Drive" not in content for content in user_messages)
        assert "Google Drive" not in call["tools"][0]["description"]


def test_short_follow_up_receives_recent_conversational_context():
    client = FakeClient(
        StructuredToolResponse(text="I'm here."),
        StructuredToolResponse(text="I meant the worker was unavailable."),
    )
    interpreter = TurnInterpreter(client)

    asyncio.run(interpreter.interpret("Hello?"))
    result = asyncio.run(interpreter.interpret("What do you mean?"))

    assert result.text == "I meant the worker was unavailable."
    second_messages = client.calls[1]["messages"]
    assert {"role": "user", "content": "Hello?"} in second_messages
    assert {"role": "assistant", "content": "I'm here."} in second_messages


def test_work_request_delegates_the_exact_utterance_without_model_authored_structure():
    client = FakeClient(StructuredToolResponse(input={}))
    result = asyncio.run(TurnInterpreter(client).interpret("Schedule the event"))

    assert result.kind is TurnKind.REQUEST
    assert result.text == ""
    assert result.request.operation == "claude.connected"
    assert result.request.target == "connected-cli"
    assert result.request.app == "claude-code"
    assert result.route_input == {}
    assert result.transcript == "Schedule the event"
    assert result.route_call_id == "toolu_atlas_test"


def test_opening_any_site_uses_the_same_claude_delegate_without_aliases():
    client = FakeClient(StructuredToolResponse(input={}))
    result = asyncio.run(TurnInterpreter(client).interpret("Open YouTube", [{
        "id": "desktop.open", "label": "Desktop Open", "domain": "desktop",
        "status": "connected", "detail": "Approved targets: youtube (url)",
    }]))

    assert result.kind is TurnKind.REQUEST
    assert result.request.operation == "claude.connected"
    assert result.transcript == "Open YouTube"
    tools = client.calls[0]["tools"]
    assert [tool["name"] for tool in tools] == ["atlas_delegate_to_claude"]
    assert tools[0]["input_schema"]["properties"] == {}


def test_atlas_catalog_does_not_prejudge_claude_code_connections():
    client = FakeClient(StructuredToolResponse(input={}))
    result = asyncio.run(TurnInterpreter(client).interpret("Open YouTube", [{
        "id": "desktop.open", "label": "Desktop Open", "domain": "desktop",
        "status": "configuration-needed", "detail": "Add desktop_target_aliases",
    }]))

    assert result.kind is TurnKind.REQUEST
    assert result.request.operation == "claude.connected"
    assert "configuration-needed" not in client.calls[0]["tools"][0]["description"]


def test_delegated_run_result_is_returned_to_claude_for_natural_narration():
    client = FakeClient(
        StructuredToolResponse(input={}),
        StructuredToolResponse(text="I'm on it. You'll see it in Workers."),
    )
    interpreter = TurnInterpreter(client)
    turn = asyncio.run(interpreter.interpret("Open YouTube"))
    text = asyncio.run(interpreter.narrate_route(turn, {
        "status": "queued", "lane": "slow", "error_code": None,
        "replayed": False, "job_visible": True,
    }))

    assert text == "I'm on it. You'll see it in Workers."
    narration = client.calls[1]
    assert "tools" not in narration
    assert narration["messages"][-2]["content"][0]["name"] == "atlas_delegate_to_claude"
    assert '"status":"queued"' in narration["messages"][-1]["content"][0]["content"]


def test_route_result_is_returned_to_claude_for_natural_narration():
    client = FakeClient(
        StructuredToolResponse(input={}),
        StructuredToolResponse(text="I queued the research. It'll appear in Workers."),
    )
    interpreter = TurnInterpreter(client)
    turn = asyncio.run(interpreter.interpret("Research this and write a report."))
    text = asyncio.run(interpreter.narrate_route(turn, {
        "status": "queued", "lane": "slow", "error_code": None,
        "replayed": False, "job_visible": True,
    }))

    assert text == "I queued the research. It'll appear in Workers."
    assert len(client.calls) == 2
    narration = client.calls[1]
    assert "tools" not in narration
    tool_result = narration["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert '"status":"queued"' in tool_result["content"]
    assert "internal error codes" in narration["system"]


def test_route_schema_is_non_strict_and_host_validation_remains_authoritative():
    client = FakeClient(StructuredToolResponse(text="Hello."))
    asyncio.run(TurnInterpreter(client).interpret("Hello"))
    tool = client.calls[0]["tools"][0]
    schema = tool["input_schema"]

    assert "strict" not in tool

    forbidden = {"minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems"}

    def keys(value):
        if isinstance(value, dict):
            for key, item in value.items():
                yield key
                yield from keys(item)
        elif isinstance(value, list):
            for item in value:
                yield from keys(item)

    assert forbidden.isdisjoint(keys(schema))
    assert set(schema["properties"]) == set(schema["required"])
    assert schema["additionalProperties"] is False


@pytest.mark.parametrize("value", [
    {"operation": "shell.execute"},
    {"url": "https://example.com"},
    {"confirmed": True},
])
def test_model_authored_delegate_fields_are_ignored(value):
    result = asyncio.run(TurnInterpreter(
        FakeClient(StructuredToolResponse(input=value))).interpret("do it"))
    assert result.request.operation == "claude.connected"
    assert result.route_input == {}
    assert result.transcript == "do it"


@pytest.mark.parametrize("response", [
    Response([{"type": "tool_use", "name": "wrong_tool", "id": "toolu_1", "input": {}}]),
    Response([
        {"type": "text", "text": "I'll do it."},
        {"type": "tool_use", "name": "atlas_delegate_to_claude", "id": "toolu_1", "input": {}},
    ]),
    Response([
        {"type": "tool_use", "name": "atlas_delegate_to_claude", "id": "toolu_1", "input": {}},
        {"type": "tool_use", "name": "atlas_delegate_to_claude", "id": "toolu_2", "input": {}},
    ]),
    Response([{"type": "text", "text": "hello"}], stop_reason="tool_use"),
])
def test_ambiguous_or_unrecognized_model_output_is_not_authority(response):
    with pytest.raises(TurnInterpretationError):
        asyncio.run(TurnInterpreter(FakeClient(response)).interpret("hello"))


def test_model_failure_is_sanitized_and_transcript_is_bounded(caplog):
    class ProviderFailure(RuntimeError):
        status_code = 400

    client = FakeClient(ProviderFailure("private api_key=must-not-escape"))
    with pytest.raises(TurnInterpretationError) as error:
        asyncio.run(TurnInterpreter(client).interpret("hello"))
    assert "api_key" not in str(error.value)
    assert "api_key" not in caplog.text
    assert "status=400" in caplog.text
    assert "conversation model" not in error.value.public_message.lower()

    timeout_client = FakeClient(asyncio.TimeoutError())
    with pytest.raises(TurnInterpretationError) as timeout_error:
        asyncio.run(TurnInterpreter(timeout_client).interpret("hello"))
    assert timeout_error.value.reason == "timeout"
    assert "still here" in timeout_error.value.public_message.lower()

    with pytest.raises(TurnInterpretationError):
        asyncio.run(TurnInterpreter(FakeClient()).interpret("x" * 5_000))
    with pytest.raises(TurnInterpretationError):
        asyncio.run(TurnInterpreter(FakeClient()).interpret(" "))


def test_interpreter_has_no_execution_plane_imports():
    source = inspect.getsource(__import__("worker.turn_interpreter", fromlist=["x"]))
    assert "from .jobstore" not in source
    assert "from .frontdesk" not in source
    assert "from .subscription_worker" not in source
