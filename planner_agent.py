"""
Multi-step goal planner for Jarvis.

Give it a goal like "research the top 3 libraries for vector indexing, summarize them
and create a comparison document". It asks the local LLM for a short plan, runs each
step with a tool, retries a failed step with a corrected input, and returns the result
(saving a Markdown document in ~/Documents/Jarvis when the plan writes one).

Extra pip installs:  pip install ddgs   (web search; the older name duckduckgo_search also works)
Run it in a thread (start_goal) because a 3B model takes a while per step.
"""
import html
import importlib
import json
import os
import re
import threading
import urllib.request

from llm_utils import chat, DEFAULT_MODEL

PLAN_MODEL = DEFAULT_MODEL
OUT_DIR = os.path.join(os.path.expanduser("~"), "Documents", "Jarvis")
MAX_STEPS = 6
MAX_STEP_RETRIES = 2
SUBST_LIMIT = 3000            # chars of a previous result fed into the next step

cancel_event = threading.Event()


# ------------------------------------------------------------------ tools
TOOLS = {}


def register_tool(name, description):
    """Decorator. Tool signature: fn(input_text, ctx) -> str.  Override defaults by re-registering a name."""
    def deco(fn):
        TOOLS[name] = {"fn": fn, "desc": description}
        return fn
    return deco


def _strip_html(raw):
    raw = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", html.unescape(raw)).strip()


def _fetch(url, limit=1500):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Jarvis)"})
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read(400_000).decode("utf-8", errors="replace")
    return _strip_html(raw)[:limit]


def _search(query, n=5):
    try:
        from ddgs import DDGS
    except ImportError:
        raise ImportError("Web search requires the ddgs package. Install it with: pip install ddgs") from None
    with DDGS() as d:
        return list(d.text(query, max_results=n))


@register_tool("research", "Search the web for a topic and read the top pages. Input: a search query.")
def _research(query, ctx):
    hits = _search(query, 4)
    if not hits:
        raise RuntimeError("search returned nothing")
    parts = []
    for h in hits[:3]:
        try:
            body = _fetch(h["href"])
        except Exception:
            body = h.get("body", "")
        parts.append(f"SOURCE: {h['title']} ({h['href']})\n{body}")
    return "\n\n".join(parts)


@register_tool("read_page", "Read the text of one web page. Input: a URL.")
def _read_page(url, ctx):
    return _fetch(url.strip(), limit=3500)


@register_tool("ask_llm", "Think, summarize, compare or rewrite. Input: a full instruction that includes the text to work on.")
def _ask(prompt, ctx):
    return chat([{"role": "user", "content": prompt}], model=PLAN_MODEL, temperature=0.3)


@register_tool("write_document", "Save the final document. Input: the full document text in Markdown.")
def _write(text, ctx):
    os.makedirs(OUT_DIR, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", ctx["goal"].lower()).strip("-")[:50] or "document"
    path = os.path.join(OUT_DIR, f"{slug}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    ctx["file"] = path
    return f"Saved to {path}"


@register_tool("run_python", "Write and run a Python script for a calculation or file job. Input: what the script should do.")
def _run_python(task, ctx):
    code_executor = importlib.import_module("code_executor")
    res = code_executor.solve(task, cancel=cancel_event)
    if not res.ok:
        raise RuntimeError(res.stderr[-300:])
    return res.stdout.strip() or "(script ran, no output)"


# ------------------------------------------------------------------ planning
def _tool_list():
    return "\n".join(f'- "{n}": {t["desc"]}' for n, t in TOOLS.items())


def make_plan(goal, tries=3):
    system = (
        "You break a goal into a short plan of at most %d steps. Reply with JSON only:\n"
        '{"steps":[{"tool":"<tool name>","input":"<text>","why":"<few words>"}]}\n'
        "Tools:\n%s\n\n"
        'Inside an input you may write {prev} for the previous step\'s result or {step1}, {step2}... '
        "for a specific earlier result. Use research to gather facts, ask_llm to summarize or "
        "compare, and write_document last if a document is wanted. Keep it minimal."
    ) % (MAX_STEPS, _tool_list())
    for _ in range(tries):
        try:
            raw = chat([{"role": "system", "content": system},
                        {"role": "user", "content": goal}],
                       model=PLAN_MODEL, json_mode=True, temperature=0.1)
            steps = json.loads(raw).get("steps", [])
            steps = [s for s in steps if isinstance(s, dict)
                     and s.get("tool") in TOOLS and isinstance(s.get("input"), str)]
            if steps:
                return steps[:MAX_STEPS]
        except Exception:
            continue
    return []


def _fill(text, results):
    def sub(m):
        key = m.group(1)
        if key == "prev":
            val = results[-1] if results else ""
        else:
            i = int(key[4:]) - 1
            val = results[i] if 0 <= i < len(results) else ""
        return val[:SUBST_LIMIT]
    return re.sub(r"\{(prev|step\d+)\}", sub, text)


def _repair_input(step, error):
    prompt = (f'The tool "{step["tool"]}" failed.\nInput was: {step["input"][:500]}\n'
              f"Error: {error}\nReply with ONLY a corrected input text.")
    return chat([{"role": "user", "content": prompt}], model=PLAN_MODEL, temperature=0.3).strip()


def run_goal(goal, say=print):
    """Plan, execute, recover. Returns dict(ok, answer, file, steps)."""
    cancel_event.clear()
    say("Let me plan that out.")
    plan = make_plan(goal)
    if not plan:
        return {"ok": False, "answer": "I couldn't turn that into a plan.", "file": "", "steps": []}

    ctx = {"goal": goal, "file": ""}
    results, log = [], []
    for i, step in enumerate(plan, 1):
        if cancel_event.is_set():
            return {"ok": False, "answer": "Stopped.", "file": ctx["file"], "steps": log}
        say(f"Step {i} of {len(plan)}: {step.get('why') or step['tool']}.")
        inp = _fill(step["input"], results)
        out, err = None, None
        for attempt in range(MAX_STEP_RETRIES + 1):
            try:
                out = TOOLS[step["tool"]]["fn"](inp, ctx)
                break
            except Exception as e:
                err = str(e)
                if attempt < MAX_STEP_RETRIES:
                    say("That step hit a problem. Adjusting and retrying.")
                    inp = _repair_input({"tool": step["tool"], "input": inp}, err)
        if out is None:
            log.append((i, step["tool"], f"FAILED: {err}"))
            return {"ok": False,
                    "answer": f"I got stuck on step {i}: {err}",
                    "file": ctx["file"], "steps": log}
        results.append(out)
        log.append((i, step["tool"], "ok"))

    final = results[-1]
    if ctx["file"]:
        answer = f"All done. I saved the finished document to {ctx['file']}."
    else:
        answer = final[:800]
    return {"ok": True, "answer": answer, "file": ctx["file"], "steps": log}


# ------------------------------------------------------------------ voice-command glue
_VERBS = re.compile(r"\b(research|summari[sz]e|compare|create|write|save|find out|list|analy[sz]e)\b", re.I)


def looks_like_goal(text):
    t = text.lower()
    return t.startswith("goal") or len(_VERBS.findall(t)) >= 2


def start_goal(text, say=print, on_done=None):
    """Run in the background so the UI keeps working. on_done(result_dict) fires at the end."""
    goal = re.sub(r"^\s*goal[:,]?\s*", "", text, flags=re.I)

    def work():
        res = run_goal(goal, say=say)
        say(res["answer"])
        if on_done:
            on_done(res)

    threading.Thread(target=work, daemon=True).start()


def cancel():
    """Call this from your 'stop' command."""
    cancel_event.set()