"""One-request diagnostic; never prints a credential."""
from __future__ import annotations
import sys
from pathlib import Path
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pipeline.env_loader import load_dotenv
from agents.research_agent import _call_llm

load_dotenv()

try:
    _call_llm('Return only {"ok": true}.')
    print("groq_health=ok")
except HTTPError as error:
    print(f"groq_status={error.code}")
    print(error.read().decode(errors="replace")[:1000])
