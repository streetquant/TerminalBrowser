#!/usr/bin/env python3
"""Emit a read-only TermDeck state and terminal-auth cohort inventory."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from termdeck.state_inventory import StateInventoryError, inventory_state  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-root",
        "--source-path",
        dest="source_root",
        required=True,
        type=Path,
        help="absolute TermDeck state root (the singular alias is retained for compatibility)",
    )
    parser.add_argument("--terminal-auth-root", type=Path, default=None)
    parser.add_argument(
        "--no-holder-scan",
        action="store_true",
        help="skip the read-only /proc holder scan",
    )
    parser.add_argument(
        "--require-quiescent",
        action="store_true",
        help="fail unless the holder scan completes and finds no open state file",
    )
    parser.add_argument(
        "--require-complete-cohorts",
        action="store_true",
        help="fail if a terminal-auth WAL/SHM/journal has no matching primary",
    )
    args = parser.parse_args(argv)
    try:
        report = inventory_state(
            args.source_root,
            terminal_auth_root=args.terminal_auth_root,
            check_holders=not args.no_holder_scan,
            require_quiescent=args.require_quiescent,
            require_complete_cohorts=args.require_complete_cohorts,
        )
    except StateInventoryError as exc:
        print(json.dumps({"schema": "termdeck.state-inventory.v1", "ok": False, "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps({"schema": "termdeck.state-inventory.v1", "ok": True, "inventory": report}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
