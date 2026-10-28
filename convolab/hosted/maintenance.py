"""Conservative local-copy cleanup. Durable object cleanup runs independently."""
from pathlib import Path
import shutil
import threading

from .auth import canonical_uuid
from .admission import DurableAdmission
from .settings import HostedError


class LocalMaintenance(DurableAdmission):
    def sweep(self, root, active=()):
        root=Path(root).resolve()
        if not root.exists():
            return 0
        removed=0
        try:owners=list(root.iterdir())
        except OSError:return 0
        for owner in owners:
            if owner.is_symlink() or owner.is_junction() or not owner.is_dir():continue
            try:canonical_uuid(owner.name)
            except HostedError:continue
            try:executions=list(owner.iterdir())
            except OSError:continue
            for execution in executions:
                if execution.is_symlink() or execution.is_junction() or not execution.is_dir() or execution.name in active:continue
                try:
                    canonical_uuid(execution.name)
                    # Never recursively operate on an unchecked computed target.
                    target=execution.resolve()
                    if not target.is_relative_to(root) or target.parent!=owner.resolve():continue
                    allowed=self._request_rpc('local_expired',{'p_execution':execution.name,'p_owner':owner.name})
                    if allowed is not True:continue
                    # Refuse junctions/symlinks within the tree before deletion.
                    if any(p.is_symlink() or (hasattr(p,'is_junction') and p.is_junction()) for p in execution.rglob('*')):continue
                    shutil.rmtree(target)
                    removed+=1
                except (HostedError,OSError):continue  # Retry next sweep; no false success.
        return removed


def start_local_maintenance(settings, runtime):
    """Wake/sweep independently of browser reruns, while this process is alive."""
    maintenance=LocalMaintenance(settings)
    def loop():
        while True:
            with runtime._lock:
                active={key for key,job in runtime._jobs.items() if not job.done.is_set()}
            try:maintenance.sweep(runtime.output_root,active)
            except OSError:pass
            # Remove expired terminal job observations/settings after files expire.
            with runtime._lock:jobs=list(runtime._jobs.items())
            for key,job in jobs:
                if job.done.is_set() and not (runtime.output_root/job.lease.owner_id/key).exists():
                    try:
                        expired=maintenance._request_rpc('local_expired',{'p_execution':key,'p_owner':job.lease.owner_id})
                    except HostedError:
                        continue
                    if expired is True:
                        with runtime._lock:runtime._jobs.pop(key,None)
            threading.Event().wait(60)
    thread=threading.Thread(target=loop,name='convolab-local-retention',daemon=True)
    thread.start()
    return thread
