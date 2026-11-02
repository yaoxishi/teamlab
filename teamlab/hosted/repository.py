"""Read-only, user-scoped execution metadata; no public object/media URLs."""
from dataclasses import dataclass
from datetime import datetime, timezone
import re

from .auth import SupabaseBoundary, VerifiedIdentity, canonical_uuid
from .settings import HostedError


PRIVATE_BUCKET = "convolab-private"
READ_COLUMNS = "id,owner_id,status,save_status,created_at,finished_at,expires_at,object_key,sha256"


def checkpoint_key(owner_id, execution_id, checkpoint_id):
    return "/".join((canonical_uuid(owner_id), canonical_uuid(execution_id),
                     "checkpoints", canonical_uuid(checkpoint_id) + ".zip"))


def valid_checkpoint_key(value, owner_id, execution_id):
    if not isinstance(value, str):
        return False
    parts = value.split("/")
    if len(parts) != 4 or parts[:3] != [owner_id, execution_id, "checkpoints"] or not parts[3].endswith(".zip"):
        return False
    try:
        return checkpoint_key(owner_id, execution_id, parts[3][:-4]) == value
    except HostedError:
        return False


def _timestamp(value, *, optional=False):
    if optional and value is None:
        return None
    if not isinstance(value, str):
        raise HostedError("The private result metadata is invalid.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HostedError("The private result metadata is invalid.") from None
    if parsed.tzinfo is None:
        raise HostedError("The private result metadata is invalid.")
    return parsed


@dataclass(frozen=True)
class ExecutionRecord:
    id: str
    owner_id: str
    status: str
    save_status: str
    created_at: datetime
    finished_at: datetime | None
    expires_at: datetime | None
    object_key: str | None
    sha256: str | None

    @classmethod
    def owned(cls, row, identity: VerifiedIdentity, now):
        if not isinstance(row, dict) or row.get("owner_id") != identity.owner_id:
            raise HostedError("The requested private item is unavailable.")
        identifier = canonical_uuid(row.get("id"))
        expiry = _timestamp(row.get("expires_at"), optional=True)
        if expiry is not None and expiry <= now:
            raise HostedError("The requested private item is unavailable.")
        if row.get("status") not in {"pending", "running", "complete", "failed", "interrupted"} or row.get("save_status") not in {"pending", "saved", "failed"}:
            raise HostedError("The private result metadata is invalid.")
        pointer, digest = row.get("object_key"), row.get("sha256")
        if pointer is not None and not valid_checkpoint_key(pointer, identity.owner_id, identifier):
            raise HostedError("The private result metadata is invalid.")
        if (pointer is None) != (digest is None) or (digest is not None and not re.fullmatch(r"[0-9a-f]{64}", str(digest))):
            raise HostedError("The private result metadata is invalid.")
        return cls(identifier, identity.owner_id, row["status"], row["save_status"],
                   _timestamp(row.get("created_at")), _timestamp(row.get("finished_at"), optional=True),
                   expiry, pointer, digest)


class OwnedRepository(SupabaseBoundary):
    def __init__(self, settings, *, transport=None, now=None):
        super().__init__(settings, transport=transport)
        self.now = now or (lambda: datetime.now(timezone.utc))

    def list_executions(self, identity):
        if not isinstance(identity, VerifiedIdentity):
            raise HostedError("Your private workspace needs a valid browser session.")
        rows = self.request("GET", "/rest/v1/convolab_executions", identity.access_token,
                            params={"select": READ_COLUMNS, "owner_id": "eq." + identity.owner_id,
                                    "order": "created_at.desc"})
        if not isinstance(rows, list):
            raise HostedError("The private workspace service returned an invalid response.")
        # RLS is authoritative; this second check also prevents a malformed
        # adapter/service response from crossing the application boundary.
        return tuple(ExecutionRecord.owned(row, identity, self.now()) for row in rows)

    def get_execution(self, identity, execution_id):
        if not isinstance(identity, VerifiedIdentity):
            raise HostedError("Your private workspace needs a valid browser session.")
        identifier = canonical_uuid(execution_id)
        rows = self.request("GET", "/rest/v1/convolab_executions", identity.access_token,
                            params={"select": READ_COLUMNS, "id": "eq." + identifier,
                                    "owner_id": "eq." + identity.owner_id})
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict) or rows[0].get("id") != identifier:
            raise HostedError("The requested private item is unavailable.")
        return ExecutionRecord.owned(rows[0], identity, self.now())
