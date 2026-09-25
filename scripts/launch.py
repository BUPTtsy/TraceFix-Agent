"""Load local .env without shell evaluation and hand off to CLI."""
import os
from pathlib import Path
os.chdir(Path(__file__).resolve().parents[1])
from dotenv import load_dotenv
load_dotenv('.env', override=False, encoding='utf-8-sig')
from tracefix.cli.main import main
main()
