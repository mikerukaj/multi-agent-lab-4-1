"""The asyncio A2A broker: per-agent inboxes, ordered workers, and request/reply futures.

``A2ABroker`` owns one asyncio event loop in a background thread (the team graph is
synchronous). Each registered agent gets an ``asyncio.Queue`` inbox and one worker
task that handles its messages one at a time, in arrival order, running the agent's
synchronous handler in a thread so the loop stays free. Every envelope and reply is
copied through JSON at the boundary, so agents never share Python objects, and every
request is logged as an ``A2AExchange``. ``A2AClient`` is the synchronous face a
sender uses. See specs/002-qa-feedback-loop-mcp/contracts/a2a-binding.md.
"""

import asyncio
import concurrent.futures
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from messaging.a2a import (
    A2A_VERSION,
    SUPPORTED_OUTPUT_MODES,
    TERMINAL_STATES,
    A2AError,
    A2ATimeoutError,
    AgentCard,
    Artifact,
    BrokerClosedError,
    Message,
    SkillFailed,
    SkillRejected,
    Task,
    agent_message,
    json_copy,
    new_task,
    now_timestamp,
    rpc_error,
    rpc_request,
    rpc_result,
    validate_envelope,
    validate_message,
    validate_part,
)

SkillHandler = Callable[[Message], list[Artifact]]
UNSUPPORTED = {
    "SendStreamingMessage": "UnsupportedOperationError",
    "SubscribeToTask": "UnsupportedOperationError",
    "GetExtendedAgentCard": "ExtendedAgentCardNotConfiguredError",
}
DEFAULT_PAGE_SIZE = 50
SHUTDOWN_SECONDS = 5.0


@dataclass
class _Endpoint:
    """One registered agent: its card, handler, inbox, task store and waiters."""

    card: AgentCard
    handler: SkillHandler
    inbox: asyncio.Queue
    tasks: dict[str, Task] = field(default_factory=dict)
    waiters: dict[str, asyncio.Future] = field(default_factory=dict)
    worker: asyncio.Task | None = None


