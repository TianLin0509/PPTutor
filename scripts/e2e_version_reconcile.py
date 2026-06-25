"""E2E: version reconcile catches watcher-missed creates/copies/saves.

This uses an isolated data dir and a temporary deck directory. It deliberately
does not start VaultWatcher; every version is recovered through the reconcile
path that protects users from missed filesystem events.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

import fixtures_gen as fx  # noqa: E402

_tmp = Path(tempfile.mkdtemp(prefix="pptxver_reconcile_e2e_"))
DATA = _tmp / "appdata"
WORK = _tmp / "work"
WORK.mkdir(parents=True, exist_ok=True)
os.environ["PPTX_FINDER_DATA_DIR"] = str(DATA)
os.environ["PPTUTOR_VERSION_RECONCILE_COMMON_DIRS"] = "0"
os.environ["PPTUTOR_VERSION_RECONCILE_DIRS"] = str(WORK)

from pptx_finder.versioning import store, vault  # noqa: E402
from pptx_finder.versioning.manager import VersionManager  # noqa: E402

results: list[tuple[str, bool]] = []


def check(name: str, cond: bool) -> None:
    results.append((name, bool(cond)))
    print(f"{'PASS' if cond else 'FAIL'} | {name}")


def main() -> int:
    try:
        mgr = VersionManager()
        base = WORK / "base.pptx"
        missed_new = WORK / "missed-new.pptx"
        missed_copy = WORK / "missed-copy.pptx"

        fx.make_pptx(base, [{"body": "base v1"}])
        check("precondition: base snapshot", bool(mgr.snapshot_now(str(base))))
        check("base has one version", len(mgr.list_versions(str(base))) == 1)

        fx.make_pptx(missed_new, [{"body": "new file missed by watcher"}])
        created = mgr.reconcile_known_docs()
        check("reconcile creates first version for missed new file", created >= 1)
        check("missed new file has history", len(mgr.list_versions(str(missed_new))) == 1)

        shutil.copy2(base, missed_copy)
        mgr.reconcile_known_docs()
        copy_versions = mgr.list_versions(str(missed_copy))
        copy_doc_id = vault.doc_id_for(str(missed_copy))
        branch = store.get_branch(mgr._conn, copy_doc_id)
        check("missed copy inherits existing history", bool(copy_versions))
        check("missed copy is recorded as independent branch", branch is not None)

        fx.make_pptx(missed_new, [{"body": "new file v2 missed save"}])
        future = time.time() + 3
        os.utime(missed_new, (future, future))
        created2 = mgr.reconcile_known_docs(scan_new_files=False)
        check("reconcile catches later missed save", created2 == 1)
        check("missed new file now has two versions", len(mgr.list_versions(str(missed_new))) == 2)

        diag = "\n".join(mgr.diagnostic_lines())
        check("diagnostics include reconcile counters", "version_reconcile:" in diag and "last_new_checked=" in diag)

        ok = sum(1 for _, passed in results if passed)
        print(f"\n=== E2E version reconcile: {ok}/{len(results)} passed ===")
        return 0 if ok == len(results) else 1
    finally:
        shutil.rmtree(_tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
