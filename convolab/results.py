"""Read-only research-result views. No model or bundled-persona resolution."""
from __future__ import annotations

import csv
from dataclasses import dataclass
import io
import json
from pathlib import Path
import re
from zipfile import ZIP_DEFLATED, ZipFile

import yaml
from filelock import Timeout

from .config import parse_study_yaml
from .llm import Redactor, format_error, utc_now
from .outputs import (CSV_COLUMNS, OutputError, _atomic_text, _csv_text, inspect_lifecycle, inspect_state_records,
                      native_lock, open_output, read_jsonl, read_output_text)


class ResultsError(ValueError):
    pass


def checked_folder(root, folder):
    root, folder = Path(root).resolve(), Path(folder).resolve()
    if folder == root or not folder.is_relative_to(root) or not folder.is_dir():
        raise ResultsError("Choose an existing execution folder inside this app's output folder.")
    return folder


def saved_folders(root):
    root = Path(root)
    if not root.exists():
        return []
    return sorted((path.parent for path in root.rglob("execution.json")
                   if path.parent.resolve().is_relative_to(root.resolve()) and path.is_file()),
                  key=lambda p: str(p), reverse=True)


def safe_file(folder, relative):
    file = folder / relative
    if not file.resolve().is_relative_to(folder.resolve()):
        raise ResultsError("A saved file points outside the selected execution folder.")
    return file


def csv_table(folder, name, warnings):
    path = safe_file(folder, name + ".csv")
    if not path.is_file():
        warnings.append(f"{name}.csv is missing; retained data is incomplete.")
        return [], []
    rows = []
    try:
        with open_output(path, newline="") as file:
            reader = csv.DictReader(file, strict=True)
            columns = reader.fieldnames or []
            if len(columns) != len(set(columns)):
                warnings.append(f"{name}.csv has duplicate columns; its rows cannot be inspected reliably.")
                return [], columns
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    warnings.append(f"An incomplete {name}.csv row was retained and excluded from this view.")
                else:
                    rows.append(row)
            return rows, columns
    except (csv.Error, UnicodeError):
        warnings.append(f"An invalid fragment in {name}.csv was retained and excluded from this view.")
        return rows, []


def _legacy_lifecycle(folder, metadata, runs, transcripts, warnings):
    """Legacy schema can finalize abandoned lifecycle without migrating research data."""
    if metadata.get("status") != "running":
        return False
    lock = native_lock(folder / ".execution.lock")
    try:
        lock.acquire(timeout=0)
    except Timeout:
        warnings.append("An active coordinator owns this execution; reopening cannot resume or replace it.")
        return True
    try:
        current = json.loads(read_output_text(folder / "execution.json"))
        if current.get("status") == "running":
            detected = utc_now()
            explanation = ("Execution was found without an active owner. It is interrupted and will not resume. "
                           "The finish time records detection, not the unknown time of interruption. "
                           "Retained logs may omit an in-flight request.")
            for run in runs:
                if run.get("status") == "running":
                    retained = [row for row in transcripts if row.get("run_id") == run.get("run_id")]
                    run.update(status="interrupted", end_reason="interrupted", finished_at=detected,
                               n_turns=len(retained), n_passes=sum(r.get("passed") == "TRUE" for r in retained),
                               error=explanation)
            with open_output(folder / "runs.csv", newline="") as file:
                columns = next(csv.reader(file))
            _atomic_text(folder / "runs.csv", _csv_text(columns, runs))
            current.update(status="interrupted", finished_at=detected, error=explanation,
                           n_runs_finalized=sum(r.get("status") != "running" for r in runs))
            _atomic_text(folder / "execution.json", json.dumps(current, ensure_ascii=False, indent=2) + "\n")
            warnings.append(explanation)
        metadata.clear()
        metadata.update(current)
        return False
    finally:
        lock.release()


@dataclass(frozen=True)
class SavedResults:
    folder: Path
    study: dict
    metadata: dict
    tables: dict
    states: list
    versions: dict
    uncommitted: dict
    warnings: tuple
    errors: tuple
    legacy: bool
    visibility: str
    persona_source: str
    active: bool
    files_complete: bool
    measurements_complete: bool
    archive: bytes | None


