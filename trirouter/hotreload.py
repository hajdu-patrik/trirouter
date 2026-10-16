"""Reloads a package's modules in place when its .py or config .json files change on disk."""
import importlib
import sys
from pathlib import Path


class HotReloader:
    """Mtime-scans the package and re-imports `target` fresh after a change; a failed reload keeps the old code."""

    def __init__(self, package, root, target, keep=()):
        self.package, self.root, self.target = package, Path(root), target
        self.keep = {f"{package}.{name}" for name in keep}
        self.module = None
        self.warning = ""
        self._seen = {}

    def _signature(self):
        files = [*self.root.glob("*.py"), *self.root.glob("config/*.json")]
        sig = {}
        for path in files:
            try:
                st = path.stat()
            except OSError:
                continue
            sig[str(path)] = (st.st_mtime_ns, st.st_size)
        return sig

    def _purge(self):
        prefix = self.package + "."
        stale = {n: m for n, m in sys.modules.items() if (n == self.package or n.startswith(prefix)) and n not in self.keep}
        for name in stale:
            del sys.modules[name]
        return stale

    def stale(self):
        """True when the next current() call would (re)load: nothing loaded yet, or a file changed."""
        return self.module is None or self._signature() != self._seen

    def current(self):
        """The target module, reloaded first if a source or config file changed since the last load."""
        sig = self._signature()
        if self.module is not None and sig == self._seen:
            return self.module
        first = self.module is None
        old = None if first else self._purge()
        try:
            module = importlib.import_module(f"{self.package}.{self.target}")
        except Exception as exc:  # noqa: BLE001 - e.g. a syntax error mid-edit; keep serving the old code
            if old is not None:
                self._purge()
                sys.modules.update(old)
            self._seen = sig
            self.warning = f"trirouter reload failed, still on the previous code ({type(exc).__name__}: {exc})"
            if first:
                raise
            return self.module
        self.module, self._seen, self.warning = module, sig, ""
        return module
