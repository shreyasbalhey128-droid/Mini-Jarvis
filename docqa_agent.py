"""
docqa_agent.py - ask questions about one document, for Mini-Jarvis (Atlas).

Your "index my documents" / "what do my documents say about..." search (Atlas) looks across many files.
This adds the other half: pick ONE document (drop a PDF on the Jarvis window, or name a file) and have a
conversation about just that document, with follow-up questions.

  Drop a PDF / Word / text file on the window, then:   "ask questions about it"     (or "ask about this document")
  By name:                                              "ask questions about my resume"   "let's go through the contract"
  Then ask freely:   "what are the deadlines"   "who is the author"   "what does it say about refunds"
                     "and the second one?"   "summarize it"
  One question without a mode:                          "what does my resume say about python"
                                                        "in the contract, when does it end"
  Leave:                                                "stop document mode"   (it also ends after 10 idle minutes)
  Other:                                                "what documents have you indexed"

How it answers: a short document (about 6,500 characters, a page or two) goes to the AI whole. A longer one is cut
into passages and only the best matching passages are sent. The passages are found with the embedding model you
already use for document search (nomic-embed-text); without it, it falls back to matching keywords.
The AI is told to answer ONLY from the document and to say so when the answer is not in it.
Scanned PDFs (pictures of text) can't be read.

This file does the reading, finding passages and understanding what you said. voice.py does the speaking.
"""
import math
import re
import time

WHOLE_DOC_CHARS = 6500        # up to this many characters the AI sees the whole document
CHUNK_CHARS = 1000
CHUNK_OVERLAP = 150
MAX_CHUNKS = 150              # about 150,000 characters; the rest of a huge file is ignored
TOP_PASSAGES = 4
IDLE_SECONDS = 600
MAX_TEXT = 400_000

DOC_WORDS = r"(?:document|doc|file|pdf|paper|report|resume|cv|notes|contract|essay|assignment|thesis|syllabus|book|manual|article|slides|chapter|letter|invoice|policy|agreement|proposal|brief|presentation)"
_THIS = {"this", "that", "it", "the document", "this document", "that document", "the file", "this file", "that file",
         "the pdf", "this pdf", "that pdf", "the doc", "this doc", "that doc", "the paper", "this paper"}
_QWORDS = ("what", "whats", "who", "whos", "when", "where", "why", "how", "which", "does", "do", "did", "is", "are",
           "was", "were", "can", "could", "will", "would", "tell me", "find", "give me", "list", "name", "explain",
           "describe", "summarize", "summarise", "show me", "any", "according", "and", "also", "so", "then")
_STOPWORDS = set("a an the of to in on at for and or is are was were be been it this that these those with as by from "
                 "what who when where why how which does do did can could will would about say says said document "
                 "file pdf tell me give list name explain describe any there their his her its".split())


def _norm(c):
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", c.lower().replace("'", "")).split())


# ---------------- cutting and ranking ----------------
def chunk(text, size=CHUNK_CHARS, overlap=CHUNK_OVERLAP, max_chunks=MAX_CHUNKS):
    """Passages of about `size` characters, cut at paragraph or sentence ends, overlapping a little."""
    text = re.sub(r"[ \t]+", " ", text.replace("\r", "")).strip()
    out, start = [], 0
    while start < len(text) and len(out) < max_chunks:
        end = min(len(text), start + size)
        if end < len(text):
            window = text[start + size // 2:end]
            cut = max(window.rfind("\n\n"), window.rfind(". "), window.rfind("\n"), window.rfind("? "))
            if cut != -1:
                end = start + size // 2 + cut + 1
        piece = text[start:end].strip()
        if piece:
            out.append(piece)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return out


def _words(text):
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOPWORDS and len(w) > 1]


def keyword_rank(chunks, question, k=TOP_PASSAGES):
    """Indexes of the passages that share the most (rare) words with the question. No AI model needed."""
    q = set(_words(question))
    if not q or not chunks:
        return list(range(min(k, len(chunks))))
    docs = [set(_words(c)) for c in chunks]
    df = {w: sum(1 for d in docs if w in d) for w in q}
    n = len(chunks)
    scores = []
    for i, d in enumerate(docs):
        s = sum(math.log(1 + n / df[w]) for w in q if w in d and df[w])
        scores.append((s, i))
    scores.sort(reverse=True)
    best = [i for s, i in scores[:k] if s > 0]
    return best or list(range(min(k, n)))


def cosine(a, b):
    num = sum(x * y for x, y in zip(a, b))
    den = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return num / den if den else 0.0


# ---------------- one document ----------------
class DocSession:
    def __init__(self, name, path):
        self.name, self.path = name, path
        self.text = ""
        self.chunks, self.vecs = [], None
        self.hist = []                       # [(question, answer)] for follow-ups
        self.t_last = time.time()

    @property
    def whole(self):
        return len(self.text) <= WHOLE_DOC_CHARS

    @property
    def active(self):
        return time.time() - self.t_last < IDLE_SECONDS

    def load(self, read_full):
        """Reads the file. False if no text could be read (empty, scanned, unsupported)."""
        try:
            text = read_full(self.path) or ""
        except Exception:
            text = ""
        self.text = text.strip()[:MAX_TEXT]
        if len(self.text) < 20:
            return False
        if not self.whole:
            self.chunks = chunk(self.text)
        self.t_last = time.time()
        return True

    def prepare(self, embed):
        """Embeds the passages (once). Leaves vecs None when the embedding model is missing -> keyword fallback."""
        if self.whole or self.vecs is not None or not self.chunks:
            return
        vecs = []
        for c in self.chunks:
            v = embed(c)
            if v is None:
                self.vecs = None
                return
            vecs.append(v)
        self.vecs = vecs

    def excerpts(self, question, embed):
        """The text the AI gets: the whole document, or the best passages in reading order."""
        if self.whole:
            return self.text
        idx = None
        if self.vecs is not None:
            qv = embed(question)
            if qv is not None:
                ranked = sorted(((cosine(qv, v), i) for i, v in enumerate(self.vecs)), reverse=True)
                idx = [i for s, i in ranked[:TOP_PASSAGES] if s >= 0.2] or None
        if idx is None:
            idx = keyword_rank(self.chunks, question)
        return "\n\n---\n\n".join(self.chunks[i] for i in sorted(idx))

    def remember(self, q, a):
        self.hist = (self.hist + [(q, a)])[-3:]
        self.t_last = time.time()


