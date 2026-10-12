import importlib.util
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


spec = importlib.util.spec_from_file_location("transcription_server", Path(__file__).with_name("server.py"))
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


@pytest.fixture
def client():
    with patch.dict(os.environ, {"TRANSCRIPTION_API_TOKEN": "x" * 32}), TestClient(server.app) as instance:
        yield instance


def test_unauthorized_upload_never_decodes(client):
    with patch.object(server, "transcribe_file") as decode:
        assert client.post("/transcribe", content=b"audio").status_code == 401
        decode.assert_not_called()


def test_limits_and_hidden_endpoints(client):
    headers = {"x-transcription-token": "x" * 32, "content-type": "application/octet-stream"}
    assert client.post("/transcribe", headers={**headers, "content-length": str(server.MAX_BYTES + 1)}).status_code == 413
    assert client.post("/transcribe?language=../../model", headers=headers, content=b"audio").status_code == 400
    assert client.post("/load", headers=headers).status_code == 404
    assert client.get("/docs").status_code == 404
    assert client.post("/transcribe", headers=headers, content=b"").status_code == 400


def test_transcription_and_cleanup(client):
    paths = []
    def decode(source, directory, language):
        paths.append(source)
        assert source.read_bytes() == b"audio"
        assert language == "ar"
        return {"text": "recognized speech", "language": "ar"}
    with patch.object(server, "transcribe_file", side_effect=decode):
        result = client.post("/transcribe?language=ar", content=b"audio",
                             headers={"x-transcription-token": "x" * 32, "content-type": "application/octet-stream"})
    assert result.status_code == 200
    assert result.json()["language"] == "ar"
    assert not paths[0].exists()