class A2ABroker:
    """In-process A2A broker on its own event-loop thread. Use it as a context manager."""

    def __init__(self, request_timeout_seconds: float = 1800.0) -> None:
        self.request_timeout_seconds = request_timeout_seconds
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="a2a-broker", daemon=True
        )
        self._executor = concurrent.futures.ThreadPoolExecutor(thread_name_prefix="a2a-skill")
        self._endpoints: dict[str, _Endpoint] = {}
        self._log: list[dict] = []
        self._log_by_rpc: dict[Any, dict] = {}
        self._log_lock = threading.Lock()
        self._closed = False

    def __enter__(self) -> "A2ABroker":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def register(self, card: AgentCard, handler: SkillHandler) -> None:
        """Add an agent: an inbox and a worker that runs ``handler`` per message."""
        future = asyncio.run_coroutine_threadsafe(
            self._register(json_copy(card), handler), self._loop
        )
        future.result(SHUTDOWN_SECONDS)

    def client(self, sender: str) -> "A2AClient":
        """Return a synchronous client that sends on behalf of ``sender``."""
        return A2AClient(self, sender, self.request_timeout_seconds)

    def card(self, name: str) -> AgentCard:
        """Return a copy of a registered agent's card (``UnknownAgent`` otherwise)."""
        endpoint = self._endpoints.get(name)
        if endpoint is None:
            raise A2AError("UnknownAgent", f"no agent named {name!r} is registered")
        return json_copy(endpoint.card)

    def exchanges(self) -> list[dict]:
        """Return copies of every logged exchange, in the order requests arrived."""
        with self._log_lock:
            return json_copy(self._log)

    def task_id_for(self, rpc_id: Any) -> str | None:
        """Return the task a ``SendMessage`` request created, if it got that far."""
        with self._log_lock:
            entry = self._log_by_rpc.get(rpc_id)
            return entry["task_id"] if entry else None

    def request(self, envelope: dict, timeout: float | None = None) -> dict:
        """Deliver one envelope and wait for its JSON-RPC reply (low-level API).

        Raises:
            BrokerClosedError: if the broker is closed.
            TimeoutError: if no reply arrives within ``timeout`` seconds.
        """
        if self._closed:
            raise BrokerClosedError()
        future = asyncio.run_coroutine_threadsafe(self._handle(json_copy(envelope)), self._loop)
        return json_copy(future.result(timeout))

    def close(self) -> None:
        """Fail pending requests, stop the workers and the loop, and release the threads."""
        if self._closed:
            return
        self._closed = True
        if self._thread.is_alive():
            asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop).result(SHUTDOWN_SECONDS)
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(SHUTDOWN_SECONDS)
        if not self._loop.is_running():
            self._loop.close()
        self._executor.shutdown(wait=False, cancel_futures=True)

    async def _register(self, card: AgentCard, handler: SkillHandler) -> None:
        endpoint = _Endpoint(card=card, handler=handler, inbox=asyncio.Queue())
        endpoint.worker = asyncio.create_task(self._work(endpoint))
        self._endpoints[card["name"]] = endpoint

    async def _shutdown(self) -> None:
        for endpoint in self._endpoints.values():
            if endpoint.worker:
                endpoint.worker.cancel()
            for waiter in endpoint.waiters.values():
                if not waiter.done():
                    waiter.set_exception(BrokerClosedError())

    async def _handle(self, envelope: dict) -> dict:
        """Answer one envelope; protocol failures become JSON-RPC error replies."""
        rpc = envelope.get("rpc") if isinstance(envelope, dict) else None
        rpc_id = rpc.get("id") if isinstance(rpc, dict) else None
        entry = self._open_entry(envelope, rpc_id)
        try:
            validate_envelope(envelope)
            version = envelope.get("serviceParameters", {}).get("A2A-Version")
            if version != A2A_VERSION:
                raise A2AError(
                    "VersionNotSupportedError",
                    f"A2A-Version {version!r} is not supported; use {A2A_VERSION}",
                )
            endpoint = self._endpoints.get(envelope["recipient"])
            if endpoint is None:
                raise A2AError(
                    "UnknownAgent", f"no agent named {envelope['recipient']!r} is registered"
                )
            result = await self._dispatch(endpoint, rpc["method"], rpc.get("params", {}), entry)
            if entry["outcome"] == "pending" and rpc["method"] != "SendMessage":
                self._update(entry, outcome="ok")
            return rpc_result(rpc_id, result)
        except A2AError as error:
            self._update(entry, outcome=f"error:{error.code}")
            return rpc_error(rpc_id, error)
        finally:
            self._update(entry, finished_at=now_timestamp())

    async def _dispatch(self, endpoint: _Endpoint, method: str, params: dict, entry: dict) -> Any:
        if method == "SendMessage":
            return await self._send_message(endpoint, params, entry)
        if method == "GetTask":
            return self._get_task(endpoint, params)
        if method == "ListTasks":
            return self._list_tasks(endpoint, params)
        if method == "CancelTask":
            return self._cancel_task(endpoint, params)
        if method in UNSUPPORTED:
            raise A2AError(UNSUPPORTED[method], f"{method} is not supported by this binding")
        if "PushNotificationConfig" in method:
            raise A2AError(
                "PushNotificationNotSupportedError", "push notifications are not supported"
            )
        raise A2AError("MethodNotFound", f"unknown method {method!r}")

    async def _send_message(self, endpoint: _Endpoint, params: dict, entry: dict) -> dict:
        message = params.get("message")
        validate_message(message)
        configuration = params.get("configuration") or {}
        accepted = configuration.get("acceptedOutputModes")
        if accepted and not SUPPORTED_OUTPUT_MODES & set(accepted):
            raise A2AError("ContentTypeNotSupportedError", f"cannot produce any of {accepted}")
        task = new_task(message.get("contextId") or entry["sender"])
        task["history"] = [message]
        endpoint.tasks[task["id"]] = task
        done = asyncio.get_running_loop().create_future()
        endpoint.waiters[task["id"]] = done
        metadata = message.get("metadata", {})
        self._update(
            entry,
            skill_id=metadata.get("skillId"),
            context_id=task["contextId"],
            task_id=task["id"],
            reference_task_ids=list(message.get("referenceTaskIds", [])),
        )
        self._record_state(entry, task)
        await endpoint.inbox.put((task["id"], entry))
        if configuration.get("returnImmediately"):
            return {"task": json_copy(task)}
        await done
        return {"task": json_copy(task)}

    async def _work(self, endpoint: _Endpoint) -> None:
        """Handle the endpoint's messages one at a time, in arrival order."""
        while True:
            task_id, entry = await endpoint.inbox.get()
            task = endpoint.tasks[task_id]
            if task["status"]["state"] != "TASK_STATE_SUBMITTED":
                continue  # Canceled while queued.
            self._set_state(endpoint, task, entry, "TASK_STATE_WORKING")
            state, reason = await self._run_handler(endpoint, task)
            self._set_state(endpoint, task, entry, state, reason)

    async def _run_handler(self, endpoint: _Endpoint, task: Task) -> tuple[str, str | None]:
        """Run the handler in a thread; map its outcome to a terminal state and reason."""
        message = task["history"][0]
        skill = message.get("metadata", {}).get("skillId")
        skills = [item["id"] for item in endpoint.card["skills"]]
        if skill not in skills:
            return "TASK_STATE_REJECTED", f"unknown skill {skill!r}; this agent offers {skills}"
        loop = asyncio.get_running_loop()
        try:
            artifacts = await loop.run_in_executor(
                self._executor, endpoint.handler, json_copy(message)
            )
            task["artifacts"] = _checked_artifacts(artifacts)
        except SkillRejected as error:
            return "TASK_STATE_REJECTED", str(error)
        except SkillFailed as error:
            if error.artifacts:
                task["artifacts"] = _checked_artifacts(error.artifacts)
            return "TASK_STATE_FAILED", str(error)
        except A2AError as error:
            return "TASK_STATE_FAILED", error.detail
        except Exception as error:  # A handler bug must fail its task, not the broker.
            return "TASK_STATE_FAILED", f"{type(error).__name__}: {error}"
        return "TASK_STATE_COMPLETED", None

    def _get_task(self, endpoint: _Endpoint, params: dict) -> Task:
        task = json_copy(self._find_task(endpoint, params))
        history_length = params.get("historyLength")
        if isinstance(history_length, int) and history_length >= 0:
            task["history"] = task.get("history", [])[-history_length:] if history_length else []
        return task

    def _list_tasks(self, endpoint: _Endpoint, params: dict) -> dict:
        tasks = [
            task
            for task in endpoint.tasks.values()
            if params.get("contextId") in (None, task["contextId"])
            and params.get("status") in (None, task["status"]["state"])
        ]
        size = int(params.get("pageSize") or DEFAULT_PAGE_SIZE)
        start = int(params.get("pageToken") or 0)
        page = tasks[start:start + size]
        next_token = str(start + size) if start + size < len(tasks) else ""
        return {
            "tasks": json_copy(page),
            "totalSize": len(tasks),
            "pageSize": size,
            "nextPageToken": next_token,
        }

    def _cancel_task(self, endpoint: _Endpoint, params: dict) -> Task:
        task = self._find_task(endpoint, params)
        state = task["status"]["state"]
        if state != "TASK_STATE_SUBMITTED":
            raise A2AError("TaskNotCancelableError", f"task {task['id']} is {state}")
        with self._log_lock:
            entry = next((e for e in self._log if e["task_id"] == task["id"]), None)
        self._set_state(endpoint, task, entry, "TASK_STATE_CANCELED", "canceled while queued")
        return json_copy(task)

    def _find_task(self, endpoint: _Endpoint, params: dict) -> Task:
        task = endpoint.tasks.get(params.get("id"))
        if task is None:
            raise A2AError(
                "TaskNotFoundError", f"no task {params.get('id')!r} on {endpoint.card['name']}"
            )
        return task

    def _set_state(
        self,
        endpoint: _Endpoint,
        task: Task,
        entry: dict | None,
        state: str,
        reason: str | None = None,
    ) -> None:
        """Move ``task`` to ``state``; on a terminal state, log it and wake the sender."""
        task["status"] = {"state": state, "timestamp": now_timestamp()}
        if reason:
            task["status"]["message"] = agent_message(reason)
        if entry is not None:
            self._record_state(entry, task)
        if state in TERMINAL_STATES:
            if entry is not None:
                self._update(entry, outcome=state)
            waiter = endpoint.waiters.pop(task["id"], None)
            if waiter is not None and not waiter.done():
                waiter.set_result(None)

    def _open_entry(self, envelope: Any, rpc_id: Any) -> dict:
        envelope = envelope if isinstance(envelope, dict) else {}
        rpc = envelope.get("rpc") if isinstance(envelope.get("rpc"), dict) else {}
        with self._log_lock:
            entry = {
                "sequence": len(self._log) + 1,
                "sender": str(envelope.get("sender", "")),
                "recipient": str(envelope.get("recipient", "")),
                "method": str(rpc.get("method", "")),
                "skill_id": None,
                "context_id": None,
                "task_id": None,
                "reference_task_ids": [],
                "states": [],
                "outcome": "pending",
                "started_at": now_timestamp(),
                "finished_at": "",
            }
            self._log.append(entry)
            self._log_by_rpc[rpc_id] = entry
        return entry

    def _update(self, entry: dict, **changes: Any) -> None:
        with self._log_lock:
            entry.update(changes)

    def _record_state(self, entry: dict, task: Task) -> None:
        state = {"state": task["status"]["state"], "timestamp": task["status"]["timestamp"]}
        with self._log_lock:
            entry["states"].append(state)