def build_prompt(name, question, excerpts, hist=()):
    earlier = ""
    if hist:
        earlier = "Earlier in this conversation:\n" + "\n".join(f"Q: {q}\nA: {a}" for q, a in hist) + "\n\n"
    return (f'You answer questions about a document called "{name}". Use ONLY the document text below. If the answer '
            "is not in it, say you can't find that in the document; never guess. Reply in 2 to 4 short sentences that "
            "sound natural read aloud: no lists, no markdown. Give numbers, dates and names exactly as written.\n\n"
            f"{earlier}Document text:\n<<<\n{excerpts}\n>>>\n\nQuestion: {question}")


def is_summary_question(c):
    t = _norm(c)
    return bool(re.search(r"\b(summari[sz]e|summary|overview|tldr|tl dr|gist)\b", t)
                or re.fullmatch(r"what(?: is|s) (?:it|this|the document|the file|the pdf) about|what(?: is|s) in (?:it|this|the document|the file)", t))


def looks_like_question(c):
    t = _norm(c)
    return bool(t) and (t.startswith(_QWORDS) or bool(re.search(r"\b(it say|does it|mention|the document|the file)\b", t)))


# ---------------- what you said ----------------
def is_this(x):
    """'it', 'this document', 'the pdf'... (the document you are on, not a name)."""
    return x.strip() in _THIS


def _docish(x):
    """Does this phrase sound like a file / document (so we don't treat 'the weather' as one)?"""
    x = x.strip()
    return x in _THIS or bool(re.search(r"\b" + DOC_WORDS + r"\b", x)) or bool(re.search(r"\.[a-z0-9]{2,4}\b", x))


def match_control(c, indexed=()):
    """{'action': 'enter'|'exit'|'list'|'scoped', 'target'?, 'question'?} or None.
    `indexed` = names of the documents Atlas has indexed (so 'my resume' is understood when it is one of them)."""
    t = _norm(c)
    if not t:
        return None
    if re.fullmatch(r"(?:please )?(?:stop|exit|end|leave|close|quit|forget)(?: the| this| that)? (?:document|doc|file|pdf)(?: mode| chat)?|"
                    r"(?:i m |im |i am |we re |we are |were )?(?:done|finished)(?: with)?(?: the| this| that)? (?:document|doc|file|pdf)|"
                    r"document mode off|stop asking about (?:the|this|that) (?:document|file|pdf)|back to normal chat", t):
        return {"action": "exit"}
    if re.fullmatch(r"(?:what|which) (?:documents?|files) (?:have you|did you|do you have|are) (?:indexed|index|know|learned|read|loaded)|"
                    r"list (?:my |the )?indexed (?:documents|files)|what s indexed|whats indexed|what files have you indexed", t):
        return {"action": "list"}

    def ok(x):
        return _docish(x) or any(n and n.lower().rsplit(".", 1)[0].replace("_", " ") in x for n in indexed)

    for pat in (r"(?:i want to |can i |let me |lets |let s |please )?(?:ask|chat|talk)(?: you)?(?: some| a few)?(?: questions?)? (?:about|on|with) (?P<x>.+)",
                r"(?:ask|answer) (?:me )?(?:my )?questions? (?:about|on|from) (?P<x>.+)",
                r"(?:lets|let s) (?:go through|talk about|look at|discuss|go over) (?P<x>.+)",
                r"(?:read|open|load) (?P<x>.+?) and (?:answer|let me ask)(?: my)? questions",
                r"(?:chat|talk) with (?P<x>.+)"):
        m = re.fullmatch(pat, t)
        if m and ok(m.group("x")):
            return {"action": "enter", "target": m.group("x")}
    if re.fullmatch(r"(?:enter |start |begin |open )?(?:document|doc|pdf) (?:mode|chat|q and a|q a|questions)", t):
        return {"action": "enter", "target": "this"}

    q_start = r"(?:what|when|who|where|how|why|which|does|do|is|are|did|can|tell me|find)"
    for pat in (r"what does (?P<x>.+?) say about (?P<q>.+)",
                rf"(?:according to|in|from|check) (?:my |the )?(?P<x>.+?),? (?P<q>{q_start}\b.+)",
                rf"ask (?:my |the )?(?P<x>.+?),? (?P<q>{q_start}\b.+)"):
        m = re.fullmatch(pat, t)
        if m and ok(m.group("x")) and m.group("x") not in _THIS:
            q = m.group("q")
            if pat.startswith("what does"):
                q = f"what does it say about {q}"
            return {"action": "scoped", "target": m.group("x"), "question": q}
    return None