"""Native DeepSeek Harness model bridge adapter.

The adapter deliberately owns only the ARC-side transport.  Provider
credentials, model routing, retries, and streaming remain inside DSH's native
``ctx.llm`` service; this process authenticates to the local bridge with a
0600 token and exchanges versioned NDJSON events over a Unix socket.
"""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path
from typing import Any, Mapping

from ..errors import FailureCategory, ProviderFailure
from ..output import CandidateMaterial
from .base import (
    IsolationMode,
    ProviderCapabilities,
    ProviderDiagnostic,
    ProviderExecution,
    ProviderRequest,
    ProviderResumeRequest,
    ProviderTerminalKind,
    ProviderUsage,
    StructuredOutputMode,
    UsageAvailability,
)


REQUEST_SCHEMA_VERSION = "arc.dsh-llm.request.v1"
DEFAULT_PROVIDER_ROUTE = "deepseek-official"


class DshAdapter:
    name = "dsh"
    compatibility_version = "dsh-native-llm-bridge.v1"

    def __init__(
        self,
        *,
        socket_path: str | Path | None = None,
        token_path: str | Path | None = None,
        provider_route: str | None = None,
        connect_timeout_seconds: float = 3.0,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.socket_path = Path(socket_path) if socket_path is not None else None
        self.token_path = Path(token_path) if token_path is not None else None
        self.provider_route = provider_route
        self.connect_timeout_seconds = connect_timeout_seconds
        self.environment = environment

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            native_resume=False,
            structured_output=StructuredOutputMode.PROMPT,
            usage=UsageAvailability.PARTIAL,
            config_isolation=IsolationMode.EXPLICIT,
            tool_isolation=IsolationMode.EXPLICIT,
            cooperative_stop=True,
            provider_persistence=False,
        )

    def doctor(self) -> ProviderDiagnostic:
        socket_path, token_path, provider_route = self._paths_and_route()
        available = (
            socket_path.is_socket()
            and token_path.is_file()
            and bool(_read_token(token_path))
        )
        return ProviderDiagnostic(
            self.name,
            available,
            str(socket_path) if socket_path.exists() else None,
            details={
                "socket_path": str(socket_path),
                "token_path": str(token_path),
                "provider_route": provider_route,
                "credential_owner": "deepseek-harness",
            },
        )

    def start(
        self,
        request: ProviderRequest,
        observer: Any,
        stop: Any,
    ) -> ProviderExecution:
        return self._call(request, observer, stop)

    def resume(
        self,
        handle: Any,
        request: ProviderResumeRequest,
        observer: Any,
        stop: Any,
    ) -> ProviderExecution:
        return ProviderExecution(
            ProviderTerminalKind.FAILED,
            failure=ProviderFailure(
                "The DSH bridge does not expose native provider resume; ARC must start a fresh generation.",
                category=FailureCategory.UNAVAILABLE,
                details={"code": "native_resume_unavailable"},
            ),
            diagnostics={"provider": self.name, "native_resume": False},
        )

    def _call(self, request: ProviderRequest, observer: Any, stop: Any) -> ProviderExecution:
        socket_path, token_path, provider_route = self._paths_and_route(request.environment)
        token = _read_token(token_path)
        if not token:
            return _failed(
                "The DSH ARC bridge token is missing or empty.",
                FailureCategory.UNAVAILABLE,
                details={"code": "bridge_token_unavailable", "token_path": str(token_path)},
            )

        payload = {
            "schema_version": REQUEST_SCHEMA_VERSION,
            "token": token,
            "op": "generate",
            "provider": provider_route,
            "model": request.model,
            "prompt": request.prompt,
        }
        if request.capabilities.get("dsh_system_prompt"):
            payload["system"] = request.capabilities["dsh_system_prompt"]

        started = time.monotonic()
        text_parts: list[str] = []
        event_count = 0
        reasoning_chars = 0
        usage: ProviderUsage | None = None
        finish_reason: str | None = None
        failure: ProviderFailure | None = None
        observer.progress("llm_provider_started", {"bridge": "dsh", "provider_route": provider_route})
        try:
            with self._connect(socket_path) as connection:
                connection.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
                buffer = b""
                while True:
                    stop.raise_if_requested()
                    try:
                        chunk = connection.recv(64 * 1024)
                    except socket.timeout:
                        continue
                    if not chunk:
                        break
                    buffer += chunk
                    while b"\n" in buffer:
                        raw, buffer = buffer.split(b"\n", 1)
                        if not raw.strip():
                            continue
                        event_count += 1
                        event = _decode_event(raw)
                        kind = event.get("type")
                        if kind == "text-delta":
                            text = event.get("text")
                            if isinstance(text, str):
                                text_parts.append(text)
                        elif kind == "reasoning-delta":
                            text = event.get("text")
                            if isinstance(text, str):
                                reasoning_chars += len(text)
                        elif kind == "usage":
                            usage = _usage(event.get("usage"))
                        elif kind == "finish":
                            finish_reason = event.get("reason")
                            if finish_reason in {"error", "aborted"}:
                                failure = _event_failure(event)
                            break
                    if finish_reason is not None:
                        break
                if buffer.strip() and finish_reason is None:
                    raise ProviderFailure(
                        "The DSH bridge returned an unterminated NDJSON event.",
                        category=FailureCategory.SCHEMA,
                        details={"code": "bridge_invalid_event_stream"},
                    )
        except ProviderFailure as exc:
            failure = exc
        except OSError as exc:
            failure = ProviderFailure(
                f"Could not reach the DSH ARC bridge: {exc}",
                category=FailureCategory.TRANSPORT,
                retryable=True,
                details={"code": "bridge_transport", "socket_path": str(socket_path)},
            )

        diagnostics = {
            "socket_path": str(socket_path),
            "provider_route": provider_route,
            "event_count": event_count,
            "reasoning_chars": reasoning_chars,
            "finish_reason": finish_reason,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
        if failure is not None:
            observer.progress("llm_provider_failed", {"category": failure.category.value})
            return ProviderExecution(
                ProviderTerminalKind.FAILED,
                usage=usage,
                failure=failure,
                diagnostics=diagnostics,
            )
        text = "".join(text_parts)
        if not text.strip():
            failure = ProviderFailure(
                "The DSH bridge completed without visible model text.",
                category=FailureCategory.TRANSPORT,
                details={"code": "bridge_empty_output"},
            )
            observer.progress("llm_provider_failed", {"category": failure.category.value})
            return ProviderExecution(
                ProviderTerminalKind.FAILED,
                usage=usage,
                failure=failure,
                diagnostics=diagnostics,
            )
        observer.progress("llm_provider_finished", {"bridge": "dsh", "finish_reason": finish_reason or "eof"})
        return ProviderExecution(
            ProviderTerminalKind.COMPLETED,
            candidates=(CandidateMaterial(text=text, terminal=True),),
            usage=usage,
            diagnostics=diagnostics,
        )

    def _connect(self, socket_path: Path) -> socket.socket:
        deadline = time.monotonic() + self.connect_timeout_seconds
        last_error: OSError | None = None
        while time.monotonic() < deadline:
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.settimeout(0.25)
            try:
                connection.connect(str(socket_path))
                return connection
            except OSError as exc:
                last_error = exc
                connection.close()
                time.sleep(0.05)
        if last_error is None:
            last_error = OSError("connection timeout")
        raise last_error

    def _paths_and_route(
        self,
        environment: Mapping[str, str] | None = None,
    ) -> tuple[Path, Path, str]:
        source = dict(os.environ if environment is None else environment)
        if self.environment is not None:
            source = {**source, **self.environment}
        socket_path = self.socket_path or Path(
            source.get("ARC_DSH_LLM_SOCKET")
            or source.get("DSH_ARC_LLM_SOCKET")
            or (Path.home() / ".dsh" / "runtime" / "arc-llm.sock")
        )
        token_path = self.token_path or Path(
            source.get("ARC_DSH_LLM_TOKEN_FILE")
            or source.get("DSH_ARC_LLM_TOKEN_FILE")
            or f"{socket_path}.token"
        )
        provider_route = self.provider_route or source.get(
            "ARC_DSH_PROVIDER", DEFAULT_PROVIDER_ROUTE
        )
        return socket_path, token_path, provider_route


def _read_token(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ""


def _decode_event(raw: bytes) -> Mapping[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderFailure(
            "The DSH bridge returned invalid JSON.",
            category=FailureCategory.SCHEMA,
            details={"code": "bridge_invalid_json"},
        ) from exc
    if not isinstance(value, Mapping):
        raise ProviderFailure(
            "The DSH bridge returned a non-object event.",
            category=FailureCategory.SCHEMA,
            details={"code": "bridge_event_not_object"},
        )
    return value


def _usage(value: Any) -> ProviderUsage | None:
    if not isinstance(value, Mapping):
        return None
    return ProviderUsage(
        _nonnegative_int(value.get("input_tokens")),
        _nonnegative_int(value.get("output_tokens")),
        _nonnegative_int(value.get("cached_input_tokens")),
    )


def _nonnegative_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _event_failure(event: Mapping[str, Any]) -> ProviderFailure:
    failure = event.get("failure")
    message = (
        failure.get("message")
        if isinstance(failure, Mapping) and isinstance(failure.get("message"), str)
        else "The DSH native model call failed."
    )
    code = (
        failure.get("code")
        if isinstance(failure, Mapping) and isinstance(failure.get("code"), str)
        else "bridge_provider_error"
    )
    return ProviderFailure(
        message,
        category=FailureCategory.TRANSPORT,
        retryable=code in {"RATE_LIMIT", "rate_limit", "bridge_transport"},
        details={"code": code},
    )


def _failed(
    message: str,
    category: FailureCategory,
    *,
    details: Mapping[str, Any],
) -> ProviderExecution:
    failure = ProviderFailure(message, category=category, details=details)
    return ProviderExecution(ProviderTerminalKind.FAILED, failure=failure, diagnostics=dict(details))
