"""Detached worker entry point. Its input names saved authority, never a new prompt."""

import argparse
import asyncio
from pathlib import Path

from software_factory.context import FactoryContext
from software_factory.worker import WorkerExecutor


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--storage", required=True, type=Path)
    parser.add_argument("--session", required=True)
    parser.add_argument("--worker", required=True)
    parser.add_argument("--operation", required=True)
    args = parser.parse_args()
    context = FactoryContext.open(
        args.workspace, storage_root=args.storage, session_id=args.session
    )
    asyncio.run(WorkerExecutor(args.workspace, context, args.worker).execute(args.operation))


if __name__ == "__main__":
    main()
