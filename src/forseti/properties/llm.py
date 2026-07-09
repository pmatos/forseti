"""The proposer's LLM transport seam -- a single-shot completion behind a port.

The property proposer (#65) makes a Core-native LLM call rather than delegating
to the host harness (ADR-0009 D3), so the grading loop (W3) and GEPA (#5) can
drive it programmatically. `LLMClient` is the provider-agnostic seam: v1 ships
one implementation, `ClaudeCliClient`, which shells out to ``claude -p`` with
stdlib `subprocess`+`json` (the base install stays dependency-free). A future
OpenAI/local backend implements the same three members -- that is the
multi-provider generalisation point.

Mirrors `esbmc/runner.py`'s subprocess/timeout/typed-error idiom, but every
invocation failure raises `LLMError` rather than returning a value: a proposer
that can't reach the model must fail loud, never silently propose nothing (the
"never silently pass" invariant applies to the LLM call as much as to ESBMC).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Protocol

# Truncate captured stderr / envelope text in error messages so a runaway model
# or a stack trace can't bloat the raised exception.
_ERR_CLIP = 500


class LLMError(RuntimeError):
    """Any transport/invocation failure of the underlying model (fail-loud)."""


class LLMClient(Protocol):
    """A provider-agnostic single-shot completion plus provenance descriptors.

    `provider` and `model` are recorded on a proposer run's telemetry so a batch
    of candidates is traceable to the backend that produced it. `complete`
    returns the model's *text* with any transport envelope already unwrapped --
    the string a candidate parser can consume directly. `provider`/`model` are
    read-only (properties) so a frozen-dataclass client with a `ClassVar`
    `provider` still satisfies the protocol.
    """

    @property
    def provider(self) -> str: ...

    @property
    def model(self) -> str: ...

    def complete(self, prompt: str) -> str: ...


@dataclass(frozen=True)
class ClaudeCliClient:
    """`LLMClient` backed by ``claude -p`` (ADR-0009 D3, v1 hardcoded backend).

    `extra_args` defaults to ``--strict-mcp-config`` (no ``--mcp-config`` given,
    so MCP servers stay disabled) for a cleaner, more hermetic run. The prompt is
    written to the child's STDIN, never argv: a C translation unit easily exceeds
    ``ARG_MAX``, and ``-p`` reads its prompt from stdin.
    """

    model: str = "sonnet"
    claude_bin: str = "claude"
    timeout_s: float = 120.0
    extra_args: tuple[str, ...] = ("--strict-mcp-config",)
    provider: ClassVar[str] = "claude -p"

    def complete(self, prompt: str) -> str:
        """Run ``claude -p`` on `prompt`; return the model's text or raise `LLMError`.

        ``--output-format json`` yields one envelope
        ``{"type","subtype","is_error","result",...}``; the assistant's text is
        ``env["result"]``. Every failure mode -- binary missing, timeout, other
        OS error, non-zero exit, non-JSON stdout, an error envelope, or an
        empty/absent/non-string ``result`` -- becomes an `LLMError`.
        """
        cmd = [
            self.claude_bin,
            "-p",
            "--output-format",
            "json",
            "--model",
            self.model,
            *self.extra_args,
        ]
        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                text=True,
                capture_output=True,
                timeout=self.timeout_s,
            )
        except FileNotFoundError as exc:
            raise LLMError(f"claude not found: {self.claude_bin}") from exc
        except subprocess.TimeoutExpired as exc:
            raise LLMError(f"claude timed out after {self.timeout_s}s") from exc
        except OSError as exc:
            raise LLMError(f"claude invocation failed: {exc}") from exc

        if proc.returncode != 0:
            raise LLMError(
                f"claude exit {proc.returncode}: {proc.stderr.strip()[:_ERR_CLIP]}"
            )
        try:
            envelope = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise LLMError("claude --output-format json emitted non-JSON") from exc
        if not isinstance(envelope, dict) or envelope.get("is_error"):
            raise LLMError(f"claude reported an error: {str(envelope)[:_ERR_CLIP]}")
        result = envelope.get("result")
        if not isinstance(result, str) or not result.strip():
            raise LLMError("claude returned an empty or absent result")
        return result


if TYPE_CHECKING:
    # mypy-only structural guard: fail type-checking if ClaudeCliClient ever
    # drifts from the LLMClient protocol it must satisfy (mirrors fix.py).
    def _client_is_llmclient(c: ClaudeCliClient) -> LLMClient:
        return c
