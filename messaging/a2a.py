"""A2A 1.0 protocol objects, builders, validators and errors, using only the stdlib.

Field names follow the A2A 1.0 JSON (camelCase); there is no ``kind`` member:
a Part is told apart by which of ``text``/``data``/``raw``/``url`` it holds.
Enums use their full names (``TASK_STATE_COMPLETED``, ``ROLE_USER``) and
timestamps are ISO-8601 UTC with millisecond precision and a ``Z`` suffix.
See specs/002-qa-feedback-loop-mcp/contracts/a2a-binding.md.
"""

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Literal, TypedDict

A2A_VERSION = "1.0"
BINDING_URN = "urn:multi-agent-lab:a2a-binding:asyncio-broker:1.0"

TaskState = Literal[
    "TASK_STATE_SUBMITTED",
    "TASK_STATE_WORKING",
    "TASK_STATE_COMPLETED",
    "TASK_STATE_FAILED",
    "TASK_STATE_CANCELED",
    "TASK_STATE_REJECTED",
    "TASK_STATE_INPUT_REQUIRED",
    "TASK_STATE_AUTH_REQUIRED",
]
Role = Literal["ROLE_USER", "ROLE_AGENT"]
ROLES = ("ROLE_USER", "ROLE_AGENT")
TERMINAL_STATES = frozenset(
    {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED"}
)
PART_CONTENT_KEYS = ("text", "data", "raw", "url")
SUPPORTED_OUTPUT_MODES = frozenset({"application/json", "text/plain"})

ERROR_CODES = {
    "TaskNotFoundError": -32001,
    "TaskNotCancelableError": -32002,
    "PushNotificationNotSupportedError": -32003,
    "UnsupportedOperationError": -32004,
    "ContentTypeNotSupportedError": -32005,
    "InvalidAgentResponseError": -32006,
    "ExtendedAgentCardNotConfiguredError": -32007,
    "ExtensionSupportRequiredError": -32008,
    "VersionNotSupportedError": -32009,
    "MethodNotFound": -32601,
    "InvalidParams": -32602,
    "UnknownAgent": -32602,
    "InternalError": -32603,
    "BrokerClosedError": -32603,
    "A2ATimeoutError": -32603,
}


class Part(TypedDict, total=False):
    """One piece of message or artifact content: exactly one of the content members."""

    text: str
    data: Any
    raw: str
    url: str
    mediaType: str
    filename: str
    metadata: dict


class _MessageRequired(TypedDict):
    messageId: str
    role: Role
    parts: list[Part]


class Message(_MessageRequired, total=False):
    """One A2A message; ``metadata.skillId`` names the skill, ``metadata.sender`` the agent."""

    contextId: str
    taskId: str
    metadata: dict
    extensions: list[str]
    referenceTaskIds: list[str]


class _TaskStatusRequired(TypedDict):
    state: TaskState


class TaskStatus(_TaskStatusRequired, total=False):
    """A task's state, with a reason message for failed or rejected tasks."""

    message: Message
    timestamp: str


class _ArtifactRequired(TypedDict):
    artifactId: str
    parts: list[Part]


class Artifact(_ArtifactRequired, total=False):
    """A task's output."""

    name: str
    description: str
    metadata: dict
    extensions: list[str]


class _TaskRequired(TypedDict):
    id: str
    contextId: str
    status: TaskStatus


class Task(_TaskRequired, total=False):
    """The unit of work a message starts on its recipient."""

    artifacts: list[Artifact]
    history: list[Message]
    metadata: dict


class SendMessageConfiguration(TypedDict, total=False):
    """Options for ``SendMessage``; ``returnImmediately`` defaults to false."""

    acceptedOutputModes: list[str]
    historyLength: int
    returnImmediately: bool


class AgentSkill(TypedDict):
    """One skill an agent card advertises."""

    id: str
    name: str
    description: str
    tags: list[str]


class AgentInterface(TypedDict):
    """Where and how to reach an agent: here, always the asyncio broker binding."""

    url: str
    protocolBinding: str
    protocolVersion: str


class AgentCapabilities(TypedDict):
    """Optional protocol features; this binding supports none of them."""

    streaming: bool
    pushNotifications: bool
    extendedAgentCard: bool


class AgentCard(TypedDict):
    """An agent's published description."""

    name: str
    description: str
    supportedInterfaces: list[AgentInterface]
    version: str
    capabilities: AgentCapabilities
    defaultInputModes: list[str]
    defaultOutputModes: list[str]
    skills: list[AgentSkill]


class Envelope(TypedDict):
    """One request on the broker binding: who sends it, to whom, and the JSON-RPC body."""

    sender: str
    recipient: str
    serviceParameters: dict[str, str]
    rpc: dict


class A2AError(Exception):
    """An A2A or JSON-RPC error: ``code``, A2A error ``type_name`` and ``detail``."""

    def __init__(self, type_name: str, detail: str, code: int | None = None) -> None:
        super().__init__(detail)
        self.type_name = type_name
        self.detail = detail
        self.code = code if code is not None else ERROR_CODES.get(type_name, -32603)

    @classmethod
    def from_rpc(cls, error: dict) -> "A2AError":
        """Rebuild the error from a JSON-RPC ``error`` object, keeping its subclass."""
        data = error.get("data") or {}
        type_name = data.get("type", "InternalError")
        detail = data.get("detail", error.get("message", ""))
        error_class = {"BrokerClosedError": BrokerClosedError}.get(type_name, A2AError)
        return error_class(type_name, detail, error.get("code"))

    def __str__(self) -> str:
        return f"{self.type_name} ({self.code}): {self.detail}"


class A2ATimeoutError(A2AError):
    """The client stopped waiting for an answer (FR-133)."""

    def __init__(self, detail: str) -> None:
        super().__init__("A2ATimeoutError", detail)


class BrokerClosedError(A2AError):
    """The broker shut down before the request was answered."""

    def __init__(self, type_name: str = "BrokerClosedError", detail: str = "", code=None) -> None:
        super().__init__("BrokerClosedError", detail or "the A2A broker is closed", code)


class SkillRejected(Exception):
    """Raised by a skill handler for a request it will not take on (task REJECTED)."""


class SkillFailed(Exception):
    """Raised by a skill handler whose agent could not finish the work (task FAILED).

    ``artifacts`` are attached to the failed task, so the sender still gets the
    structured outcome (for example the ``DependencyFailure`` that stopped the work).
    """

    def __init__(self, message: str, artifacts: list[Artifact] | None = None) -> None:
        super().__init__(message)
        self.artifacts = list(artifacts or [])


def json_copy(value: Any) -> Any:
    """Return a deep copy made through JSON, so no Python object crosses the broker."""
    return json.loads(json.dumps(value))


def new_id() -> str:
    """Return a new identifier: a ``uuid4`` as 32 hex characters."""
    return uuid.uuid4().hex


def now_timestamp() -> str:
    """Return the current UTC time as ISO-8601 with milliseconds and a ``Z`` suffix."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def text_part(text: str) -> Part:
    """Return a plain-text Part."""
    return {"text": text, "mediaType": "text/plain"}


def data_part(value: Any) -> Part:
    """Return a JSON data Part."""
    return {"data": value, "mediaType": "application/json"}


def user_message(
    parts: list[Part],
    context_id: str,
    metadata: dict,
    reference_task_ids: list[str] | tuple[str, ...] = (),
) -> Message:
    """Return a request message (``ROLE_USER``) for ``context_id``."""
    return {
        "messageId": new_id(),
        "role": "ROLE_USER",
        "parts": list(parts),
        "contextId": context_id,
        "metadata": dict(metadata),
        "referenceTaskIds": list(reference_task_ids),
    }


def agent_message(text: str) -> Message:
    """Return an agent message holding one text part (used for status reasons)."""
    return {"messageId": new_id(), "role": "ROLE_AGENT", "parts": [text_part(text)]}


def artifact(name: str, parts: list[Part]) -> Artifact:
    """Return a named artifact."""
    return {"artifactId": new_id(), "name": name, "parts": list(parts)}


def new_task(context_id: str) -> Task:
    """Return a new task in ``TASK_STATE_SUBMITTED``."""
    return {
        "id": new_id(),
        "contextId": context_id,
        "status": {"state": "TASK_STATE_SUBMITTED", "timestamp": now_timestamp()},
        "artifacts": [],
        "history": [],
    }


def agent_skill(skill_id: str, name: str, description: str, tags: list[str]) -> AgentSkill:
    """Return one agent-card skill."""
    return {"id": skill_id, "name": name, "description": description, "tags": list(tags)}


def agent_card(name: str, description: str, skills: list[AgentSkill], version: str) -> AgentCard:
    """Return an agent card reachable over the broker binding at ``broker://<name>``."""
    return {
        "name": name,
        "description": description,
        "supportedInterfaces": [
            {
                "url": f"broker://{name}",
                "protocolBinding": BINDING_URN,
                "protocolVersion": A2A_VERSION,
            }
        ],
        "version": version,
        "capabilities": {
            "streaming": False, "pushNotifications": False, "extendedAgentCard": False
        },
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["application/json"],
        "skills": list(skills),
    }


def validate_part(part: Any) -> None:
    """Raise ``A2AError`` (-32602) unless ``part`` holds exactly one content member."""
    if not isinstance(part, dict):
        raise A2AError("InvalidParams", "a Part must be an object")
    if "kind" in part:
        raise A2AError("InvalidParams", "a Part must not have a 'kind' member (A2A 1.0)")
    present = [key for key in PART_CONTENT_KEYS if key in part]
    if len(present) != 1:
        raise A2AError("InvalidParams", f"a Part needs exactly one of {PART_CONTENT_KEYS}")
    if present[0] == "text" and not isinstance(part["text"], str):
        raise A2AError("InvalidParams", "a text Part's 'text' must be a string")


def validate_message(message: Any) -> None:
    """Raise ``A2AError`` (-32602) unless ``message`` is a valid A2A 1.0 Message."""
    if not isinstance(message, dict):
        raise A2AError("InvalidParams", "message must be an object")
    if "kind" in message:
        raise A2AError("InvalidParams", "a Message must not have a 'kind' member (A2A 1.0)")
    if not isinstance(message.get("messageId"), str) or not message["messageId"]:
        raise A2AError("InvalidParams", "message.messageId must be a non-empty string")
    if message.get("role") not in ROLES:
        raise A2AError("InvalidParams", f"message.role must be one of {ROLES}")
    parts = message.get("parts")
    if not isinstance(parts, list) or not parts:
        raise A2AError("InvalidParams", "message.parts must be a non-empty list")
    for part in parts:
        validate_part(part)
    if not isinstance(message.get("metadata", {}), dict):
        raise A2AError("InvalidParams", "message.metadata must be an object")
    references = message.get("referenceTaskIds", [])
    if not isinstance(references, list) or not all(isinstance(r, str) for r in references):
        raise A2AError("InvalidParams", "message.referenceTaskIds must be a list of strings")


def validate_envelope(envelope: Any) -> None:
    """Raise ``A2AError`` (-32602) unless ``envelope`` is a well-formed broker request."""
    if not isinstance(envelope, dict):
        raise A2AError("InvalidParams", "envelope must be an object")
    for key in ("sender", "recipient"):
        if not isinstance(envelope.get(key), str) or not envelope[key]:
            raise A2AError("InvalidParams", f"envelope.{key} must be a non-empty string")
    if not isinstance(envelope.get("serviceParameters", {}), dict):
        raise A2AError("InvalidParams", "envelope.serviceParameters must be an object")
    rpc = envelope.get("rpc")
    if not isinstance(rpc, dict) or rpc.get("jsonrpc") != "2.0":
        raise A2AError("InvalidParams", "envelope.rpc must be a JSON-RPC 2.0 request")
    if not isinstance(rpc.get("method"), str) or "id" not in rpc:
        raise A2AError("InvalidParams", "envelope.rpc needs a 'method' and an 'id'")
    if not isinstance(rpc.get("params", {}), dict):
        raise A2AError("InvalidParams", "envelope.rpc.params must be an object")


def rpc_request(method: str, params: dict) -> dict:
    """Return a JSON-RPC 2.0 request with a new id."""
    return {"jsonrpc": "2.0", "id": new_id(), "method": method, "params": params}


def rpc_result(rpc_id: Any, result: Any) -> dict:
    """Return a JSON-RPC 2.0 success response."""
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def rpc_error(rpc_id: Any, error: A2AError) -> dict:
    """Return a JSON-RPC 2.0 error response carrying the A2A error type and detail."""
    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "error": {
            "code": error.code,
            "message": error.type_name,
            "data": {"type": error.type_name, "detail": error.detail},
        },
    }


def find_text(message: Message) -> str:
    """Return the joined text of a message's text parts."""
    return "".join(part["text"] for part in message["parts"] if "text" in part)


def find_data(message: Message) -> Any:
    """Return the first data part's value, or ``None`` if the message has none."""
    return next((part["data"] for part in message["parts"] if "data" in part), None)


def artifact_data(task: Task, name: str) -> Any:
    """Return the data of the task's artifact called ``name``.

    Raises:
        A2AError: ``InvalidAgentResponseError`` if there is no such artifact or data part.
    """
    parts = [
        part
        for item in task.get("artifacts", [])
        if item.get("name") == name
        for part in item["parts"]
        if "data" in part
    ]
    if not parts:
        raise A2AError("InvalidAgentResponseError", f"task {task['id']} has no '{name}' artifact")
    return parts[0]["data"]