def open_results(root, folder):
    folder = checked_folder(root, folder)
    warnings, secrets = [], []
    try:
        study = parse_study_yaml(read_output_text(safe_file(folder, "study.yaml")))
        metadata = json.loads(read_output_text(safe_file(folder, "execution.json")))
        if not isinstance(metadata, dict):
            raise ValueError
    except (ValueError, OSError, UnicodeError):
        raise ResultsError("Saved study or lifecycle metadata is missing or unreadable. Files are retained; inspect this folder before starting a new execution.") from None
    if (not isinstance(study.get("run", {}), dict) or not isinstance(study.get("agents", []), list)
            or not all(isinstance(a, dict) for a in study.get("agents", []))
            or not isinstance(study.get("items", []), list) or not all(isinstance(i, dict) for i in study.get("items", []))):
        raise ResultsError("Saved study fields have an unreadable structure. Retained files are unchanged; inspect this folder before starting a new execution.")
    # Old studies may contain inline credentials. Never restore or expose them.
    for agent in study.get("agents", []):
        if isinstance(agent, dict):
            key = agent.pop("api_key", None)
            if isinstance(key, str) and key:
                secrets.append(key)
    redactor = Redactor(secrets)
    tables, headers = {}, {}
    for name in ("runs", "agents", "transcripts", "responses", "reactions"):
        if name == "reactions" and not (folder / "reactions.csv").exists() and not study.get("run", {}).get("inner_reaction", False):
            tables[name], headers[name] = [], []
        else:
            tables[name], headers[name] = csv_table(folder, name, warnings)
    legacy = ("silence_visibility" not in study.get("run", {})
              or not {"public_event_index", "event_type"}.issubset(headers["transcripts"]))
    state_data = {"states": [], "state_versions": {}, "uncommitted_public_events": {}}
    active, complete = False, False
    # Reject path escapes before strict inspection, including state/transcript files.
    for name in ("states.jsonl", "calls.jsonl", "runs.csv", "agents.csv", "transcripts.csv", ".execution.lock"):
        safe_file(folder, name)
    safe_file(folder, "transcripts")
    for path in (folder / "transcripts").glob("run_*.txt"):
        safe_file(folder, path.relative_to(folder))
    if legacy:
        warnings.append("Legacy hidden-silence results: display indices are derived from spoken rows only. No historical silence-state snapshots are assumed.")
        if not {"public_event_index", "state_version"}.issubset(headers["responses"]):
            warnings.append("Legacy measurement files do not record the public-event/state links. Those missing links are unknown; no survey-to-state relationship is invented.")
        active = _legacy_lifecycle(folder, metadata, tables["runs"], tables["transcripts"], warnings)
        indices = {}
        for row in tables["transcripts"]:
            run = row.get("run_id")
            if row.get("passed") == "TRUE":
                row.update(public_event_index="", event_type="silence")
            else:
                indices[run] = indices.get(run, 0) + 1
                row.update(public_event_index=str(indices[run]), event_type="message")
        if (folder / "states.jsonl").exists():
            legacy_states, state_warnings = read_jsonl(folder / "states.jsonl")
            for snapshot in legacy_states:
                # Old speech-only state records can be validated against derived
                # display events. Existing silence records never become speech.
                if "public_event_index" not in snapshot:
                    snapshot["public_event_index"] = snapshot.get("public_message_index")
                if "event_type" not in snapshot:
                    snapshot["event_type"] = "init" if snapshot.get("state_version") == 0 else "message"
            try:
                state_data = inspect_state_records(metadata, tables["agents"], tables["runs"], tables["transcripts"], legacy_states, state_warnings)
                warnings.extend(state_data["warnings"])
            except (ValueError, KeyError, TypeError, AttributeError):
                warnings.append("Legacy state batches cannot be verified completely; raw snapshots are retained but not displayed as committed.")
        complete = (not active and metadata.get("status") == "completed"
                    and len(tables["runs"]) == metadata.get("n_runs_requested") == metadata.get("n_runs_finalized")
                    and all(r.get("status") in {"completed", "failed"} for r in tables["runs"]))
    else:
        try:
            lifecycle = inspect_lifecycle(folder)
            metadata, tables["runs"] = lifecycle["metadata"], lifecycle["runs"]
            active, complete = lifecycle["active"], lifecycle["complete"]
            state_data = lifecycle
            warnings.extend(lifecycle["warnings"])
        except (ValueError, KeyError, TypeError, AttributeError, OSError, UnicodeError):
            warnings.append("Saved lifecycle/state records cannot be verified completely. Retained files are unchanged and are not presented as a complete dataset.")
    calls = []
    if (folder / "calls.jsonl").is_file():
        calls, call_warnings = read_jsonl(folder / "calls.jsonl")
        warnings.extend(call_warnings)
    else:
        warnings.append("calls.jsonl is missing; request outcomes cannot be verified.")
    # A successful final attempt replaces prior retry errors for this administration.
    final_calls = {}
    for call in calls:
        if call.get("call_id"):
            if (type(call.get("attempt")) is not int or call["attempt"] != final_calls.get(call["call_id"], {}).get("attempt", 0) + 1
                    or call["attempt"] > 3):
                warnings.append("Saved request attempts do not follow the one-to-three-attempt budget. Audit records are incomplete or unverified.")
            final_calls[call["call_id"]] = call
        else:
            warnings.append("A saved audit object has no call identity and cannot verify a request.")
    if not legacy:
        _verify_retained(study, tables, final_calls, state_data, warnings, active)
    errors = []
    cause = metadata.get("error_details")
    if isinstance(cause, dict) and cause:
        from .llm import error_details
        fallback = error_details("unknown")
        errors.append(format_error({**fallback, **cause}, folder=folder))
    elif metadata.get("error"):
        errors.append(str(metadata["error"]) + f" Retained files: {folder}.")
    for call in final_calls.values():
        if call.get("error_type"):
            from .llm import error_details
            from .llm import GUIDANCE
            category = call["error_type"] if call["error_type"] in GUIDANCE else "unknown"
            details = error_details(category, meta=call, call_type=call.get("call_type"),
                                    call_id=call.get("call_id"), attempts=call.get("attempt"))
            details.update({key: call[key] for key in ("seat", "agent", "model") if key in call})
            errors.append(format_error(details, folder=folder))
    for run in tables["runs"]:
        if run.get("error"):
            errors.append(f"{run.get('run_id', 'Unknown run')}: {run['error']} Retained files: {folder}.")
    missing = [row for name in ("responses", "reactions") for row in tables[name] if row.get("valid") != "TRUE"]
    survey_errors = any(c.get("call_type") == "survey" and c.get("error_type") for c in final_calls.values())
    if missing:
        warnings.append(f"{len(missing)} measurement values are missing or invalid. Blank values are not answers; inspect the affected rows and call explanations.")
    finalized_success = metadata.get("status") == "completed" and all(r.get("status") == "completed" for r in tables["runs"])
    measurements_complete = finalized_success and complete and not missing and not survey_errors and not warnings
    if legacy:
        measurements_complete = False  # older contracts cannot establish current measurement completeness
    if active:
        warnings.append("Results are still being written. Refresh the view to inspect retained progress; downloads become available after the owner finishes.")
    sanitized_study = redactor.value(study)
    archive = None if active else _archive(folder, sanitized_study, redactor)
    return SavedResults(folder, sanitized_study, redactor.value(metadata), redactor.value(tables),
                        redactor.value(state_data["states"]), redactor.value(state_data["state_versions"]),
                        state_data["uncommitted_public_events"], tuple(dict.fromkeys(redactor.value(warnings))),
                        tuple(dict.fromkeys(redactor.value(errors))), legacy,
                        "hidden" if legacy else study.get("run", {}).get("silence_visibility", "public"),
                        "builtin" if study.get("persona_source") == "builtin" and metadata.get("persona_dataset") else "custom",
                        active, complete and not warnings, measurements_complete, archive)


