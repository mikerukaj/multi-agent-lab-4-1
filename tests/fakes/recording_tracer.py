"""A ``Tracer`` whose LangSmith client records spans in memory instead of sending them.

The client is a ``MagicMock(spec=langsmith.Client)``: ``RunTree.post`` calls its
``create_run`` and ``RunTree.patch`` its ``update_run``, so ``Recorder.spans`` rebuilds
the span tree (name, parent, trace, inputs, outputs, metadata) without any network
(contracts/tracing.md, "Test hooks"). The file name does not match ``test*.py``.
"""

from unittest import mock

import langsmith

from observability.redaction import Redactor
from observability.tracing import Tracer


class Recorder:
    """The spans a recording client received, keyed by run ID."""

    def __init__(self, client: mock.MagicMock) -> None:
        self.client = client

    @property
    def spans(self) -> dict[str, dict]:
        spans: dict[str, dict] = {}
        for call in self.client.create_run.call_args_list:
            kwargs = call.kwargs
            spans[str(kwargs["id"])] = {
                "id": str(kwargs["id"]),
                "name": kwargs["name"],
                "parent": str(kwargs["parent_run_id"]) if kwargs.get("parent_run_id") else None,
                "trace_id": str(kwargs.get("trace_id")),
                "inputs": kwargs.get("inputs") or {},
                "outputs": {},
                "metadata": dict((kwargs.get("extra") or {}).get("metadata") or {}),
                "error": None,
            }
        for call in self.client.update_run.call_args_list:
            kwargs = call.kwargs
            span = spans.get(str(call.args[0] if call.args else kwargs.get("run_id")))
            if span is None:
                continue
            span["outputs"] = kwargs.get("outputs") or {}
            span["metadata"].update((kwargs.get("extra") or {}).get("metadata") or {})
            span["error"] = kwargs.get("error")
        return spans

    def named(self, prefix: str) -> list[dict]:
        """Spans whose name starts with ``prefix`` (e.g. ``agent:``)."""
        return [span for span in self.spans.values() if span["name"].startswith(prefix)]

    def roots(self) -> list[dict]:
        return [span for span in self.spans.values() if span["parent"] is None]

    def ancestors(self, span: dict) -> list[str]:
        """Names of a span's ancestors, nearest first."""
        spans, names, parent = self.spans, [], span["parent"]
        while parent is not None and parent in spans:
            names.append(spans[parent]["name"])
            parent = spans[parent]["parent"]
        return names

    def text(self) -> str:
        """Everything the client was sent, as one string (for secret scans)."""
        return repr(self.client.mock_calls)


def recording_tracer(redactor: Redactor | None = None, on_warning=None) -> tuple[Tracer, Recorder]:
    """An enabled ``Tracer`` on a recording client, and its ``Recorder``."""
    client = mock.MagicMock(spec=langsmith.Client)
    tracer = Tracer(True, client, project="tests", redactor=redactor or Redactor([]),
                    on_warning=on_warning)
    return tracer, Recorder(client)
