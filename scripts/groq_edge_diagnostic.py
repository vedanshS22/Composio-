"""Temporary edge diagnostic. It never prints API credentials."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BASE = "https://api.groq.com/openai/v1"
MODEL = "groq/compound"
USER_AGENT = "scout100/1.0"

def clean(value: str) -> str:
    return " ".join(value.replace(os.getenv("GROQ_API_KEY", ""), "[redacted]").split())[:800]

def emit(endpoint: str, model: str, status: int, body: str) -> None:
    print(f"endpoint: {endpoint}")
    print(f"model: {model}")
    print(f"HTTP status: {status}")
    print(f"sanitized response body: {clean(body)}")
    print(f"User-Agent sent: {USER_AGENT}")

def urllib_request(path: str, model: str, payload: dict | None = None) -> int:
    body = json.dumps(payload).encode() if payload else None
    request = Request(BASE + path, data=body, method="POST" if payload else "GET", headers={"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}", "Content-Type": "application/json", "User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=30) as response:  # nosec - fixed provider endpoint
            content = response.read().decode(errors="replace"); emit("urllib " + BASE + path, model, response.status, content); return response.status
    except HTTPError as error:
        emit("urllib " + BASE + path, model, error.code, error.read().decode(errors="replace")); return error.code

def curl_request(path: str, model: str, payload: dict | None = None) -> None:
    command = ["curl.exe", "-sS", "-i", "-X", "POST" if payload else "GET", BASE + path, "-H", f"Authorization: Bearer {os.environ['GROQ_API_KEY']}", "-H", "Content-Type: application/json", "-H", f"User-Agent: {USER_AGENT}"]
    if payload: command += ["--data", json.dumps(payload)]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    raw = completed.stdout.replace("\r\n", "\n"); chunks = raw.split("\n\n"); final = chunks[-1]
    headers = chunks[-2] if len(chunks) > 1 else raw
    status = next((int(line.split()[1]) for line in headers.splitlines() if line.startswith("HTTP/")), 0)
    emit("curl " + BASE + path, model, status, final or completed.stderr)

if __name__ == "__main__":
    models = urllib_request("/models", "n/a")
    chat = urllib_request("/chat/completions", MODEL, {"model": MODEL, "messages": [{"role": "user", "content": "Return only JSON: {\"ok\":true}"}], "response_format": {"type": "json_object"}, "temperature": 0})
    if models == 403 or chat == 403:
        curl_request("/models", "n/a")
        curl_request("/chat/completions", MODEL, {"model": MODEL, "messages": [{"role": "user", "content": "Return only JSON: {\"ok\":true}"}], "response_format": {"type": "json_object"}, "temperature": 0})
