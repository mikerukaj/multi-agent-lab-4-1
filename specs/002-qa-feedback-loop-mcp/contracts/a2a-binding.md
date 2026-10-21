# Contract: A2A 1.0 over the asyncio Broker (custom binding)

**Binding identifier**: `urn:multi-agent-lab:a2a-binding:asyncio-broker:1.0`
**A2A version**: 1.0 (a2a-protocol.org/latest/specification)
**Implemented in**: `messaging/a2a.py` (types, builders, validators, errors),
`messaging/broker.py` (`A2ABroker`, `A2AClient`), `messaging/endpoints.py` (agent cards,
skill handlers). The design is in [research.md](../research.md) R17–R21.

This file is the binding documentation that A2A §12 requires of a custom binding.

## 1. Transport and envelope

The broker is in-process. Requests travel through per-recipient `asyncio.Queue` inboxes
on one event loop running in a background thread. Every request is one **envelope**:

```json
{
  "sender": "coder",
  "recipient": "qa-debugger",
  "serviceParameters": {"A2A-Version": "1.0"},
  "rpc": {"jsonrpc": "2.0", "id": "7f3c…", "method": "SendMessage", "params": { … }}
}
```

The reply is the JSON-RPC 2.0 response object, `{"jsonrpc": "2.0", "id": "7f3c…",
"result": …}` or `{…, "error": {…}}`. The broker copies the envelope on the way in and
the reply on the way out with `json.loads(json.dumps(x))`, so no Python object is shared
between sender and recipient.

## 2. Data mappings (§12: data types, enums, timestamps, binary)

| Concern | Mapping |
|---------|---------|
| Messages and objects | ProtoJSON, with camelCase member names exactly as in A2A 1.0. No `kind` member is ever emitted. A type is identified by which member is present (Part: `text` / `data` / `raw` / `url`; response: `task` / `message`) |
| Enums | Full enum value names as strings: `TASK_STATE_COMPLETED`, `ROLE_USER` |
| Timestamps | ISO-8601 UTC with millisecond precision and a `Z` suffix: `2026-10-19T14:03:22.418Z` |
| Binary | `raw` is base64. Not used by this project's skills |
| IDs | `messageId`, task `id`, `artifactId` and JSON-RPC `id` are `uuid.uuid4().hex`. `contextId` is the team `run_id` |
| Skill payloads | The *contents* of a `data` part are application data, not A2A protocol objects. They use the project's snake_case field names exactly as defined in [data-model.md](../data-model.md) (`QAContext`, `FinalReportRequest`, `FixRequest`, `QAIterationResult`, `QAReport`, `CoderFix`). Only A2A protocol objects (Message, Task, Part, Artifact, AgentCard, …) use camelCase |

## 3. Service parameters (§12: service parameters)

They travel in `serviceParameters` (a JSON object of string → string), which stands in
for HTTP headers. Multiple values are comma-separated in one string.

| Name | Rule |
|------|------|
| `A2A-Version` | Required. It must be `1.0`. If it is missing or different, the reply is `VersionNotSupportedError` |

No other names are reserved. Unknown names are ignored.

## 4. Operations (§12: all §3 operations)

| Method | Supported | Behavior |
|--------|:---------:|----------|
| `SendMessage` | ✅ | Validates the message. Creates a `Task` in `TASK_STATE_SUBMITTED` and queues it for the recipient's worker. Returns `{"task": …}`: the submitted task if `configuration.returnImmediately` is true, otherwise the terminal task |
| `GetTask` | ✅ | `params: {"id", "historyLength"?}` → the `Task`, or `TaskNotFoundError` |
| `ListTasks` | ✅ | A core A2A 1.0 operation (spec §5.3, §9.4.4; confirmed 2026-10-19). `params: {"contextId"?, "status"?, "pageSize"?, "pageToken"?}` → `{"tasks", "totalSize", "pageSize", "nextPageToken"}` for that recipient. `pageSize` defaults to 50; `pageToken` is an offset string, and `""` means the last page |
| `CancelTask` | ✅ | `params: {"id"}`. A `SUBMITTED` task is removed from the queue and becomes `CANCELED`. A `WORKING` or terminal task gets `TaskNotCancelableError` |
| `SendStreamingMessage`, `SubscribeToTask` | ❌ | `UnsupportedOperationError`. Cards declare `capabilities.streaming: false` |
| Push-notification config operations | ❌ | `PushNotificationNotSupportedError`. Cards declare `capabilities.pushNotifications: false` |
| `GetExtendedAgentCard` | ❌ | `ExtendedAgentCardNotConfiguredError`. Cards declare `capabilities.extendedAgentCard: false` |
| Agent card discovery | ✅ | `A2AClient.get_card(name)` returns the registered card, standing in for the well-known URL in HTTP bindings |

