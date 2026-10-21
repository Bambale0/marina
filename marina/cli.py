"""Engineering commands; no secrets are printed."""

import argparse
import re
import time
from pathlib import Path

from . import create_app
from .db import insert, transaction


def init_registry(app):
    spec = Path(__file__).resolve().parent.parent / "docs/planning/epics/E00.md"
    with transaction(app.config["DATABASE"]) as db:
        for line in spec.read_text().splitlines():
            match = re.match(r"\| (O\d{2}|D\d{2}) \| (.+)", line)
            if not match:
                continue
            id, rest = match.groups()
            parts = [p.strip() for p in rest.split("|")]
            object_id = "registry-" + id.lower()
            if db.execute("SELECT 1 FROM objects WHERE id=?", (object_id,)).fetchone():
                continue
            insert(
                db,
                "decision",
                {
                    "title": id + " · " + parts[0],
                    "owner": parts[-2] if len(parts) > 2 else "Владелец",
                    "status": "open" if id.startswith("O") else "agreed",
                    "evidence": "GitHub issue #4; " + (parts[1] if len(parts) > 1 else ""),
                    "epics": [],
                },
                int(app.config["CLOCK"]()),
                id=object_id,
            )
    print("Decision registry initialized. Materials and tariffs remain unpublished.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["init", "worker", "poll", "backup", "verify"])
    parser.add_argument("--path")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    app = create_app()
    if args.command == "init":
        init_registry(app)
    elif args.command == "poll":
        from .bot import poll

        poll(app, args.once)
    elif args.command == "worker":
        from .operations import work_once, tick

        while True:
            tick(app)
            count = work_once(app)
            if args.once:
                break
            if not count:
                time.sleep(1)
    elif args.command in ("backup", "verify"):
        parser.error("--path is required") if not args.path else None
        from .operations import backup, verify_backup

        result = (
            backup(app.config["DATABASE"], args.path)
            if args.command == "backup"
            else verify_backup(args.path)
        )
        print(result)


if __name__ == "__main__":
    main()
