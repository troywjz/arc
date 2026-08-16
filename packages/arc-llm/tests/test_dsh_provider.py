from __future__ import annotations

import json
import socket
import threading
from pathlib import Path
from typing import Any

from arc_llm import ProviderRequest, ProviderTerminalKind
from arc_llm.providers.dsh import DshAdapter, REQUEST_SCHEMA_VERSION


class Observer:
    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    def progress(self, kind: str, data: Any) -> None:
        self.events.append((kind, data))

    def native_handle(self, _handle: Any) -> None:
        raise AssertionError("DSH bridge must not expose native resume handles")


class Stop:
    def raise_if_requested(self) -> None:
        return


def test_dsh_adapter_consumes_authenticated_ndjson_stream(tmp_path: Path) -> None:
    socket_path = tmp_path / "arc-llm.sock"
    token_path = tmp_path / "arc-llm.token"
    token_path.write_text("test-token\n", encoding="utf-8")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    server.listen(1)
    received: dict[str, Any] = {}

    def serve() -> None:
        connection, _ = server.accept()
        with connection:
            raw = b""
            while b"\n" not in raw:
                raw += connection.recv(4096)
            received.update(json.loads(raw.split(b"\n", 1)[0]))
            events = [
                {"type": "started"},
                {"type": "text-delta", "text": "answer"},
                {
                    "type": "usage",
                    "usage": {"input_tokens": 4, "output_tokens": 2},
                },
                {"type": "finish", "reason": "stop"},
            ]
            connection.sendall(
                b"".join(
                    (json.dumps(event) + "\n").encode("utf-8") for event in events
                )
            )
        server.close()

    thread = threading.Thread(target=serve)
    thread.start()
    adapter = DshAdapter(
        socket_path=socket_path,
        token_path=token_path,
        provider_route="fake-provider",
    )
    result = adapter.start(
        ProviderRequest("prompt", "fake-model", None, {}, 3, tmp_path),
        Observer(),
        Stop(),
    )
    thread.join(timeout=3)

    assert result.terminal_kind is ProviderTerminalKind.COMPLETED
    assert result.candidates[0].text == "answer"
    assert result.usage is not None
    assert result.usage.input_tokens == 4
    assert result.usage.output_tokens == 2
    assert received == {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "token": "test-token",
        "op": "generate",
        "provider": "fake-provider",
        "model": "fake-model",
        "prompt": "prompt",
    }


def test_dsh_doctor_reports_missing_bridge_as_unavailable(tmp_path: Path) -> None:
    adapter = DshAdapter(
        socket_path=tmp_path / "missing.sock",
        token_path=tmp_path / "missing.token",
    )
    diagnostic = adapter.doctor()
    assert diagnostic.available is False
    assert diagnostic.details["credential_owner"] == "deepseek-harness"