**Task lifecycle**:
`SUBMITTED → WORKING → COMPLETED | FAILED | REJECTED`, or `SUBMITTED → CANCELED`. A
failed or rejected task's `status.message` is an agent `Message` with one `text` part
giving the reason.

## 5. Errors (§12: error mapping)

These are native JSON-RPC error objects:
`{"code": int, "message": str, "data": {"type": "<A2A error name>", "detail": str}}`.

| A2A error | Code |
|-----------|------|
| `TaskNotFoundError` | -32001 |
| `TaskNotCancelableError` | -32002 |
| `PushNotificationNotSupportedError` | -32003 |
| `UnsupportedOperationError` | -32004 |
| `ContentTypeNotSupportedError` | -32005 |
| `InvalidAgentResponseError` | -32006 |
| `ExtendedAgentCardNotConfiguredError` | -32007 (confirmed against spec §5.4, 2026-10-19) |
| `ExtensionSupportRequiredError` | -32008 (confirmed against spec §5.4, 2026-10-19) |
| `VersionNotSupportedError` | -32009 |
| Invalid envelope or params, unknown recipient | -32602 Invalid params (`data.type` = `InvalidParams` / `UnknownAgent`) |
| Unknown method | -32601 Method not found |
| Broker internal failure | -32603 Internal error |

`ContentTypeNotSupportedError` is returned when `acceptedOutputModes` excludes both
`application/json` and `text/plain`.

## 6. Streaming and authentication (§12)

- **Streaming**: not supported, and declared in every card (see §4).
- **Authentication**: none. Both agents live in one process started by one user, which
  is the trust boundary. Cards omit `securitySchemes` and `security`. Any other process
  or network peer is out of scope (spec Assumption "A2A scope").

## 7. Agent cards

```json
{
  "name": "qa-debugger",
  "description": "Writes and runs pytest tests for a program, reviews its code, and returns a fix instruction list.",
  "supportedInterfaces": [{"url": "broker://qa-debugger",
                           "protocolBinding": "urn:multi-agent-lab:a2a-binding:asyncio-broker:1.0",
                           "protocolVersion": "1.0"}],
  "version": "0.3.0",
  "capabilities": {"streaming": false, "pushNotifications": false, "extendedAgentCard": false},
  "defaultInputModes": ["text/plain", "application/json"],
  "defaultOutputModes": ["application/json"],
  "skills": [
    {"id": "review-program", "name": "Review program",
     "description": "Run one QA iteration: tests, sandboxed run, review, verdict.",
     "tags": ["qa", "pytest", "review"]},
    {"id": "final-report", "name": "Final report",
     "description": "Summarise the run's QA iterations and unresolved findings.",
     "tags": ["qa", "report"]}
  ]
}
```

The `coder` card has the same shape, with the one skill
`{"id": "fix-program", "name": "Fix program", "description": "Apply a fix instruction list to the workspace and answer each finding.", "tags": ["coder", "fix"]}`.

Every card's `version` is `messaging.__version__` (`"0.3.0"`, increment 3 of the
project, pre-1.0). It is the single version constant in the repository. The MCP server's
`Server("file-tools", version=…)` uses it too.

## 8. Skill payloads

Each request `Message` has `role: "ROLE_USER"` and
`metadata: {"skillId": …, "sender": …}`. It carries one `text` part (a readable
summary) and one `data` part (`mediaType: "application/json"`). Each successful `Task`
has exactly one artifact holding one `data` part.

| Skill | Request `data` part | Artifact (`name`) | Artifact `data` |
|-------|--------------------|-------------------|-----------------|
| `review-program` | `QAContext` ([qa-agent-api.md](qa-agent-api.md)), with `workspace` as a string | `qa-iteration` | `QAIterationResult` as JSON (`findings` match [qa-findings.schema.json](qa-findings.schema.json)) |
| `final-report` | `FinalReportRequest`: `{"run_id", "workspace", "status", "end_reason", "max_iterations", "iterations"}` | `qa-report` | `QAReport` |
| `fix-program` | `FixRequest`: `{"workspace", "answer_ids": [...], "findings": [QAFinding…], "iteration": k}` (`text` = `render_fix_brief(...)`) | `coder-fix` | `CoderFix` |

How agent outcomes map to task states:

