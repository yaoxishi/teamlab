"""Immutable, bounded research snapshots; envelope is outside research schemas."""
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import re
import tempfile
from zipfile import ZipFile, ZIP_DEFLATED, BadZipFile

from convolab.outputs import native_lock, open_output, OutputError
from convolab.results import open_results
from convolab.llm import error_details
from convolab.json_protocol import strict_json_loads
from .auth import canonical_uuid
from .limits import BoundedOutputStore, MAX_BUNDLE_BYTES
from .settings import HostedError

ENVELOPE = 'convolab-checkpoint.json'
FILES = {'study.yaml', 'execution.json', 'agents.csv', 'runs.csv', 'transcripts.csv',
         'responses.csv', 'reactions.csv', 'calls.jsonl', 'states.jsonl'}


def allowed_file(name):
    return name in FILES or bool(re.fullmatch(r'transcripts/run_[0-9]{3,}\.txt', name))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def snapshot(store, lease):
    # Caller holds the writer mutex at a coordinator boundary. No OS lock,
    # temporary files, credentials, or mutable visitor editor enters this ZIP.
    records = {}
    for file in store._files.values():
        if not file.closed:
            file.flush()
    for path in sorted(store.folder.rglob('*')):
        name = path.relative_to(store.folder).as_posix()
        if allowed_file(name) and path.is_file():
            if path.is_symlink() or not path.resolve().is_relative_to(store.folder.resolve()):
                raise HostedError('The private checkpoint could not be verified.')
            with open_output(path, binary=True) as file:
                records[name] = file.read()
    if sum(map(len, records.values())) > MAX_BUNDLE_BYTES:
        raise HostedError('The website output-size limit was reached.')
    envelope = {'version': 1, 'owner_id': lease.owner_id, 'execution_id': lease.execution_id,
        'engine_execution_id': canonical_uuid(store.metadata['execution_id']),
        'files': {n: {'bytes': len(b), 'sha256': sha(b)} for n, b in records.items()}}
    buf = io.BytesIO()
    with ZipFile(buf, 'w', ZIP_DEFLATED) as archive:
        for name, data in records.items():
            archive.writestr(name, data)
        archive.writestr(ENVELOPE, json.dumps(envelope, separators=(',', ':')))
    data = buf.getvalue()
    if len(data) > MAX_BUNDLE_BYTES:
        raise HostedError('The website output-size limit was reached.')
    return data


def unpack(data, record):
    """Validate all bytes before extraction; no arbitrary paths or ZIP bombs."""
    try:
        if not isinstance(data, bytes) or len(data) > MAX_BUNDLE_BYTES or sha(data) != record.sha256:
            raise ValueError
        with ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            names = [i.filename for i in infos]
            if (len(names) != len(set(names)) or ENVELOPE not in names or len(names) > 1010
                    or any(n != ENVELOPE and not allowed_file(n) for n in names)
                    or any(i.flag_bits & 1 or i.is_dir() or (i.external_attr >> 16) & 0o170000 == 0o120000 for i in infos)
                    or sum(i.file_size for i in infos) > MAX_BUNDLE_BYTES + 256000):
                raise ValueError
            envelope = strict_json_loads(archive.read(ENVELOPE).decode('utf-8'))
            if (type(envelope.get('version')) is not int or envelope.get('version') != 1 or envelope.get('owner_id') != record.owner_id
                    or envelope.get('execution_id') != record.id
                    or not isinstance(envelope.get('files'), dict)
                    or set(envelope['files']) != set(names) - {ENVELOPE}):
                raise ValueError
            engine = canonical_uuid(envelope['engine_execution_id'])
            files = {name: archive.read(name) for name in envelope['files']}
            for name, raw in files.items():
                if envelope['files'][name] != {'bytes': len(raw), 'sha256': sha(raw)}:
                    raise ValueError
            if json.loads(files['execution.json'])['execution_id'] != engine:
                raise ValueError
            return files
    except (BadZipFile, ValueError, TypeError, KeyError, AttributeError, RuntimeError, OSError):
        raise HostedError('The saved checkpoint failed its integrity check. No study was started.') from None


class CheckpointStore(BoundedOutputStore):
    """Cloud saves only at coordinator boundaries, never inside a state batch."""
    def __init__(self, *args, lease, sink):
        self.lease, self.sink = lease, sink
        super().__init__(*args)
        try:
            self.checkpoint()
        except OutputError:
            # Original initialization is outside the engine's finalizer loop.
            # Close ownership/writers and retain honest local failure metadata.
            super().finalize()
            raise

    def checkpoint(self):
        with self._mutex:
            try:
                self.sink.save(self.lease, snapshot(self, self.lease))
            except Exception:
                self.abort.signal(error_details('storage') | {'hosting_reason': 'cloud_save_failed'})
                mark=getattr(self.sink,'mark_failed',None)
                if mark:
                    mark(self.lease)
                else:
                    self.sink.failed,self.sink.save_status=True,'failed'
                raise OutputError('The private checkpoint could not be saved.', folder=self.folder) from None

    def start_run(self, *args, **kwargs):
        with self._mutex:
            super().start_run(*args, **kwargs)
            self.checkpoint()

    def record_turn(self, *args, **kwargs):
        with self._mutex:
            super().record_turn(*args, **kwargs)
            self.checkpoint()

    def finish_run(self, *args, **kwargs):
        with self._mutex:
            super().finish_run(*args, **kwargs)
            # Preserve a prior cloud failure; final failure snapshot may still save.
            if not self.abort.event.is_set():
                self.checkpoint()

    def finalize(self):
        with self._mutex:
            try:
                super().finalize()
            except OutputError:
                # Still retain eligible, bounded raw fragments on a local
                # finalization fault, without calling it a complete dataset.
                self.checkpoint()
                raise
            try:
                self.checkpoint()
            except OutputError:
                # A final cloud fault must also become the original storage abort.
                # No research call is retried; local terminal bytes stay truthful.
                super().finalize()
                raise


@contextmanager
def cloud_results(data, record, temporary_root):
    """Ephemeral, request-free view using authoritative cloud lifecycle evidence."""
    files = unpack(data, record)
    root = Path(temporary_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='view-', dir=root) as directory:
        folder = Path(directory) / 'research'
        folder.mkdir()
        for name, raw in files.items():
            path = folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        # Never let the local reader infer abandonment of cloud-owned work.
        # This lock guards the immutable restored copy, not an execution worker.
        lock = native_lock(folder / '.execution.lock')
        lock.acquire(timeout=0)
        try:
            view = open_results(Path(directory), folder)
            live = record.status in {'pending', 'running'}
            warnings = tuple(w for w in view.warnings if not w.startswith(('An active coordinator', 'Results are still being written.')))
            if record.status == 'interrupted':
                warnings += ('Cloud ownership expired. This is the last verified checkpoint; in-flight requests or later records may be absent. It cannot resume.',)
            elif live:
                warnings += ('The cloud owner is still active. This is a retained checkpoint, not the live process folder.',)
            if record.save_status != 'saved':
                warnings += ('The latest save was not confirmed. These are the last verified retained bytes; they may be incomplete.',)
            # Raw research lifecycle is never rewritten to fabricate cloud history.
            yield replace(view, active=live, files_complete=view.files_complete and not live,
                          measurements_complete=view.measurements_complete and record.status == 'complete' and record.save_status == 'saved',
                          warnings=warnings, archive=None)
        finally:
            lock.release()
