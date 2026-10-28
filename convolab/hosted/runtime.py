"""One thread-safe server coordinator; never cached visitor keys or UI state."""
from functools import lru_cache
import os
from pathlib import Path
import threading

from .admission import DurableAdmission, ServiceSettings
from .coordinator import ExecutionCoordinator
from .storage import DurableCheckpoints
from .maintenance import start_local_maintenance

_runtime_lock = threading.RLock()


@lru_cache(maxsize=1)
def _configured_runtime(settings):
    service = ServiceSettings.from_environment(settings, os.environ)
    runtime=ExecutionCoordinator(DurableAdmission(service), Path("deploy/private/working"))
    configure=getattr(runtime,'configure_storage',None)
    if configure:
        configure(lambda: DurableCheckpoints(service))
        start_local_maintenance(service,runtime)
    return runtime


def execution_runtime(settings):
    # Remains disabled for the staged source until owner-controlled setup and
    # later storage/release verification. No accounts or environment edits here.
    if os.environ.get("CONVOLAB_HOSTED_EXECUTION_ENABLED") != "1":
        return None
    # lru_cache permits simultaneous first-miss construction. Serialize it so
    # every browser observes the same process coordinator after initial setup.
    with _runtime_lock:
        return _configured_runtime(settings)
