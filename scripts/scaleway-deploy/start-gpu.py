#!/usr/bin/env python3
"""Start the Scaleway GPU instance on-demand.

Called by the DocklyOCR worker when a new job arrives and the GPU is powered off.
Waits until Ollama is responsive before returning.

Usage:
    python scripts/scaleway-deploy/start-gpu.py

Requires:
    SCW_ACCESS_KEY, SCW_SECRET_KEY, SCW_GPU_SERVER_ID, SCW_GPU_ZONE in .env or environment.

This script is designed to be called from the OCR worker before launching the
pipeline subprocess. If the GPU is already running, it returns immediately.
"""

from __future__ import annotations

import os
import sys
import time

import httpx


def get_env(key: str) -> str:
    val = os.environ.get(key, "")
    if not val:
        print(f"ERROR: {key} not set in environment", file=sys.stderr)
        sys.exit(1)
    return val


def main() -> int:
    ollama_url = os.environ.get("OLLAMA_URL", "http://localhost:11434")

    # Quick check: is Ollama already reachable?
    try:
        r = httpx.get(f"{ollama_url}/api/tags", timeout=3)
        if r.status_code == 200:
            print("GPU already running, Ollama responsive.")
            return 0
    except httpx.HTTPError:
        pass

    # Not reachable → need Scaleway API keys
    access_key = get_env("SCW_ACCESS_KEY")
    secret_key = get_env("SCW_SECRET_KEY")
    server_id = get_env("SCW_GPU_SERVER_ID")
    zone = os.environ.get("SCW_GPU_ZONE", "fr-par-2")

    api_base = f"https://api.scaleway.com/instance/v1/zones/{zone}"
    headers = {"X-Auth-Token": secret_key, "Content-Type": "application/json"}

    # Check current server state
    print(f"Checking GPU server {server_id} in {zone}...")
    with httpx.Client(timeout=15, headers=headers) as client:
        r = client.get(f"{api_base}/servers/{server_id}")
        r.raise_for_status()
        state = r.json()["server"]["state"]
        print(f"  Current state: {state}")

        if state == "running":
            print("  Already running, waiting for Ollama...")
        elif state in ("stopped", "stopped in place"):
            print("  Starting GPU server...")
            r = client.post(
                f"{api_base}/servers/{server_id}/action",
                json={"action": "poweron"},
            )
            r.raise_for_status()
            print("  Power-on command sent.")
        else:
            print(f"  WARNING: Unexpected state '{state}', attempting poweron anyway...")
            try:
                client.post(
                    f"{api_base}/servers/{server_id}/action",
                    json={"action": "poweron"},
                )
            except httpx.HTTPError:
                pass

    # Wait for Ollama to become responsive (max 5 min)
    print("Waiting for Ollama to come online...")
    deadline = time.time() + 300
    while time.time() < deadline:
        try:
            r = httpx.get(f"{ollama_url}/api/tags", timeout=5)
            if r.status_code == 200:
                models = [m["name"] for m in r.json().get("models", [])]
                if "glm-ocr:latest" in models:
                    print(f"  Ollama ready with models: {models}")
                    return 0
                print(f"  Ollama up but glm-ocr not loaded yet: {models}")
        except httpx.HTTPError:
            pass
        time.sleep(10)

    print("ERROR: Timeout waiting for Ollama after 5 minutes", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
