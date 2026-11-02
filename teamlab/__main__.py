"""Small command-line entry point; execution stays in the shared engine."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import signal
import sys
import threading

from .config import (ConfigError, import_personas, read_import, read_study_data,
                     resolve_credentials, validate_study, with_custom_persona_pool)
from .llm import (AbortSignal, CancelledWork, Redactor, error_details, format_error,
                  validate_capabilities)
from .outputs import OutputError, OutputStore
from .simulation import run_study


class _ParserExit(Exception):
    def __init__(self, status):
        self.status = status


class _Parser(argparse.ArgumentParser):
    def __init__(self, *args, stdout, stderr, **kwargs):
        self.stdout, self.stderr = stdout, stderr
        super().__init__(*args, **kwargs)

    def _print_message(self, message, file=None):
        if message:
            (self.stderr if file is sys.stderr else self.stdout).write(message)

    def exit(self, status=0, message=None):
        if message:
            self.stderr.write(message)
        raise _ParserExit(status)

    def error(self, message):
        # argparse's original message can repeat arbitrary command-line values.
        self.print_usage(file=sys.stderr)
        self.exit(2, "Invalid command arguments. Use 'python -m teamlab run --help' for the required syntax.\n")


def _parser(stdout, stderr):
    parser = _Parser(prog="python -m teamlab", description="Run a TeamLab study and save its research outputs.",
                     stdout=stdout, stderr=stderr)
    commands = parser.add_subparsers(dest="command", required=True,
        parser_class=lambda **kwargs: _Parser(stdout=stdout, stderr=stderr, **kwargs))
    run = commands.add_parser("run", help="Validate and run a study YAML file.")
    run.add_argument("study", help="Path to the study YAML file.")
    run.add_argument("--personas", metavar="POOL", help="CSV or XLSX persona pool; selects custom source and pool mode.")
    return parser


def _candidate_secrets(data, environment):
    values = []
    entries = data.get("agents", ())
    for agent in entries if isinstance(entries, (list, tuple)) else ():
        if not isinstance(agent, dict):
            continue
        key = agent.get("api_key")
        if isinstance(key, str) and key:
            values.append(key)
        reference = agent.get("api_key_env")
        if isinstance(reference, str):
            value = environment.get(reference)
            if isinstance(value, str) and value:
                values.append(value)
    return tuple(values)


@contextmanager
def _keyboard_stop(abort, context, redactor):
    """SIGINT signals the existing abort boundary; it never abandons workers."""
    previous = None
    installed = threading.current_thread() is threading.main_thread()
    if installed:
        previous = signal.getsignal(signal.SIGINT)
        def stop(signum, frame):
            details = error_details("application", meta=context,
                                    call_type=context.get("call_type"))
            details.update(agent=context.get("agent"), model=context.get("model"),
                summary="Execution was stopped from the keyboard.",
                suggested_action="Inspect the retained data before starting a new execution. Already submitted requests may still finish and be billed.")
            abort.signal(redactor.value(details))
        signal.signal(signal.SIGINT, stop)
    try:
        yield
    finally:
        if installed:
            signal.signal(signal.SIGINT, previous)


def main(argv=None, *, environment=None, stdout=None, stderr=None,
         request=None, sleep=None, store_factory=OutputStore) -> int:
    """Injected services are for offline tests, not extra command-line flags."""
    stdout = stdout if stdout is not None else sys.stdout
    stderr = stderr if stderr is not None else sys.stderr
    environment = os.environ if environment is None else environment
    redactor = Redactor()
    folder = None
    abort = AbortSignal()
    context = {}

    def write(message, *, warning=False):
        stream = stderr if warning else stdout
        stream.write(redactor.text(message) + "\n")
        stream.flush()

    def progress(record):
        nonlocal folder
        event = record["event"]
        run_id = record["run_id"]
        context.clear()
        context.update(record)
        if event == "run_started":
            folder = Path(record["folder"])
            write(f"Starting {run_id} ({record['number']} of {record['n_runs']}). Results folder: {folder}")
        elif event == "stage":
            stage = {"state_init": "Initializing private states", "state_update": "Updating private states",
                     "main": "Speaking", "survey": f"{record.get('phase', '').capitalize()} questionnaire"}[record["call_type"]]
            seat = f", seat {record['seat']} ({record['agent']})" if record.get("seat") else ""
            write(f"{run_id}, opportunity {record['turn']}{seat}: {stage}.")
        elif event == "turn":
            activity = "pass (visible silence)" if record["passed"] and record["public_event_index"] else "pass (hidden)" if record["passed"] else "speech"
            write(f"{run_id}, opportunity {record['turn']}: recorded {activity} from {record['speaker']}.")
        elif event == "measurement_warning":
            write(format_error(record["error_details"], folder=folder), warning=True)
            missing = ", ".join(record["missing_items"]) or "none"
            reaction = "; required reaction is missing" if record["reaction_missing"] else ""
            write(f"Missing questionnaire items: {missing}{reaction}.", warning=True)
        elif event == "run_finished":
            write(f"{run_id}: {record['status']}; ending reason: {record['end_reason']}.")
            if record["error"]:
                write(record["error"] + f" Available data is retained in {folder}.", warning=True)

    try:
        args = _parser(stdout, stderr).parse_args(argv)
        data = read_study_data(args.study)
        secrets = _candidate_secrets(data, environment)
        redactor = Redactor(secrets)
        if args.personas:
            personas = import_personas(read_import(args.personas), Path(args.personas).name, mode="pool")
            data = with_custom_persona_pool(data, personas)
        study = validate_study(data, yaml_input=True)
        resolve_credentials(study, environment)
        parameters = validate_capabilities(study)
        for message in dict.fromkeys((*study.warnings(), *(w for p in parameters for w in p.warnings))):
            write("Configuration warning: " + message, warning=True)
        write(f"Study validated: {study.run.n_runs} Runs, {len(study.agents)} participants. Preparing outputs before requests.")
        with _keyboard_stop(abort, context, redactor):
            result = run_study(study, environment=environment, request=request, sleep=sleep,
                progress=progress, store_factory=store_factory, abort_signal=abort, additional_secrets=secrets)
        folder = result.folder
        write(f"Execution {result.status}; {result.n_runs_finalized} Runs finalized; {result.n_failed_runs} failed. Results folder: {folder}")
        if result.error_details:
            write(format_error(result.error_details, folder=folder), warning=True)
        if (abort.cause or {}).get("summary") == "Execution was stopped from the keyboard.":
            return 130
        return 1 if result.status != "completed" or result.n_failed_runs else 0
    except _ParserExit as error:
        return error.status
    except ConfigError as error:
        details = error_details("invalid_configuration")
        details.update(summary=f"Study could not start: {redactor.text(str(error))}",
            suggested_action="Correct the study or referenced input/key settings and run again. No model requests were sent.")
        write(format_error(details), warning=True)
        return 2
    except OutputError as error:
        folder = error.folder or folder
        write(format_error(abort.cause or error_details("storage"), folder=folder), warning=True)
        write("Initial outputs could not be established; no model requests were sent.", warning=True)
        return 1
    except CancelledWork:
        write(format_error(abort.cause or error_details("application"), folder=folder), warning=True)
        return 130 if (abort.cause or {}).get("summary") == "Execution was stopped from the keyboard." else 1
    except KeyboardInterrupt:
        # Interrupt before the execution handler was installed: no worker work.
        write("Stopped before execution. No model requests were sent.", warning=True)
        return 130
    except Exception:
        write(format_error(error_details("application"), folder=folder), warning=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
