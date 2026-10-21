"""Run from the app working directory: python scripts/docker_schema.py init|verify."""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from database import engine
from docker_schema import init_schema, verify_schema


async def run(command):
    try:
        await (init_schema() if command == "init" else verify_schema())
        print(f"Schema {command} succeeded")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "verify"))
    asyncio.run(run(parser.parse_args().command))
