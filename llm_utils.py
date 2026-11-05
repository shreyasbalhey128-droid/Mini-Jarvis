"""Tiny Ollama client shared by the planner, code executor and screen agent (no extra pip installs)."""
import json
import os
import urllib.request

_host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
if not _host.startswith("http"):
    _host = "http://" + _host
OLLAMA_URL = _host.rstrip("/") + "/api/chat"

DEFAULT_MODEL = "llama3.2:3b"


def chat(messages, model=DEFAULT_MODEL, json_mode=False, temperature=0.2,
         num_ctx=4096, timeout=600, images=None):
    """Send a chat to the local Ollama server and return the reply text.
    images: optional list of base64-encoded PNG/JPEG strings, attached to the last message
    (only works with vision models such as moondream or llama3.2-vision)."""
    messages = [dict(m) for m in messages]
    if images:
        messages[-1]["images"] = images
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": temperature, "num_ctx": num_ctx},
    }
    if json_mode:
        payload["format"] = "json"
    req = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))["message"]["content"]