def _archive(folder, study, redactor):
    buffer = io.BytesIO()
    names = ["execution.json", "study.yaml", "agents.csv", "transcripts.csv", "responses.csv", "runs.csv",
             "reactions.csv", "states.jsonl", "calls.jsonl"]
    names += [str(path.relative_to(folder)).replace("\\", "/") for path in (folder / "transcripts").glob("run_*.txt")
              if re.fullmatch(r"run_[0-9]{3,}\.txt", path.name)]
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        for name in names:
            path = safe_file(folder, name)
            if path.is_file():
                if name == "study.yaml":
                    raw = yaml.safe_dump(study, allow_unicode=True, sort_keys=False).encode("utf-8")
                else:
                    with open_output(path, binary=True) as file:
                        raw = file.read()
                # Preserve CRLF and even retained torn UTF-8 fragments. Only the
                # archive copy is sanitized; the research records never change.
                text = raw.decode("utf-8", errors="surrogateescape")
                archive.writestr(name, redactor.text(text).encode("utf-8", errors="surrogateescape"))
    return buffer.getvalue()


def _verify_retained(study, tables, calls, states, warnings, active):
    """Verify exported counts and record joins; never infer an answer from a gap."""
    try:
        agents = {(r["run_id"], int(r["seat"])): r for r in tables["agents"]}
        if len(agents) != len(tables["agents"]):
            raise ValueError
        items = {r["item_id"]: r for r in study.get("items", [])}
        by_event, turns = {}, {}
        for row in tables["transcripts"]:
            run, turn, seat = row["run_id"], int(row["turn"]), int(row["seat"])
            person = agents[run, seat]
            if (person["agent"] != row["speaker"] or person["model"] != row["model"]
                    or row["passed"] not in {"TRUE", "FALSE"} or turn != len(turns.setdefault(run, [])) + 1):
                raise ValueError
            turns[run].append(row)
            if row["public_event_index"]:
                index = int(row["public_event_index"])
                if index != sum(r["run_id"] == run for r in by_event.values()) + 1:
                    raise ValueError
                by_event[run, index] = row
        snapshots = {(s["run_id"], s["seat"], s["state_version"]): s for s in states["states"]}
        for snapshot in snapshots.values():
            call = calls[snapshot["call_id"]]
            if (call.get("error_type") or call.get("call_type") != ("state_init" if snapshot["state_version"] == 0 else "state_update")
                    or call.get("target_state_version") != snapshot["state_version"]
                    or call.get("turn") != snapshot["after_turn"]
                    or any(call.get(key) != snapshot[key] for key in ("run_id", "seat", "agent", "model", "public_event_index", "public_message_index"))):
                raise ValueError
        for run_turns in turns.values():
            for transcript in run_turns:
                matching = [c for c in calls.values() if c.get("run_id") == transcript["run_id"] and c.get("call_type") == "main"
                            and c.get("turn") == int(transcript["turn"]) and c.get("seat") == int(transcript["seat"])]
                if len(matching) != 1 or matching[0].get("error_type"):
                    raise ValueError
                call = matching[0]
                if transcript["text"] != ("" if transcript["passed"] == "TRUE" else call.get("cleaned_text")):
                    raise ValueError
        for row in tables["runs"]:
            if row["status"] != "running" and (int(row["n_turns"]) != len(turns.get(row["run_id"], []))
                    or int(row["n_passes"]) != sum(r["passed"] == "TRUE" for r in turns.get(row["run_id"], []))):
                raise ValueError
        seen = set()
        for name in ("responses", "reactions"):
            for row in tables[name]:
                call = calls[row["call_id"]]
                marker = (name, row["call_id"], row.get("item_id"))
                if marker in seen or call["call_type"] != "survey":
                    raise ValueError
                seen.add(marker)
                if any(str(call.get(key) if call.get(key) is not None else "") != row[key]
                       for key in ("run_id", "seat", "agent", "model", "phase", "public_event_index", "state_version")):
                    raise ValueError
                if row["after_turn"] != str(call["turn"]) or row["valid"] not in {"TRUE", "FALSE"}:
                    raise ValueError
                if study.get("run", {}).get("private_state", True):
                    if (row["run_id"], int(row["seat"]), int(row["state_version"])) not in snapshots:
                        raise ValueError
                if name == "responses":
                    item = items[row["item_id"]]
                    if not item.get(row["phase"]) or row["scale"] != item["scale"]:
                        raise ValueError
                    if row["valid"] == "TRUE" and item["type"] == "likert":
                        value = float(row["response"])
                        if not value.is_integer() or not item["min"] <= value <= item["max"]:
                            raise ValueError
        if not active:
            for call in calls.values():
                if call.get("call_type") != "survey":
                    continue
                # Unreturned fatal calls have no measurable output to fabricate.
                if call.get("error_type") and not call.get("raw_response"):
                    continue
                selected = [item for item in items.values() if item.get(call.get("phase"))]
                retained = [r for r in tables["responses"] if r["call_id"] == call["call_id"]]
                if {r["item_id"] for r in retained} != {i["item_id"] for i in selected}:
                    warnings.append(f"Measurement rows are incomplete for {call.get('run_id')}, seat {call.get('seat')}, {call.get('phase')} after opportunity {call.get('turn')}. Retained values remain available; no missing answers are invented.")
            r = study.get("run", {})
            for row in tables["runs"]:
                if row["status"] != "completed":
                    continue
                run_id = row["run_id"]
                expected = {(phase, after, seat) for phase, after in (("pre", 0), ("post", int(row["n_turns"])))
                            if any(i.get(phase) for i in items.values()) for rid, seat in agents if rid == run_id}
                for event in turns.get(run_id, []):
                    if event["public_event_index"] and int(event["turn"]) % int(r.get("measure_every_n_turns", 1)) == 0 and any(i.get("turn") for i in items.values()):
                        expected.update(("turn", int(event["turn"]), seat) for rid, seat in agents if rid == run_id)
                actual = {(c.get("phase"), c.get("turn"), c.get("seat")) for c in calls.values()
                          if c.get("run_id") == run_id and c.get("call_type") == "survey"}
                if expected != actual:
                    warnings.append(f"{run_id}: saved questionnaire administrations do not match the completed conversation. Measurements are incomplete or unverified; retained records are unchanged.")
    except (KeyError, ValueError, TypeError, OverflowError):
        warnings.append("Saved record counts or joins are inconsistent. The dataset and measurements are incomplete or unverified; retained files are unchanged.")


