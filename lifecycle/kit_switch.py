"""Token-free hosted lifecycle switch; missing records preserve existing guards."""

import argparse
import json
from pathlib import Path
import sys

MESSAGE = "Lifecycle feature switch is invalid."


def lifecycle_switch(path):
    try:
        if Path(path).is_symlink():
            raise ValueError(MESSAGE)
        data = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return "on"
    except (OSError, ValueError, RecursionError):
        raise ValueError(MESSAGE) from None
    if (not isinstance(data, dict) or not isinstance(data.get("features"), dict)
            or type(data["features"].get("lifecycle")) is not bool):
        raise ValueError(MESSAGE)
    return "on" if data["features"]["lifecycle"] else "off"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kit", default=".github/agentic-kit.json")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        value = lifecycle_switch(args.kit)
        with Path(args.output).open("a") as handle:
            handle.write("lifecycle=" + value + "\n")
    except (ValueError, OSError):
        print(MESSAGE, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
