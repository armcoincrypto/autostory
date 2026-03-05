#!/usr/bin/env python3
"""
Convert Telegram Desktop tdata folder to Telethon session string.
Use the output in Dashboard → Accounts → Add → Import from tdata.
Or upload a zip of tdata in the dashboard (Add Account → Import from tdata).

Usage:
  python -m scripts.convert_tdata /path/to/tdata
  python -m scripts.convert_tdata "C:\\Users\\You\\AppData\\Roaming\\Telegram Desktop\\tdata"

Requires: pip install opentele
"""
import asyncio
import os
import sys
from pathlib import Path

# Project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Load .env for API credentials
from dotenv import load_dotenv
load_dotenv()

from src.core.tdata_convert import tdata_to_session_string


async def main(tdata_path: str, passcode: str = None):
    try:
        session_string = await tdata_to_session_string(tdata_path, passcode=passcode)
    except ValueError as e:
        print(str(e))
        sys.exit(1)
    print("\n--- Session string (paste in Dashboard → Add Account → Import from tdata) ---\n")
    print(session_string)
    print("\n--- End ---\n")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        default = os.path.expanduser("~/AppData/Roaming/Telegram Desktop/tdata" if os.name == "nt" else "~/tdata")
        mac_default = os.path.expanduser("~/Library/Containers/org.telegram.desktop/Data/Library/Application Support/Telegram Desktop/tdata")
        print("Usage: python -m scripts.convert_tdata /path/to/tdata [--passcode LOCAL_PASSCODE]")
        print(f"  Windows: \"{default}\"")
        print(f"  macOS (App Store): \"{mac_default}\"")
        sys.exit(1)
    passcode = None
    args = sys.argv[1:]
    if "--passcode" in args:
        i = args.index("--passcode")
        if i + 1 < len(args):
            passcode = args[i + 1]
            args = args[:i] + args[i + 2:]
        else:
            print("--passcode requires a value (your Telegram Local Passcode)")
            sys.exit(1)
    asyncio.run(main(args[0], passcode=passcode))
