"""Process-owned workers; explicit live pause/continue, never checkpoint recovery."""
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading
import time
from uuid import uuid4
import weakref

import httpx
from pydantic import SecretStr

from convolab.llm import AbortSignal, CancelledWork, error_details, model_attempt, format_error
from convolab.simulation import run_study
from .admission import Lease
from .auth import VerifiedIdentity, canonical_uuid
from .limits import BoundedOutputStore, MAX_EXECUTION_SECONDS, check_study
from .policy import hosted_provider_attempt
from .settings import HostedError


class _DispatchTransport(httpx.BaseTransport):
    def __init__(self, transport, check):
        self.transport, self.check = transport, check
        self.cancelled = False

    def handle_request(self, request):
        try:
            self.check()  # Immediately before the HTTP send, including retries.
        except CancelledWork:
            self.cancelled = True
            raise
        return self.transport.handle_request(request)

    def close(self):
        self.transport.close()


class _LifecycleStore:
    """Delegate unchanged writers without a bound-method reference cycle."""
    def __init__(self, store, before_finalize):
        self._store, self._before_finalize = store, before_finalize

    def __getattr__(self, name):
        return getattr(self._store, name)

    def finalize(self):
        self._before_finalize()
        return self._store.finalize()


@dataclass(repr=False)
class _Job:
    lease: Lease
    abort: AbortSignal = field(default_factory=AbortSignal)
    done: threading.Event = field(default_factory=threading.Event)
    stop_heartbeat: threading.Event = field(default_factory=threading.Event)
    status: str = "running"
    progress: dict = field(default_factory=dict)
    result: object = None
    outcome_recorded: bool = False
    finish_attempted: bool = False
    hosting_reason: str | None = None
    finished_at: datetime | None = None
    worker: object = None
    started: float = 0
    checkpoint: object = None
    live_events: list = field(default_factory=list)
    live_context: dict = field(default_factory=dict)
    live_settings: dict = field(default_factory=dict)
    live_errors: list = field(default_factory=list)
    pause_requested: bool = False
    paused: threading.Event = field(default_factory=threading.Event)
    pause_wake: threading.Event = field(default_factory=threading.Event)
    finalizing: bool = False


