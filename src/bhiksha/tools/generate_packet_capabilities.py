"""Generate Bhiksha's shared-kernel packet capability manifest."""

from __future__ import annotations

import argparse
from pathlib import Path

from bhiksha.packets.capabilities import write_packet_capability_manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("artifacts/capabilities/bhiksha_packet_capabilities_v1.json"),
    )
    args = parser.parse_args(argv)
    print(write_packet_capability_manifest(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
