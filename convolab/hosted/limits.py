"""Approved capacity checks and bounded writes without truncating records."""
import csv
import io
import json
from pathlib import Path

from convolab.desktop import estimate_calls
from convolab.llm import error_details
from convolab.outputs import CSV_COLUMNS, OutputError, OutputStore
from .settings import HostedError

MAX_CALLS = 300
MAX_UPLOAD_BYTES = 5_000_000
MAX_BUNDLE_BYTES = 25_000_000
MAX_EXECUTION_SECONDS = 30 * 60
# Reserve terminal metadata and ZIP headers; not a wall-clock limit.
FINALIZATION_RESERVE = 512_000
ZIP_RESERVE = 64_000


def check_upload(data):
    if not isinstance(data, bytes) or len(data) > MAX_UPLOAD_BYTES:
        raise HostedError("Uploaded files must be 5 MB or smaller.")
    return data


def check_study(study):
    estimate = estimate_calls(study)
    if estimate["total"] > MAX_CALLS:
        raise HostedError("This study exceeds the website's 300 estimated model-call limit. Reduce its settings explicitly before starting.")
    return estimate


class BoundedOutputStore(OutputStore):
    """Pre-write checks inside the original store's writer lock."""
    def __init__(self, *args, max_bytes=MAX_BUNDLE_BYTES, **kwargs):
        self.max_bytes = max_bytes
        self._finalizing = False
        super().__init__(*args, **kwargs)

    def _budget(self, name, size, *, replace=False):
        limit = self.max_bytes - (ZIP_RESERVE if self._finalizing else FINALIZATION_RESERVE)
        total = sum(p.stat().st_size for p in self.folder.rglob("*") if p.is_file())
        old = (self.folder / name).stat().st_size if replace and (self.folder / name).is_file() else 0
        if total - old + size > limit:
            self.abort.signal(error_details("storage") | {"hosting_reason": "output_limit"})
            raise OutputError("The website output limit was reached. Previously written records were retained.", folder=self.folder)

    def _atomic(self, name, text):
        with self._mutex:
            self._budget(name, len(self.redactor.text(text).encode("utf-8")), replace=True)
            return super()._atomic(name, text)

    def append_csv(self, name, record):
        with self._mutex:
            safe = {k: "TRUE" if v is True else "FALSE" if v is False else v
                    for k, v in self.redactor.value(record).items()}
            target = io.StringIO(newline="")
            csv.DictWriter(target, fieldnames=CSV_COLUMNS[name]).writerow(safe)
            self._budget(name + ".csv", len(target.getvalue().encode("utf-8")))
            return super().append_csv(name, record)

    def append_jsonl(self, name, records):
        with self._mutex:
            size = sum(len((json.dumps(self.redactor.value(r), ensure_ascii=False,
                        separators=(",", ":")) + "\n").encode("utf-8")) for r in records)
            self._budget(name + ".jsonl", size)
            return super().append_jsonl(name, records)

    def finalize(self):
        self._finalizing = True
        return super().finalize()
