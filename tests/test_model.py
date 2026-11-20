import json

import httpx
import pytest
from openai import AsyncOpenAI

from recall.model import DEFAULT_MODEL, OUTPUT_TOKENS, Model, ModelError, scripted
from recall.sources import load_fixture


@pytest.mark.parametrize("kind", ["success", "refusal", "malformed"])
def test_official_sdk_structured_response_path(monkeypatch, kind):
    snapshot = load_fixture("23-794")
    expected = scripted(snapshot)
    reserved = []

    def handler(request):
        assert str(request.url) == "https://api.openai.com/v1/responses"
        payload = json.loads(request.content)
        assert payload["model"] == DEFAULT_MODEL
        assert payload["max_output_tokens"] == OUTPUT_TOKENS
        assert payload["store"] is False and "tools" not in payload
        assert json.loads(payload["input"]) == {"notice_text": snapshot.text}
        format_ = payload["text"]["format"]
        assert format_["type"] == "json_schema" and format_["strict"]
        content = {"type": "output_text", "text": expected.model_dump_json(), "annotations": []}
        if kind == "refusal":
            content = {"type": "refusal", "refusal": "Declined."}
        elif kind == "malformed":
            content["text"] = '{"groups":[], "unsupported":[123]}'
        return httpx.Response(
            200,
            json={
                "object": "response",
                "id": "resp_mock",
                "model": DEFAULT_MODEL,
                "created_at": 1,
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "id": "msg_mock",
                        "role": "assistant",
                        "status": "completed",
                        "content": [content],
                    }
                ],
            },
        )

    def local_client(**kwargs):
        return AsyncOpenAI(
            **kwargs,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

    monkeypatch.setenv("OPENAI_API_KEY", "mock")
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.setattr("recall.model.AsyncOpenAI", local_client)
    model = Model(reserved.append)
    if kind == "success":
        assert model.extract(snapshot) == expected
    elif kind == "refusal":
        with pytest.raises(ModelError, match="refused"):
            model.extract(snapshot)
    else:
        with pytest.raises(ModelError, match="schema"):
            model.extract(snapshot)
    assert len(reserved) == 1 and reserved[0] > 0
