"""Synchronized, flushed audit data and coordinator-owned lifecycle records."""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from importlib.metadata import distributions
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
from uuid import uuid4

from filelock import FileLock, SoftFileLock, Timeout

from . import __version__
from .config import FrozenExecution, dump_study_yaml
from .llm import AbortSignal, Redactor, error_details, format_error, utc_now
from .json_protocol import strict_json_loads
from .prompts import PromptBuilder, PublicEvent

CSV_COLUMNS = {
    "agents": "run_id seat agent persona_id model ideology age race_ethnicity gender education party openness conscientiousness extraversion agreeableness neuroticism role_instructions".split(),
    "transcripts": "run_id turn public_message_index public_event_index event_type seat speaker model passed text timestamp".split(),
    "responses": "run_id seat agent model phase after_turn public_event_index state_version call_id scale item_id item_position response valid timestamp".split(),
    "reactions": "run_id seat agent model phase after_turn public_event_index state_version call_id reaction valid timestamp".split(),
    "runs": "run_id status n_turns n_passes end_reason speaker_order agent_names started_at finished_at error".split(),
}


class OutputError(OSError):
    """Output failure already signaled to the shared execution abort event."""
    def __init__(self, message, *, folder=None):
        super().__init__(message)
        self.folder = folder


def safe_study_name(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", value).rstrip(" .")
    if not value:
        value = "study"
    if re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", value):
        value = "_" + value
    # Keep long names within practical Windows component/path limits.
    return value[:80].rstrip(" .") or "study"


def native_lock(path: Path):
    lock = FileLock(str(path), timeout=0)
    if isinstance(lock, SoftFileLock):
        raise OutputError("This system has no supported operating-system-backed output lock.")
    return lock


def _atomic_text(path: Path, text: str):
    if os.name == "nt":
        return _windows_atomic_text(path, text)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="",
                                         dir=path.parent, prefix="." + path.name + "-",
                                         suffix=".tmp", delete=False) as file:
            name = Path(file.name)
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
    finally:
        if name is not None and name.exists():
            name.unlink()


def _windows_path(value: Path) -> str:
    value = str(value.resolve())
    if value.startswith("\\\\?\\"):
        return value
    return "\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value


