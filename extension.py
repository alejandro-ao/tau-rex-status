"""Report Tau's agent state to Rex through the OSC 7501 program-status protocol.

Rex (and other supporting terminals) show the current status in tab headers and
session pickers. This extension maps Tau's lifecycle onto the protocol:

- ``idle``: no agent run is active.
- ``working``: the model or a tool is running.
- ``blocked``: a tool waits for the user's answer (``ask_user_question``).
- ``done``: a run finished successfully.
- ``error``: a run stopped after a provider, retry, or compaction failure.

Rex documents the protocol at
https://www.superlogical.com/rex/docs/build/program-status

Load it with ``tau -e <path-to-extension.py>``, install it under
``~/.tau/extensions/``, or install the repository with
``tau install git:github.com/alejandro-ao/tau-rex-status``. Set
``TAU_PROGRAM_STATUS=0`` to disable the escape sequences.
"""

from __future__ import annotations

import base64
import os
import re
import sys
from typing import TextIO

from tau_agent.messages import AgentMessage, AssistantMessage
from tau_coding.extensions import ExtensionAPI, ExtensionContext

APP_NAME = "tau"
DISABLE_ENV = "TAU_PROGRAM_STATUS"
MAX_MESSAGE_BYTES = 2048
MAX_TITLE_BYTES = 192
PREVIEW_CHARS = 160
QUESTION_TOOLS = frozenset({"ask_user_question", "question"})
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _status_enabled(stream: TextIO | None = None) -> bool:
    """Return whether stdout can receive OSC 7501 sequences."""
    if os.environ.get(DISABLE_ENV, "").lower() in {"0", "false", "no", "off"}:
        return False
    target = sys.__stdout__ if stream is None else stream
    if target is None or not getattr(target, "isatty", lambda: False)():
        return False
    return os.environ.get("TERM", "") != "dumb"


def _single_line(value: str) -> str:
    """Replace control characters and collapse whitespace to one line."""
    return " ".join(_CONTROL_RE.sub(" ", value).split())


def _encode_text(value: str, max_bytes: int) -> str:
    """Return protocol-safe unpadded base64 for a single-line UTF-8 value."""
    encoded = _single_line(value).encode("utf-8")
    if len(encoded) > max_bytes:
        encoded = encoded[:max_bytes].decode("utf-8", "ignore").encode("utf-8")
    return base64.b64encode(encoded).decode("ascii").rstrip("=")


def build_report(
    state: str,
    *,
    kind: str | None = None,
    title: str | None = None,
    message: str | None = None,
) -> str:
    """Build one OSC 7501 report, replacing Tau's root record completely."""
    pairs = [f"state={state}"]
    if state == "blocked" and kind is not None:
        pairs.append(f"kind={kind}")
    pairs.append(f"app={APP_NAME}")
    if title:
        pairs.append(f"title={_encode_text(title, MAX_TITLE_BYTES)}")
    if message:
        pairs.append(f"msg={_encode_text(message, MAX_MESSAGE_BYTES)}")
    return f"\x1b]7501;{':'.join(pairs)}\x1b\\"


class ProgramStatusReporter:
    """Stateful writer that deduplicates and safely emits OSC 7501 reports."""

    def __init__(
        self,
        *,
        stream: TextIO | None = None,
        enabled: bool | None = None,
    ) -> None:
        self._stream = sys.__stdout__ if stream is None else stream
        self.enabled = _status_enabled(self._stream) if enabled is None else enabled
        self._state = "idle"
        self._kind: str | None = None
        self._title = "τ"
        self._message = "Ready"
        self._last_sequence: str | None = None

    def set_title(self, title: str) -> None:
        """Update the root record's human-readable label."""
        self._title = title
        self._emit_current()

    def set_state(
        self,
        state: str,
        *,
        kind: str | None = None,
        message: str | None = None,
    ) -> None:
        """Replace Tau's root record with ``state`` and an optional message."""
        self._state = state
        self._kind = kind
        self._message = message or _default_message(state)
        self._emit_current()

    def clear(self) -> None:
        """Remove Tau's root record when the session ends."""
        self._send("\x1b]7501;state=clear\x1b\\")

    def _emit_current(self) -> None:
        self._send(
            build_report(
                self._state,
                kind=self._kind,
                title=self._title,
                message=self._message,
            )
        )

    def _send(self, sequence: str) -> None:
        if not self.enabled or sequence == self._last_sequence:
            return
        if self._stream is None:
            self.enabled = False
            return
        try:
            self._stream.write(sequence)
            self._stream.flush()
        except (OSError, ValueError):
            self.enabled = False
            return
        self._last_sequence = sequence


def _default_message(state: str) -> str:
    return {
        "idle": "Ready",
        "working": "Thinking",
        "done": "Finished",
        "error": "Request failed",
    }.get(state, state)


def _session_title(context: ExtensionContext, name: str | None = None) -> str:
    """Return a short label for the root record, preferring the session name."""
    candidate = context.session_name if name is None else name
    if isinstance(candidate, str):
        candidate = candidate.strip()
        if candidate and candidate.lower() != "untitled session":
            return f"τ | {candidate}"
    cwd = getattr(context, "cwd", None)
    cwd_name = getattr(cwd, "name", "")
    return f"τ | {cwd_name}" if cwd_name else "τ"


