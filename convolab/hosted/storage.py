"""Server-only immutable object writes, verified reads and fenced publication."""
from uuid import uuid4
import httpx

from .admission import DurableAdmission
from .checkpoints import sha
from .limits import MAX_BUNDLE_BYTES
from .repository import PRIVATE_BUCKET, checkpoint_key, OwnedRepository
from .settings import HostedError


class PrivateStorage:
    def __init__(self, settings, *, transport=None):
        self.settings, self.transport = settings, transport

    def object(self, method, key, data=None):
        # Key construction is trusted. Revalidate canonical path before HTTP.
        parts = key.split('/') if isinstance(key, str) else []
        if len(parts) != 4 or checkpoint_key(parts[0], parts[1], parts[3][:-4]) != key:
            raise HostedError('The requested private item is unavailable.')
        secret = self.settings.secret.get_secret_value()
        headers = {'apikey': secret, 'Cache-Control': 'no-store', 'x-upsert': 'false'}
        if secret.count('.') == 2:
            headers['Authorization'] = 'Bearer ' + secret
        if method not in {'GET', 'POST'} or (method == 'POST' and (not isinstance(data, bytes) or len(data) > MAX_BUNDLE_BYTES)):
            raise HostedError('The requested storage operation is unavailable.')
        if method == 'POST':
            headers['Content-Type'] = 'application/zip'
        try:
            with httpx.Client(transport=self.transport, timeout=30, trust_env=False, follow_redirects=False) as client:
                with client.stream(method, self.settings.public.url + '/storage/v1/object/' + PRIVATE_BUCKET + '/' + key,
                                   headers=headers, content=data) as response:
                    if response.status_code not in {200, 201}:
                        raise HostedError('The private storage service could not confirm the operation.')
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > MAX_BUNDLE_BYTES:
                            raise HostedError('The saved checkpoint exceeds the website limit.')
                        chunks.append(chunk)
                    return b''.join(chunks)
        except httpx.HTTPError:
            raise HostedError('The private storage service could not confirm the operation.') from None


class CheckpointRPC(DurableAdmission):
    def call(self, name, body):
        # Extend the fixed service RPC surface without making arbitrary names callable.
        if name not in {'reserve_checkpoint', 'publish_checkpoint', 'checkpoint_confirmed', 'save_failed'}:
            raise HostedError('The requested checkpoint operation is unavailable.')
        return self._request_rpc(name, body)


class DurableCheckpoints:
    def __init__(self, settings, *, transport=None):
        self.rpc = CheckpointRPC(settings, transport=transport)
        self.storage = PrivateStorage(settings, transport=transport)
        self.save_status = 'pending'
        self.failed = False

    def mark_failed(self, lease):
        self.failed, self.save_status = True, 'failed'
        try:
            self.rpc.call('save_failed', lease.body())
        except HostedError:
            pass

    def save(self, lease, data):
        key, digest = checkpoint_key(lease.owner_id, lease.execution_id, str(uuid4())), sha(data)
        body = lease.body() | {'p_key': key, 'p_sha': digest}
        try:
            if self.rpc.call('reserve_checkpoint', body) is not True:
                raise HostedError('The checkpoint owner could not be confirmed.')
            try:
                self.storage.object('POST', key, data)
            except HostedError:
                # Unknown upload acknowledgement: inspect this same immutable key.
                # No repeated POST, replacement object, or model administration.
                pass
            if sha(self.storage.object('GET', key)) != digest:
                raise HostedError('The uploaded checkpoint failed its integrity check.')
            try:
                confirmed = self.rpc.call('publish_checkpoint', body) is True
            except HostedError:
                confirmed = self.rpc.call('checkpoint_confirmed', body) is True
            if not confirmed:
                raise HostedError('The checkpoint publication was not confirmed.')
            self.save_status = 'failed' if self.failed else 'saved'
        except Exception:
            self.mark_failed(lease)
            raise HostedError('Cloud saving failed. Retained data may be incomplete; no model call will be retried.') from None


class OwnedCheckpoints:
    def __init__(self, settings, *, transport=None):
        self.repository = OwnedRepository(settings.public, transport=transport)
        self.storage = PrivateStorage(settings, transport=transport)

    def read(self, identity, execution_id):
        record = self.repository.get_execution(identity, execution_id)
        if record.object_key is None:
            raise HostedError('No verified checkpoint is available for this execution.')
        data = self.storage.object('GET', record.object_key)
        if sha(data) != record.sha256:
            raise HostedError('The saved checkpoint failed its integrity check.')
        # RLS uses database time on each request, including after a slow fetch.
        latest = self.repository.get_execution(identity, execution_id)
        if latest.object_key != record.object_key or latest.sha256 != record.sha256:
            raise HostedError('The checkpoint changed. Refresh the private result view.')
        return latest, data
