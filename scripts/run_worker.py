#!/usr/bin/env python
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import arq


def main() -> None:
    """Run the arq background worker."""
    from agent_trust.workers import WorkerSettings

    arq.run_worker(WorkerSettings)


if __name__ == "__main__":
    main()