def _finished_state(
    messages: list[AgentMessage],
    failure: str | None,
) -> tuple[str, str]:
    """Map the final messages of a run to a protocol state and message."""
    if failure:
        return "error", failure
    for message in reversed(messages):
        if not isinstance(message, AssistantMessage):
            continue
        if message.stop_reason == "error":
            return "error", message.error_message or "Request failed"
        if message.stop_reason == "aborted":
            return "idle", "Stopped"
        preview = _single_line(message.text)
        if len(preview) > PREVIEW_CHARS:
            preview = preview[: PREVIEW_CHARS - 1].rstrip() + "…"
        return "done", preview or "Finished"
    return "idle", "Stopped"


def setup(tau: ExtensionAPI) -> None:
    """Report Tau's lifecycle through Rex's OSC 7501 program-status protocol."""
    reporter = ProgramStatusReporter()
    run_messages: list[AgentMessage] = []
    run_failure: str | None = None
    run_active = False

    def on_session_start(event: object, context: ExtensionContext) -> None:
        del event
        nonlocal run_active, run_failure, run_messages
        run_active = False
        run_failure = None
        run_messages = []
        reporter.set_title(_session_title(context))
        reporter.set_state("idle", message="Ready")

    def on_session_shutdown(event: object, context: ExtensionContext) -> None:
        del event, context
        reporter.clear()

    def on_session_info_changed(event: object, context: ExtensionContext) -> None:
        name = getattr(event, "name", None)
        reporter.set_title(_session_title(context, name if isinstance(name, str) else None))

    def on_agent_start(event: object, context: ExtensionContext) -> None:
        del event, context
        nonlocal run_active, run_failure, run_messages
        run_active = True
        run_failure = None
        run_messages = []
        reporter.set_state("working", message="Thinking")

    def on_turn_start(event: object, context: ExtensionContext) -> None:
        del event, context
        if run_active:
            reporter.set_state("working", message="Thinking")

    def on_tool_execution_start(event: object, context: ExtensionContext) -> None:
        del context
        tool_name = str(getattr(event, "tool_name", "") or "tool")
        if tool_name in QUESTION_TOOLS:
            arguments = getattr(event, "args", {})
            question = arguments.get("question") if isinstance(arguments, dict) else None
            message = _single_line(question) if isinstance(question, str) else ""
            reporter.set_state(
                "blocked",
                kind="question",
                message=message or "Waiting for your answer",
            )
            return
        reporter.set_state("working", message=f"Running {tool_name}")

    def on_tool_execution_end(event: object, context: ExtensionContext) -> None:
        del context
        message = "Tool failed; thinking" if getattr(event, "is_error", False) else "Thinking"
        reporter.set_state("working", message=message)

    def on_agent_end(event: object, context: ExtensionContext) -> None:
        del context
        nonlocal run_messages
        run_messages = list(getattr(event, "messages", ()))
        if getattr(event, "will_retry", False):
            reporter.set_state("working", message="Retrying")

    def on_agent_settled(event: object, context: ExtensionContext) -> None:
        del event, context
        nonlocal run_active
        if not run_active:
            return
        run_active = False
        state, message = _finished_state(run_messages, run_failure)
        reporter.set_state(state, message=message)

    def on_auto_retry_start(event: object, context: ExtensionContext) -> None:
        del context
        attempt = getattr(event, "attempt", 1)
        maximum = getattr(event, "max_attempts", attempt)
        reporter.set_state("working", message=f"Retrying ({attempt}/{maximum})")

    def on_auto_retry_end(event: object, context: ExtensionContext) -> None:
        del context
        nonlocal run_failure
        if not getattr(event, "success", False):
            run_failure = getattr(event, "final_error", None) or "Request failed"
        else:
            run_failure = None

    def on_compaction_start(event: object, context: ExtensionContext) -> None:
        del event, context
        reporter.set_state("working", message="Compacting conversation")

    def on_compaction_end(event: object, context: ExtensionContext) -> None:
        del context
        nonlocal run_failure
        if getattr(event, "aborted", False):
            run_failure = getattr(event, "error_message", None) or "Compaction failed"
            reporter.set_state("working", message="Compaction failed")
            return
        reporter.set_state("working", message="Thinking")

    tau.on("session_start", on_session_start)
    tau.on("session_shutdown", on_session_shutdown)
    tau.on("session_info_changed", on_session_info_changed)
    tau.on("agent_start", on_agent_start)
    tau.on("turn_start", on_turn_start)
    tau.on("tool_execution_start", on_tool_execution_start)
    tau.on("tool_execution_end", on_tool_execution_end)
    tau.on("agent_end", on_agent_end)
    tau.on("agent_settled", on_agent_settled)
    tau.on("auto_retry_start", on_auto_retry_start)
    tau.on("auto_retry_end", on_auto_retry_end)
    tau.on("compaction_start", on_compaction_start)
    tau.on("compaction_end", on_compaction_end)