| Agent outcome | Task state |
|---------------|-----------|
| QA `error` set | `FAILED` (status message = the error) |
| QA result without error | `COMPLETED` |
| Coder fix `refused` / `error` | `FAILED` |
| Coder fix `completed` / `step_limit` | `COMPLETED` (`CoderFix.hit_step_cap` records the step cap) |
| `skillId` not on the card, or the payload fails validation | `REJECTED` (status message names the problem) |
| An exception escapes the handler | `FAILED` (`"<Type>: <message>"`) |

## 9. Sample exchange (§12: samples)

The request (the `qa` node, sent on behalf of the Coder):

```json
{"sender": "coder", "recipient": "qa-debugger", "serviceParameters": {"A2A-Version": "1.0"},
 "rpc": {"jsonrpc": "2.0", "id": "a1", "method": "SendMessage", "params": {
   "message": {"messageId": "m1", "role": "ROLE_USER", "contextId": "20261019-140301-ab12cd",
     "metadata": {"skillId": "review-program", "sender": "coder"},
     "parts": [{"text": "Program ready for QA review #2.", "mediaType": "text/plain"},
               {"data": {"iteration": 2, "workspace": "/…/workspace/20261019-140301-ab12cd", "…": "…"},
                "mediaType": "application/json"}]},
   "configuration": {"acceptedOutputModes": ["application/json"]}}}}
```

The reply:

```json
{"jsonrpc": "2.0", "id": "a1", "result": {"task": {
  "id": "t9", "contextId": "20261019-140301-ab12cd",
  "status": {"state": "TASK_STATE_COMPLETED", "timestamp": "2026-10-19T14:05:10.022Z"},
  "artifacts": [{"artifactId": "r1", "name": "qa-iteration",
                 "parts": [{"data": {"verdict": "fix_needed", "findings": ["…"]},
                            "mediaType": "application/json"}]}]}}}
```

The error case (wrong version):

```json
{"jsonrpc": "2.0", "id": "a2", "error": {"code": -32009, "message": "Unsupported A2A version",
  "data": {"type": "VersionNotSupportedError", "detail": "A2A-Version '0.3' is not supported; use 1.0"}}}
```

## 10. Python API

```python
class A2AError(Exception):            # carries code, error type name, detail
class A2ATimeoutError(A2AError): ...  # client gave up waiting (FR-133)
class BrokerClosedError(A2AError): ...

class A2ABroker:
    def __init__(self, request_timeout_seconds: float = 1800.0) -> None: ...  # A2A_REQUEST_TIMEOUT
    def __enter__(self) -> "A2ABroker": ...  # starts the loop thread
    def __exit__(self, *exc) -> None: ...    # close()
    def register(self, card: AgentCard, handler: SkillHandler) -> None: ...
    def client(self, sender: str) -> "A2AClient": ...
    def exchanges(self) -> list[A2AExchange]: ...  # copies of the whole run's log, in order
    def close(self) -> None: ...

SkillHandler = Callable[[Message], list[Artifact]]  # sync; runs via asyncio.to_thread;
                                                     # raise SkillRejected / SkillFailed to set the state

class A2AClient:
    def send_message(self, recipient: str, message: Message,
                     configuration: SendMessageConfiguration | None = None) -> Task: ...
    def get_task(self, recipient: str, task_id: str) -> Task: ...
    def cancel_task(self, recipient: str, task_id: str) -> Task: ...
    def get_card(self, recipient: str) -> AgentCard: ...
    def exchanges(self) -> list[A2AExchange]: ...   # same as A2ABroker.exchanges(): every
                                                     # exchange in the run (not only this
                                                     # sender's), as copies. Lets graph nodes
                                                     # refresh TeamState.a2a_log without
                                                     # holding the broker
    # Each raises A2AError for a JSON-RPC error reply, and A2ATimeoutError after the timeout
```

`messaging/endpoints.py`:

```python
def qa_card(version: str = __version__) -> AgentCard      # from messaging import __version__
def coder_card(version: str = __version__) -> AgentCard
def qa_handler(qa: QAAgent) -> SkillHandler        # dispatches on metadata.skillId
def coder_handler(coder: CoderAgent) -> SkillHandler
def register_team_agents(broker: A2ABroker, coder, qa, version: str = __version__) -> None
# Duck-typed: imports nothing from agents.* (SC-110). Handlers pass data parts through as
# QAContext / FinalReportRequest / FixRequest dicts and call qa.run_iteration(...),
# qa.final_report(...), coder.run(...); results are read via .error / .to_dict() / attributes.
```