class ExecutionCoordinator:
    def __init__(self, admission, output_root, *, request=None, transport_factory=None,
                 sleep=None, store_factory=BoundedOutputStore, heartbeat_seconds=20,
                 thread_factory=threading.Thread, max_seconds=MAX_EXECUTION_SECONDS, clock=time.monotonic,
                 checkpoint_factory=None):
        self.admission = admission
        self.output_root = Path(output_root)
        self.instance_id = str(uuid4())
        self._lock = threading.RLock()
        self._jobs = {}
        self._workspaces = {}
        self._request = request  # Offline test seam, never a visitor field.
        self._transport_factory = transport_factory
        self._sleep = sleep
        self._store_factory = store_factory
        self._heartbeat_seconds = heartbeat_seconds
        self._thread_factory = thread_factory
        self._max_seconds, self._clock = max_seconds, clock
        self._checkpoint_factory = checkpoint_factory

    def configure_storage(self, factory):
        with self._lock:
            if self._jobs:
                raise HostedError('Storage configuration requires a fresh server process.')
            self._checkpoint_factory = factory

    def attach(self, identity, workspace):
        workspace.require_owner(identity)
        with self._lock:
            refs = self._workspaces.setdefault(identity.owner_id, [])
            refs[:] = [r for r in refs if r() is not None]
            if not any(r() is workspace for r in refs):
                refs.append(weakref.ref(workspace))

    def _clear_keys(self, owner):
        # Plain workspace data only. No Streamlit imports/calls in any worker.
        with self._lock:
            for ref in self._workspaces.get(owner, ()):
                workspace = ref()
                if workspace is not None:
                    workspace.execution_finished(VerifiedIdentity(owner, SecretStr("")))

    def start(self, identity, nonce, workspace, *, demo=False):
        if not isinstance(identity, VerifiedIdentity):
            raise HostedError("Connect your private workspace before starting.")
        canonical_uuid(nonce)
        workspace.require_owner(identity)
        self.attach(identity, workspace)
        # One lock prevents the first accepted response and a duplicate response
        # from racing a process-local worker registration. DB serializes hosts.
        with self._lock, workspace._lock:
            # A locally settling worker still owns its inputs even after fencing.
            # Estimate and freeze the same editor revision. A rerun/import must
            # not replace it during a slow admission acknowledgement.
            available = not any(not job.done.is_set() for job in self._jobs.values())
            estimate = check_study(workspace.study(identity, demo=demo))
            lease = Lease(str(uuid4()), identity.owner_id, self.instance_id, str(uuid4()))
            answer = self.admission.admit(identity, nonce, lease, estimate["total"], available=available)
            if answer.outcome != "accepted":
                return {"outcome": answer.outcome, "execution_id": answer.execution_id}
            job = _Job(lease)
            job.started = self._clock()
            self._jobs[lease.execution_id] = job
            try:
                frozen = workspace.freeze(identity, demo=demo)
                job.live_settings = {k:getattr(frozen.study.run,k) for k in ('n_runs','max_turns','silence_visibility')}
                job.worker = self._thread_factory(target=self._run, args=(job, [frozen]),
                    name="convolab-hosted-execution", daemon=True)
                job.worker.start()
            except BaseException:
                job.status = "failed"
                job.hosting_reason = "start_failed"
                try:
                    job.outcome_recorded = self.admission.finish(lease, "failed")
                except HostedError:
                    pass
                self._clear_keys(identity.owner_id)
                job.finished_at = datetime.now(timezone.utc)
                job.done.set()
                raise HostedError("The execution could not start. No model request was sent. Enter keys again and choose Prepare to run again before another attempt.") from None
            return {"outcome": "accepted", "execution_id": lease.execution_id}

    def _ownership_fault(self, job):
        job.abort.signal(error_details("application") | {"hosting_reason": "ownership_lost"})
        job.abort.check()

    def _dispatch(self, job):
        self._check_time(job)
        job.abort.check()
        try:
            allowed = self.admission.dispatch(job.lease)
        except HostedError:
            allowed = False
        if not allowed:
            self._ownership_fault(job)
        self._check_time(job)
        job.abort.check()

    def _check_time(self, job):
        with self._lock:
            if not job.done.is_set() and self._clock() - job.started >= self._max_seconds:
                job.abort.signal(error_details("application") | {"hosting_reason": "time_limit"})

    @staticmethod
    def _control_state(job):
        if job.done.is_set():
            return 'finished'
        if job.abort.event.is_set():
            return 'stopping'
        if job.finalizing or job.finish_attempted:
            return 'finishing'
        if job.paused.is_set():
            return 'paused' if job.pause_requested else 'continuing'
        return 'pausing' if job.pause_requested else 'running'

    def _control_job(self, identity, execution_id):
        # Ownership/UUID checks happen before dispatch or any state mutation.
        self.observe(identity, execution_id)
        with self._lock:
            job = self._jobs[execution_id]
            if self._control_state(job) in {'finished', 'finishing', 'stopping'}:
                raise HostedError('This study is finishing or has ended. It cannot be paused or continued.')
        # Control checks must not signal a new failure on an execution that
        # finished while its ownership request was in flight.
        self._check_time(job)
        try:
            allowed = self.admission.dispatch(job.lease)
        except HostedError:
            allowed = False
        with self._lock:
            if self._control_state(job) in {'finished', 'finishing', 'stopping'}:
                raise HostedError('This study is finishing or has ended. It cannot be paused or continued.')
            if not allowed:
                job.abort.signal(error_details('application') | {'hosting_reason': 'ownership_lost'})
                job.pause_wake.set()
                raise HostedError('Execution ownership is unavailable. Refresh results; this execution cannot be continued.')
        self._check_time(job)
        if job.abort.event.is_set():
            raise HostedError('This study is stopping and cannot be continued.')
        return job

    def pause(self, identity, execution_id):
        job = self._control_job(identity, execution_id)
        with self._lock:
            state = self._control_state(job)
            if state in {'finished', 'finishing', 'stopping', 'continuing'}:
                raise HostedError('The study changed before pausing. Check its current progress.')
            job.pause_requested = True
            return self._control_state(job)

    def continue_study(self, identity, execution_id):
        job = self._control_job(identity, execution_id)
        with self._lock:
            state = self._control_state(job)
            if state == 'pausing':
                raise HostedError('Wait until the current response step finishes and the study says Paused.')
            if state in {'finished', 'finishing', 'stopping'}:
                raise HostedError('This study has ended and cannot be continued.')
            # Repeated Continue clicks are harmless; they never start a worker.
            if state == 'paused':
                job.pause_requested = False
                job.pause_wake.set()
            return self._control_state(job)

    def _pause_boundary(self, job, checkpoint=None):
        with self._lock:
            if not job.pause_requested:
                return
        self._dispatch(job)
        # Called only from the sequential coordinator, before a new response
        # step. All prior state/survey futures have settled and writes finished.
        if checkpoint:
            checkpoint()
        self._check_time(job)
        job.abort.check()
        with self._lock:
            job.paused.set()
        try:
            while True:
                self._check_time(job)  # Paused time counts toward the same limit.
                job.abort.check()
                with self._lock:
                    if not job.pause_requested:
                        break
                job.pause_wake.wait(.25)
                job.pause_wake.clear()
        finally:
            with self._lock:
                job.paused.clear()
        self._dispatch(job)

    def _begin_finalize(self, job):
        self._check_time(job)
        with self._lock:
            job.finalizing = True

    def _heartbeat(self, job):
        while not job.stop_heartbeat.wait(max(0, min(self._heartbeat_seconds,
                self._max_seconds - (self._clock() - job.started)))):
            if job.finish_attempted:
                return  # Research workers have settled; terminal RPC owns this phase.
            self._check_time(job)
            if job.abort.event.is_set():
                return
            try:
                if self.admission.renew(job.lease):
                    continue
            except HostedError:
                pass
            if job.stop_heartbeat.is_set() or job.finish_attempted:
                return  # A terminal row correctly rejects a late heartbeat.
            job.abort.signal(error_details("application") | {"hosting_reason": "ownership_lost"})
            return

    def _run(self, job, inputs):
        frozen = inputs.pop()  # Thread arguments never retain credential objects.
        heartbeat = None
        stores = []
        try:
            self._dispatch(job)
            heartbeat = threading.Thread(target=self._heartbeat, args=(job,),
                                         name="convolab-hosted-heartbeat", daemon=True)
            heartbeat.start()

            def request(agent, parameters, messages):
                self._dispatch(job)
                try:
                    if self._request is not None:
                        return self._request(agent, parameters, messages)
                    if agent.model == "mock":
                        return model_attempt(agent, parameters, messages)
                    transport = self._transport_factory() if self._transport_factory else httpx.HTTPTransport(retries=0)
                    guarded = _DispatchTransport(transport, lambda: self._dispatch(job))
                    try:
                        return hosted_provider_attempt(agent, parameters, messages, transport=guarded)
                    finally:
                        if guarded.cancelled:
                            # SDK translation cannot invent an audit attempt for
                            # a send denied at the final ownership boundary.
                            job.abort.check()
                finally:
                    # Signal without raising: sent answers still receive their
                    # real audit row before the original coordinator sees abort.
                    self._check_time(job)

            def progress(event):
                # Main-stage notification is after state publication and settled
                # surveys. Never snapshot a candidate inside append_jsonl(states).
                if stores and event.get('event') == 'stage' and event.get('call_type') == 'main':
                    checkpoint = getattr(stores[0], 'checkpoint', None)
                    if checkpoint:
                        checkpoint()
                # Bounded observation, detached copies; no transcript/secret/path.
                allowed = {"event", "run_id", "number", "n_runs", "call_type", "turn", "status", "end_reason"}
                with self._lock:
                    job.progress = {k: deepcopy(v) for k, v in event.items() if k in allowed}
                    if event.get('event') == 'run_started':
                        job.live_events.clear()
                        job.live_context.clear()
                    job.live_context.update(job.progress)
                    if event.get('event') == 'turn':
                        job.live_events.append({k:deepcopy(event[k]) for k in
                            ('run_id','turn','speaker','passed','public_event_index','text')})
                    if event.get('error_details'):
                        job.live_errors.append(format_error(event['error_details'],folder='the private retained checkpoint'))
                    elif event.get('error'):
                        job.live_errors.append(str(event['error']))
                if event.get('event') in {'run_started', 'stage'}:
                    # Speech, state batches and survey batches retain their
                    # original order. A pause never blocks an individual seat
                    # inside a parallel batch or repeats a finished call.
                    saved = event.get('event') == 'run_started' or event.get('call_type') == 'main'
                    checkpoint = None if saved or not stores else getattr(stores[0], 'checkpoint', None)
                    self._pause_boundary(job, checkpoint)

            def store_factory(*args):
                # Final lifecycle metadata sees the same time-limit cause,
                # even if the watchdog has not been scheduled yet.
                if self._checkpoint_factory:
                    from .checkpoints import CheckpointStore
                    job.checkpoint = self._checkpoint_factory()
                    store = CheckpointStore(*args, lease=job.lease, sink=job.checkpoint)
                else:
                    store = self._store_factory(*args)
                stores.append(store)
                return _LifecycleStore(store, lambda: self._begin_finalize(job))

            kwargs = {} if self._sleep is None else {"sleep": self._sleep}
            job.result = run_study(frozen.execution_study(), self.output_root / job.lease.owner_id / job.lease.execution_id,
                environment={}, request=request, progress=progress, store_factory=store_factory,
                abort_signal=job.abort, additional_secrets=frozen.redaction_secrets, **kwargs)
            status = "complete" if job.result.status == "completed" else "failed"
            # A heartbeat can fail just as the engine finishes. Never publish
            # successful hosting completion after losing dispatch ownership.
            if job.abort.event.is_set():
                status = "failed"
            job.finish_attempted = True
            job.outcome_recorded = self.admission.finish(job.lease, status)
            job.status = status if job.outcome_recorded else "unconfirmed"
        except BaseException:
            job.status = "failed"
            if not job.abort.event.is_set():
                job.abort.signal(error_details("application") | {"hosting_reason":
                    "ownership_unconfirmed" if job.finish_attempted else "worker_failed"})
            if not job.finish_attempted:
                try:
                    job.finish_attempted = True
                    job.outcome_recorded = self.admission.finish(job.lease, "failed")
                except HostedError:
                    pass
            if not job.outcome_recorded:
                # An unknown acknowledgement is not proof of abandonment.
                # Only durable lease-expiry reconciliation marks interruption.
                job.status = "unconfirmed"
        finally:
            job.hosting_reason = (job.abort.cause or {}).get("hosting_reason")
            job.stop_heartbeat.set()
            if heartbeat is not None:
                heartbeat.join()  # No heartbeat retaining keys/owner after done.
            # run_study has settled all submitted state/survey workers and files.
            frozen = None
            inputs.clear()
            stores.clear()
            self._clear_keys(job.lease.owner_id)
            with self._lock:
                job.pause_requested = False
                job.paused.clear()
                job.pause_wake.set()
                job.finished_at = datetime.now(timezone.utc)
                job.done.set()

    def observe(self, identity, execution_id):
        if not isinstance(identity, VerifiedIdentity):
            raise HostedError("Connect your private workspace before viewing progress.")
        canonical_uuid(execution_id)
        with self._lock:
            job = self._jobs.get(execution_id)
            if job is None or job.lease.owner_id != identity.owner_id:
                raise HostedError("This execution is not owned by this running server. View My results; it will not resume automatically.")
            if job.finished_at and job.finished_at + timedelta(hours=24) <= datetime.now(timezone.utc):
                raise HostedError("The requested private item is unavailable.")
            return {"execution_id": execution_id, "status": job.status,
                    "outcome_recorded": job.outcome_recorded, "done": job.done.is_set(),
                    "hosting_reason": job.hosting_reason, "progress": deepcopy(job.progress),
                    "control_state": self._control_state(job),
                    "save_status": job.checkpoint.save_status if job.checkpoint else 'unavailable',
                    "n_runs_finalized": job.result.n_runs_finalized if job.result else 0,
                    "n_failed_runs": job.result.n_failed_runs if job.result else 0}

    def wait(self, identity, execution_id, timeout=30):
        self.observe(identity, execution_id)
        return self._jobs[execution_id].done.wait(timeout)

    def read_live(self, identity, execution_id):
        """Detached live display, guarded by the same owner/fence as execution.

        Terminal research is read through owned cloud checkpoints instead. No
        local path, study, persona, prompt, response body or key is exposed.
        """
        self.observe(identity, execution_id)
        with self._lock:
            job = self._jobs[execution_id]
            if job.done.is_set():
                raise HostedError('Open the retained checkpoint in My results to inspect this finished execution.')
            lease = job.lease
        if self.admission.dispatch(lease) is not True:
            raise HostedError('Live ownership is unavailable. Refresh My results; no work will resume automatically.')
        with self._lock:
            result = deepcopy({'events':job.live_events,'context':job.live_context,
                               'settings':job.live_settings,'errors':job.live_errors})
        if self.admission.dispatch(lease) is not True:
            raise HostedError('Live ownership is unavailable. Refresh My results; no work will resume automatically.')
        return result