def _checked_artifacts(artifacts: Any) -> list[Artifact]:
    """Validate a handler's artifacts and return a JSON copy of them."""
    if not isinstance(artifacts, list):
        raise A2AError(
            "InvalidAgentResponseError", "a skill handler must return a list of artifacts"
        )
    for item in artifacts:
        if not isinstance(item, dict) or not item.get("artifactId") or not item.get("parts"):
            raise A2AError(
                "InvalidAgentResponseError", "each artifact needs an artifactId and parts"
            )
        for part in item["parts"]:
            validate_part(part)
    return json_copy(artifacts)


class A2AClient:
    """Synchronous A2A client that sends on behalf of one agent (``sender``)."""

    def __init__(self, broker: A2ABroker, sender: str, timeout_seconds: float) -> None:
        self._broker = broker
        self.sender = sender
        self.timeout_seconds = timeout_seconds

    def send_message(
        self, recipient: str, message: Message, configuration: dict | None = None
    ) -> Task:
        """Send ``message`` and return the task (terminal unless ``returnImmediately``).

        Raises:
            A2AError: for a JSON-RPC error reply.
            A2ATimeoutError: if no answer arrives in time; a still-queued task is canceled.
        """
        params = {"message": message, "configuration": configuration or {}}
        return self._call(recipient, "SendMessage", params)["task"]

    def get_task(self, recipient: str, task_id: str, history_length: int | None = None) -> Task:
        """Return the recipient's task ``task_id``."""
        params = {"id": task_id}
        if history_length is not None:
            params["historyLength"] = history_length
        return self._call(recipient, "GetTask", params)

    def list_tasks(
        self, recipient: str, context_id: str | None = None, status: str | None = None
    ) -> dict:
        """Return the recipient's tasks, optionally filtered by context and state."""
        filters = (("contextId", context_id), ("status", status))
        params = {key: value for key, value in filters if value}
        return self._call(recipient, "ListTasks", params)

    def cancel_task(self, recipient: str, task_id: str) -> Task:
        """Cancel a task that is still queued."""
        return self._call(recipient, "CancelTask", {"id": task_id})

    def get_card(self, recipient: str) -> AgentCard:
        """Return the recipient's agent card (stands in for the well-known card URL)."""
        return self._broker.card(recipient)

    def exchanges(self) -> list[dict]:
        """Return copies of the whole run's exchange log (every sender, in order)."""
        return self._broker.exchanges()

    def _call(self, recipient: str, method: str, params: dict) -> Any:
        envelope = {
            "sender": self.sender,
            "recipient": recipient,
            "serviceParameters": {"A2A-Version": A2A_VERSION},
            "rpc": rpc_request(method, params),
        }
        try:
            response = self._broker.request(envelope, self.timeout_seconds)
        except TimeoutError as error:
            self._cancel_after_timeout(recipient, envelope["rpc"]["id"])
            raise A2ATimeoutError(
                f"{recipient} did not answer within {self.timeout_seconds:g} s"
            ) from error
        if "error" in response:
            raise A2AError.from_rpc(response["error"])
        return response["result"]

    def _cancel_after_timeout(self, recipient: str, rpc_id: str) -> None:
        task_id = self._broker.task_id_for(rpc_id)
        if task_id is None:
            return
        try:
            self.cancel_task(recipient, task_id)
        except A2AError:
            pass  # Already working or finished: it cannot be canceled, and that is expected.