def transcript_view(results, run_id):
    rows = [r for r in results.tables["transcripts"] if r.get("run_id") == run_id]
    return [{**row, "observation": ("Hidden pass — researcher only" if not row.get("public_event_index")
                                   else "Visible silence") if row.get("passed") == "TRUE" else "Spoken message"}
            for row in rows]


def measurement_view(results, run_id):
    events = {row.get("public_event_index"): row for row in transcript_view(results, run_id) if row.get("public_event_index")}
    rows = []
    for row in results.tables["responses"]:
        if row.get("run_id") != run_id:
            continue
        phase = row.get("phase")
        administration = "turn " + str(row.get("after_turn")) if phase == "turn" else phase
        latest = events.get(row.get("public_event_index"), {})
        rows.append({**row, "administration": administration,
                     "event_context": "Before conversation" if phase == "pre" else
                     "Not recorded in legacy file" if results.legacy and not row.get("public_event_index") else
                     "Silence" if latest.get("event_type") == "silence" else "Spoken message" if latest else "No public event"})
    return rows


def chart_data(results, run_id, item_id):
    """One row per administration; turn N and post remain distinct x positions."""
    import pandas as pd
    rows = [row for row in measurement_view(results, run_id) if row.get("item_id") == item_id]
    order, agents, values = [], [], {}
    for row in rows:
        label, agent = row["administration"], row.get("agent", "Unknown agent")
        if label not in order:
            order.append(label)
        if agent not in agents:
            agents.append(agent)
        value = None
        if row.get("valid") == "TRUE":
            try:
                value = float(row["response"])
            except (ValueError, TypeError):
                pass
        values[label, agent] = value
    return pd.DataFrame([{agent: values.get((label, agent)) for agent in agents} for label in order],
                        index=pd.Index(order, name="Administration"))


def state_view(results, run_id, seat, version):
    if version not in results.versions.get(run_id, []):
        raise ResultsError("Choose a complete, committed version.")
    record = next((row for row in results.states if row["run_id"] == run_id and row["seat"] == seat
                   and row["state_version"] == version), None)
    if record is None:
        raise ResultsError("No verified state exists for this run and seat.")
    return {"snapshot": record, "related_surveys": [r for r in measurement_view(results, run_id)
            if r.get("seat") == str(seat) and r.get("state_version") == str(version)],
            "related_reactions": [r for r in results.tables["reactions"] if r.get("run_id") == run_id
                                  and r.get("seat") == str(seat) and r.get("state_version") == str(version)],
            "uncommitted_events": results.uncommitted.get(run_id, [])}