def open_output(path: Path, *, binary=False, newline=None):
    """Read research outputs while allowing the coordinator's atomic rename.

    Windows readers explicitly share delete access. This grants no write
    permission and changes no ACLs; external non-sharing locks still fail.
    """
    path = Path(path)
    if os.name != "nt":
        return path.open("rb") if binary else path.open("r", encoding="utf-8", newline=newline)
    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    close.restype = wintypes.BOOL
    handle = create(_windows_path(path), 0x80000000, 7, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        close(handle)
        raise
    try:
        return os.fdopen(fd, "rb") if binary else os.fdopen(fd, "r", encoding="utf-8", newline=newline)
    except BaseException:
        os.close(fd)
        raise


def read_output_text(path: Path) -> str:
    with open_output(path) as file:
        return file.read()


def _windows_atomic_text(path: Path, text: str):
    """One flushed rename with POSIX replacement semantics on Windows.

    The native temporary handle owns DELETE access from creation until rename,
    preventing a close/reopen race. Sharing target readers keep their old
    complete file; new readers get the new complete file. No retry, fallback,
    read-only override, truncation of the target or permission changes.
    """
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create.restype = wintypes.HANDLE
    rename = kernel.SetFileInformationByHandle
    rename.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    rename.restype = wintypes.BOOL
    close = kernel.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    close.restype = wintypes.BOOL

    temporary = path.parent / ("." + path.name + "-" + uuid4().hex + ".tmp")
    # GENERIC_WRITE | DELETE; all share modes; CREATE_NEW; normal attributes.
    handle = create(_windows_path(temporary), 0x40010000, 7, None, 1, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
        except BaseException:
            close(handle)
            raise
        try:
            file = os.fdopen(fd, "w", encoding="utf-8", newline="")
        except BaseException:
            os.close(fd)
            raise
        with file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
            name = _windows_path(path).encode("utf-16-le", errors="surrogatepass")
            class RenameInfo(ctypes.Structure):
                _fields_ = [("Flags", wintypes.DWORD), ("RootDirectory", wintypes.HANDLE),
                            ("FileNameLength", wintypes.DWORD),
                            ("FileName", wintypes.WCHAR * (len(name) // 2 + 1))]
            info = RenameInfo()
            info.Flags = 3  # REPLACE_IF_EXISTS | POSIX_SEMANTICS; preserve ACLs/read-only rules.
            info.FileNameLength = len(name)
            ctypes.memmove(ctypes.addressof(info) + RenameInfo.FileName.offset, name, len(name))
            if not rename(handle, 22, ctypes.byref(info), ctypes.sizeof(info)):  # FileRenameInfoEx
                raise ctypes.WinError(ctypes.get_last_error())
    finally:
        if temporary.exists():
            temporary.unlink()


def _csv_text(columns, rows):
    file = io.StringIO(newline="")
    writer = csv.DictWriter(file, fieldnames=columns)
    writer.writeheader()
    writer.writerows(rows)
    return file.getvalue()


class OutputStore:
    """The coordinator owns this object; workers may append attempt audit only."""
    def __init__(self, root: Path, frozen: FrozenExecution, generation_parameters,
                 abort: AbortSignal, redactor: Redactor):
        self.abort, self.redactor = abort, redactor
        self._mutex = threading.RLock()
        self._files = {}
        self._writers = {}
        self._runs = {}
        self._transcripts = {}
        self._models = {}
        self._closed = False
        self._lock = None
        self.folder = None
        folder_created = False
        self.frozen = frozen
        execution_id = str(uuid4())
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        self.metadata = {
            "execution_id": execution_id, "status": "running",
            "n_runs_requested": frozen.study.run.n_runs, "n_runs_finalized": 0,
            "started_at": utc_now(), "finished_at": None, "error": None, "error_details": None,
            "app_version": __version__, "python_version": sys.version.split()[0],
            "dependency_versions": {d.metadata["Name"]: d.version for d in distributions()},
            "generation_parameters": [p.to_dict() for p in generation_parameters],
            "persona_dataset": dict(frozen.persona_dataset),
        }
        try:
            root = Path(root).resolve()
            root.mkdir(parents=True, exist_ok=True)
            self.folder = root / f"{safe_study_name(redactor.text(frozen.study.study_name))}_{timestamp}_{execution_id}"
            self.folder.mkdir(exist_ok=False)
            folder_created = True
            self._lock = native_lock(self.folder / ".execution.lock")
            self._lock.acquire(timeout=0)
            (self.folder / "transcripts").mkdir()
            self._atomic("study.yaml", redactor.text(dump_study_yaml(frozen.study)))
            applicable = ["agents", "transcripts", "responses"]
            if frozen.study.run.inner_reaction:
                applicable.append("reactions")
            for name in applicable:
                file = (self.folder / f"{name}.csv").open("x", encoding="utf-8", newline="")
                self._files[name] = file
                self._writers[name] = csv.DictWriter(file, fieldnames=CSV_COLUMNS[name])
                self._writers[name].writeheader()
                file.flush()
            for name in ["calls"] + (["states"] if frozen.study.run.private_state else []):
                self._files[name] = (self.folder / f"{name}.jsonl").open("x", encoding="utf-8", newline="")
                self._files[name].flush()
            self._save_runs()
            self._save_metadata()  # published running only after every applicable file exists
        except Exception as error:
            self._fault()
            self._close_best_effort()
            if self._lock:
                self._lock.release()
            raise OutputError("The initial output files could not be established; no model requests were sent.",
                              folder=self.folder if folder_created else None) from error

    def _fault(self, record=None):
        details = error_details("storage", meta=record or {})
        for key in ("seat", "agent", "model", "call_type", "call_id"):
            if record:
                details[key] = record.get(key)
        if record and "uncommitted_state_version" in record:
            details["uncommitted_state_version"] = record["uncommitted_state_version"]
        self.abort.signal(self.redactor.value(details))

    def _atomic(self, name, text):
        safe = self.redactor.text(text)
        if name.startswith("transcripts/"):
            # §8.1 mandates atomic replacement for lifecycle files. Readable
            # transcripts use an execution-owned writer: reopening/replacing
            # a live .txt can collide with Windows readers. CSV remains
            # the authoritative retained turns for interruption inspection.
            if name not in self._files:
                self._files[name] = (self.folder / name).open("x", encoding="utf-8", newline="")
            file = self._files[name]
            file.seek(0)
            file.write(safe)
            file.truncate()
            file.flush()
            os.fsync(file.fileno())
        else:
            _atomic_text(self.folder / name, safe)

    def _save_metadata(self):
        self._atomic("execution.json", json.dumps(self.redactor.value(self.metadata), ensure_ascii=False, indent=2) + "\n")

    def _save_runs(self):
        self._atomic("runs.csv", _csv_text(CSV_COLUMNS["runs"], self.redactor.value(list(self._runs.values()))))

    def append_csv(self, name: str, record: dict):
        with self._mutex:
            try:
                if self._closed:
                    raise OutputError("Output writers have already closed.")
                safe = self.redactor.value(record)
                safe = {key: "TRUE" if value is True else "FALSE" if value is False else value
                        for key, value in safe.items()}
                self._writers[name].writerow(safe)
                self._files[name].flush()
            except Exception as error:
                self._fault({**record, "turn": record.get("turn", record.get("after_turn")),
                             "call_type": "survey" if name in {"responses", "reactions"} else record.get("call_type")})
                raise OutputError(f"Required {name} data could not be written.") from error

    def append_jsonl(self, name: str, records: list[dict]):
        with self._mutex:
            try:
                if self._closed:
                    raise OutputError("Output writers have already closed.")
                # One lock and one write per coordinator state batch; actual
                # completeness remains validated by readers after interruption.
                text = "".join(json.dumps(self.redactor.value(record), ensure_ascii=False,
                                          separators=(",", ":")) + "\n" for record in records)
                self._files[name].write(text)
                self._files[name].flush()
            except Exception as error:
                context = dict(records[0]) if records else {}
                if name == "states" and records:
                    # Even a complete write may fail at flush. Preserve bytes
                    # for inspection, but identify the unpublished target in
                    # failed lifecycle metadata so it is never displayed as
                    # a successfully committed snapshot.
                    context["uncommitted_state_version"] = records[0]["state_version"]
                    context.update(phase="init" if records[0]["state_version"] == 0 else "turn",
                                   turn=records[0]["after_turn"],
                                   call_type="state_init" if records[0]["state_version"] == 0 else "state_update")
                self._fault(context)
                raise OutputError(f"Required {name} audit data could not be written.") from error

    def audit(self, record: dict):
        self.append_jsonl("calls", [record])

    def start_run(self, run_id: str, agents: list[dict], speaker_order):
        with self._mutex:
            try:
                if run_id in self._runs:
                    raise ValueError("A run may only start once.")
                for agent in agents:
                    self.append_csv("agents", agent)
                self._models[run_id] = {str(row["seat"]): row["model"] for row in agents}
                self._runs[run_id] = {
                    "run_id": run_id, "status": "running", "n_turns": 0, "n_passes": 0,
                    "end_reason": "", "speaker_order": json.dumps(list(speaker_order)),
                    "agent_names": json.dumps({str(row["seat"]): row["agent"] for row in agents}, ensure_ascii=False),
                    "started_at": utc_now(), "finished_at": "", "error": "",
                }
                self._transcripts[run_id] = []
                self._save_runs()
                self._readable(run_id)
            except OutputError:
                raise
            except OSError as error:
                self._fault({"run_id": run_id})
                raise OutputError("The run start record could not be written.") from error

    def record_turn(self, record: dict):
        with self._mutex:
            self.append_csv("transcripts", record)
            self._transcripts[record["run_id"]].append(dict(record))
            try:
                self._readable(record["run_id"])
            except OSError as error:
                self._fault(record)
                raise OutputError("The readable transcript could not be written.") from error

    def _readable(self, run_id):
        row = self._runs[run_id]
        lines = [
            f"Study: {self.frozen.study.study_name}", f"Run: {run_id}", f"Date: {row['started_at']}",
            f"Speaker order: {row['speaker_order']}", f"Models: {json.dumps(self._models[run_id], ensure_ascii=False)}",
            f"silence_visibility: {self.frozen.study.run.silence_visibility}",
            f"Status: {row['status']}", f"End reason: {row['end_reason']}", "",
        ]
        builder = PromptBuilder(self.frozen.study.prompt_templates)
        for record in self._transcripts[run_id]:
            if record["passed"] and self.frozen.study.run.silence_visibility == "hidden":
                lines.append(f"[{record['speaker']} passes — hidden from participants]")
            else:
                lines.append(builder.event(PublicEvent(record["public_event_index"], record["speaker"],
                                                       record["event_type"], record["text"])))
        self._atomic(f"transcripts/{run_id}.txt", "\n\n".join(lines) + "\n")

    def finish_run(self, run_id, status, end_reason, error=""):
        with self._mutex:
            previous = dict(self._runs[run_id])
            previous_finalized = self.metadata["n_runs_finalized"]
            try:
                row = self._runs[run_id]
                if row["status"] != "running":
                    raise ValueError("A run may only finalize once.")
                records = self._transcripts[run_id]
                row.update(status=status, end_reason=end_reason, error=self.redactor.text(error),
                           n_turns=len(records), n_passes=sum(record["passed"] for record in records),
                           finished_at=utc_now())
                self._save_runs()  # final row before finalized execution count
                self._readable(run_id)
                self.metadata["n_runs_finalized"] += 1
                self._save_metadata()
            except OSError as failure:
                # Keep the coordinator's run active until all final writes
                # succeed, so best-effort failure finalization can still mark it
                # failed. Do not count a partly persisted finalization twice.
                self._runs[run_id] = previous
                self.metadata["n_runs_finalized"] = previous_finalized
                self._fault({"run_id": run_id})
                raise OutputError("The run's final lifecycle record could not be written.") from failure

    def _close_best_effort(self):
        for file in self._files.values():
            try:
                file.close()
            except OSError:
                pass

    def close_writers(self):
        with self._mutex:
            if self._closed:
                return
            try:
                for file in self._files.values():
                    file.flush()
                    file.close()
                self._closed = True
            except OSError as error:
                self._fault()
                self._close_best_effort()
                self._closed = True
                raise OutputError("Required output writers could not be flushed and closed.") from error

    def finalize(self):
        """No completed metadata until all append writers close successfully."""
        try:
            self.close_writers()
            cause = self.abort.cause
            if cause:
                self.metadata.update(status="failed", error_details=cause,
                                     error=format_error(cause, folder=self.folder))
            elif self.metadata["n_runs_finalized"] == self.metadata["n_runs_requested"]:
                self.metadata.update(status="completed")
            else:
                raise ValueError("Execution cannot complete with unfinalized runs.")
            self.metadata["finished_at"] = utc_now()
            self._save_metadata()
        except OSError as error:
            self._fault()
            self.metadata.update(status="failed", finished_at=utc_now(), error_details=self.abort.cause,
                                 error=format_error(self.abort.cause, folder=self.folder))
            try:
                self._save_metadata()
            except OSError:
                pass  # folder remains incomplete; caller receives the failure
            raise OutputError("Execution finalization could not be stored completely.") from error
        finally:
            self._close_best_effort()
            if self._lock:
                self._lock.release()


def read_jsonl(path: Path) -> tuple[list[dict], tuple[str, ...]]:
    """Preserve incomplete/corrupt bytes; never treat fragments as valid rows."""
    rows, warnings = [], []
    with open_output(path, binary=True) as file:
        data = file.read()
    for position, line in enumerate(data.splitlines(keepends=True), 1):
        try:
            if not line.endswith(b"\n"):
                raise ValueError("incomplete final fragment")
            row = strict_json_loads(line.decode("utf-8"))
            if not isinstance(row, dict):
                raise ValueError("record is not an object")
            rows.append(row)
        except (ValueError, UnicodeError):
            warnings.append(f"{path.name} line {position} is incomplete or invalid and was retained for inspection.")
    return rows, tuple(warnings)


def lock_is_owned(folder: Path) -> bool:
    """File existence is not proof; test the native lock without waiting."""
    lock = native_lock(folder / ".execution.lock")
    try:
        lock.acquire(timeout=0)
    except Timeout:
        return True
    else:
        lock.release()
        return False


def inspect_states(folder: Path) -> dict:
    """Read-only complete-batch validation; never repair/export partial states."""
    from .state import StateError, validate_state_object
    path = folder / "states.jsonl"
    if not path.exists():
        return {"states": [], "state_versions": {}, "uncommitted_public_events": {}, "warnings": ()}
    metadata = json.loads(read_output_text(folder / "execution.json"))
    with open_output(folder / "agents.csv", newline="") as file:
        agents = list(csv.DictReader(file))
    with open_output(folder / "runs.csv", newline="") as file:
        run_rows = list(csv.DictReader(file))
    with open_output(folder / "transcripts.csv", newline="") as file:
        transcripts = list(csv.DictReader(file))
    rows, row_warnings = read_jsonl(path)
    return inspect_state_records(metadata, agents, run_rows, transcripts, rows, row_warnings)


def inspect_state_records(metadata, agents, run_rows, transcripts, rows, row_warnings=()):
    """Validate complete state batches against supplied read-only record views."""
    from .state import StateError, validate_state_object
    warnings = list(row_warnings)
    failed_commit = metadata.get("error_details") or {}
    seats = {}
    invalid_runs = set()
    for agent in agents:
        try:
            run_id = agent["run_id"]
            seat = int(agent["seat"])
            if seat < 1 or not agent["agent"] or None in agent or seat in seats.get(run_id, {}):
                raise ValueError
            seats.setdefault(run_id, {})[seat] = agent
        except (ValueError, TypeError, KeyError):
            invalid_runs.add(agent.get("run_id"))
            warnings.append("Invalid agent metadata was retained; its state batches cannot be verified.")
    for row in run_rows:
        try:
            actual = {int(key): value for key, value in strict_json_loads(row["agent_names"]).items()}
            known = seats.get(row["run_id"], {})
            if (set(actual) != set(range(1, len(actual) + 1)) or len(actual) not in (2, 3)
                    or {seat: r["agent"] for seat, r in known.items()} != actual):
                raise ValueError
        except (ValueError, TypeError, KeyError, AttributeError):
            invalid_runs.add(row.get("run_id"))
            warnings.append("The run's seat mapping is incomplete; its state batches cannot be verified.")
    events = {}
    for row in transcripts:
        if row.get("public_event_index"):
            try:
                index = int(row["public_event_index"])
                if index < 1 or index in events.get(row["run_id"], {}) or row["event_type"] not in {"message", "silence"}:
                    raise ValueError
                events.setdefault(row["run_id"], {})[index] = row
            except (ValueError, TypeError, KeyError):
                invalid_runs.add(row.get("run_id"))
                warnings.append("Invalid public-event metadata was retained; its state batches cannot be verified.")
    groups = {}
    for row in rows:
        run_id, version = row.get("run_id"), row.get("state_version")
        if not isinstance(run_id, str) or type(version) is not int or version < 0:
            warnings.append("An invalid state version record was retained and excluded.")
            continue
        groups.setdefault((run_id, version), []).append(row)
    committed, versions = [], {}
    columns = {"run_id", "seat", "agent", "model", "state_version", "after_turn", "public_message_index",
               "public_event_index", "event_type", "state", "call_id", "timestamp"}
    for (run_id, version), batch in sorted(groups.items()):
        try:
            if (run_id == failed_commit.get("run_id")
                    and type(failed_commit.get("uncommitted_state_version")) is int
                    and version >= failed_commit["uncommitted_state_version"]):
                raise ValueError
            actual_seats = seats.get(run_id, {})
            if run_id in invalid_runs or len(actual_seats) not in (2, 3) or len(batch) != len(actual_seats):
                raise ValueError
            if {r.get("seat") for r in batch} != set(actual_seats):
                raise ValueError
            names = {r["agent"] for r in actual_seats.values()}
            if len(names) != len(actual_seats):
                raise ValueError
            event = events.get(run_id, {}).get(version) if version else None
            expected_message_count = sum(
                e["event_type"] == "message" for index, e in events.get(run_id, {}).items() if index <= version)
            for row in batch:
                if (set(row) != columns or type(row["seat"]) is not int
                        or any(type(row[key]) is not int or row[key] < 0 for key in
                               ("after_turn", "public_message_index", "public_event_index"))
                        or row["public_event_index"] != version
                        or row["public_message_index"] != expected_message_count
                        or row["agent"] != actual_seats[row["seat"]]["agent"]
                        or row["model"] != actual_seats[row["seat"]]["model"]
                        or not isinstance(row["call_id"], str) or not row["call_id"]):
                    raise ValueError
                recorded = datetime.fromisoformat(row["timestamp"])
                if recorded.utcoffset() != timedelta(0):
                    raise ValueError
                if version == 0:
                    if row["event_type"] != "init" or row["after_turn"] != 0:
                        raise ValueError
                elif (event is None or row["event_type"] != event["event_type"]
                      or row["after_turn"] != int(event["turn"])):
                    raise ValueError
                validate_state_object(row["state"], tuple(names - {row["agent"]}))
            prior = versions.setdefault(run_id, [])
            if version != len(prior):
                raise ValueError  # do not present a later version across a gap
            prior.append(version)
            committed.extend(sorted(batch, key=lambda r: r["seat"]))
        except (ValueError, TypeError, KeyError, StateError):
            warnings.append(f"State batch {run_id}, version {version} is incomplete or invalid and was retained; it is not committed.")
    uncommitted = {run_id: sorted(set(run_events) - set(versions.get(run_id, [])))
                   for run_id, run_events in events.items()}
    for row in run_rows:
        if row["status"] == "completed" and 0 not in versions.get(row["run_id"], []):
            warnings.append(f"Completed run {row['run_id']} is missing its committed initialization batch.")
    return {"states": committed, "state_versions": versions, "uncommitted_public_events": uncommitted,
            "warnings": tuple(warnings)}


def inspect_lifecycle(folder: Path) -> dict:
    """Request-free low-level inspection; only abandoned lifecycle may change.

    Full result views/legacy display interpretation arrive with Stage 6.
    Research records, YAML, measurements, states, and fragments stay untouched.
    """
    folder = Path(folder)
    metadata = json.loads(read_output_text(folder / "execution.json"))
    with open_output(folder / "runs.csv", newline="") as file:
        reader = csv.DictReader(file, strict=True)
        if reader.fieldnames != CSV_COLUMNS["runs"]:
            raise OutputError("The saved run lifecycle columns are incomplete.")
        runs = list(reader)
    warnings = []
    if metadata.get("status") == "running":
        lock = native_lock(folder / ".execution.lock")
        try:
            lock.acquire(timeout=0)
        except Timeout:
            state_data = inspect_states(folder)
            return {"metadata": metadata, "runs": runs, "active": True, "complete": False,
                    **{key: state_data[key] for key in ("states", "state_versions", "uncommitted_public_events")},
                    "warnings": ("An active coordinator owns this execution.",) + state_data["warnings"]}
        try:
            # Reread after acquiring; the owner may have finalized between reads.
            metadata = json.loads(read_output_text(folder / "execution.json"))
            with open_output(folder / "runs.csv", newline="") as file:
                runs = list(csv.DictReader(file, strict=True))
            if metadata.get("status") == "running":
                transcript_rows = []
                with open_output(folder / "transcripts.csv", newline="") as file:
                    reader = csv.DictReader(file, strict=True)
                    try:
                        for row in reader:
                            try:
                                if (None in row or any(value is None for value in row.values())
                                        or int(row["turn"]) < 1 or int(row["seat"]) < 1
                                        or row["passed"] not in {"TRUE", "FALSE"}
                                        or row["event_type"] not in {"message", "silence"}
                                        or (row["passed"] == "TRUE" and row["text"] != "")):
                                    raise ValueError
                                transcript_rows.append(row)
                            except (ValueError, KeyError, TypeError):
                                warnings.append("An invalid persisted transcript row was retained and excluded from counts.")
                    except csv.Error:
                        warnings.append("An incomplete transcript fragment was retained and excluded from counts.")
                detected = utc_now()
                explanation = ("Execution was found without an active owner. It is interrupted and will not resume. "
                               "The finish time records detection, not the unknown time of interruption. "
                               "Retained logs may omit an in-flight request.")
                for run in runs:
                    if run["status"] == "running":
                        retained = [row for row in transcript_rows if row["run_id"] == run["run_id"]]
                        run.update(status="interrupted", end_reason="interrupted", finished_at=detected,
                                   n_turns=len(retained), n_passes=sum(row["passed"] == "TRUE" for row in retained),
                                   error=explanation)
                _atomic_text(folder / "runs.csv", _csv_text(CSV_COLUMNS["runs"], runs))
                metadata.update(status="interrupted", finished_at=detected, error=explanation,
                                n_runs_finalized=sum(row["status"] != "running" for row in runs))
                _atomic_text(folder / "execution.json", json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
                warnings.append(explanation)
        finally:
            lock.release()
    required = ["study.yaml", "agents.csv", "transcripts.csv", "responses.csv", "calls.jsonl", "runs.csv"]
    if any(p.get("call_type") in {"state_init", "state_update"} for p in metadata.get("generation_parameters", [])):
        required.append("states.jsonl")
    complete = (metadata.get("status") == "completed"
                and len(runs) == metadata.get("n_runs_requested") == metadata.get("n_runs_finalized")
                and all(row["status"] in {"completed", "failed"} for row in runs)
                and all((folder / name).is_file() for name in required))
    if metadata.get("status") == "completed" and not complete:
        warnings.append("The saved completion metadata and required files do not match; the dataset is incomplete.")
    readable_transcripts = {}
    for run in runs:
        if not re.fullmatch(r"run_[0-9]{3,}", run.get("run_id", "")):
            warnings.append("An invalid saved run identifier cannot select a transcript file.")
            continue
        file = folder / "transcripts" / (run["run_id"] + ".txt")
        if file.is_file():
            text = file.read_text(encoding="utf-8")
            if run["status"] == "interrupted":
                text = text.replace("\n\nStatus: running\n\n", "\n\nStatus: interrupted\n\n", 1)
                text = text.replace("\n\nEnd reason: \n\n", "\n\nEnd reason: interrupted\n\n", 1)
            readable_transcripts[run["run_id"]] = text
    state_data = inspect_states(folder)
    warnings.extend(state_data["warnings"])
    if state_data["warnings"] or any(state_data["uncommitted_public_events"].values()):
        complete = False
    return {"metadata": metadata, "runs": runs, "active": False, "complete": complete,
            "warnings": tuple(warnings), "readable_transcripts": readable_transcripts,
            **{key: state_data[key] for key in ("states", "state_versions", "uncommitted_public_events")}}
