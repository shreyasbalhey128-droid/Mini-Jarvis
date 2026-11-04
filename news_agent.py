"""
news_agent.py - Vortex's news for Jarvis: summaries, topic news, and follow-up questions.

It reads free RSS news feeds (no account or API key). voice.py asks the local Ollama model to turn the
headlines into a short spoken summary, and also gives you a once-a-day summary by itself.

Needs only: requests (you already have it).

Voice examples (the default news is India only):
    "what's the news"            "give me the tech news"        "world news"       "cricket news"
    "news of India"              "news about Delhi air quality" "latest on Tesla"
    "turn off daily news"        "daily news at 8 am"           "turn on daily news"

FOLLOW-UP QUESTIONS - for 10 minutes after Vortex reads you news, you can say:
    "tell me more about the second story"      "more about number 3"      "the last one"
    "next story"                               "list the stories"         "tell me more" (he asks which one)
    "why did that happen"                      "is that serious"          "who is responsible"
    "tell me more about Delhi air quality"     (a new topic: he searches the news for it)
The numbers match the numbered headlines Jarvis writes in the transcript.

Test from the terminal:  python news_agent.py   (or: python news_agent.py tech)
                         python news_agent.py topic delhi air quality
"""

import html
import re
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

import requests

# ----------------------------------------------------------------------------
# Settings - edit freely. A feed that fails is simply skipped.
# ----------------------------------------------------------------------------
FEEDS = {
    "top": [                       # the default news and the daily summary: India only
        ("Times of India", "https://timesofindia.indiatimes.com/rssfeedstopstories.cms"),
        ("The Hindu", "https://www.thehindu.com/news/national/feeder/default.rss"),
        ("BBC India", "https://feeds.bbci.co.uk/news/world/asia/india/rss.xml"),
    ],
    "india": [
        ("Times of India", "https://timesofindia.indiatimes.com/rssfeedstopstories.cms"),
        ("The Hindu", "https://www.thehindu.com/news/national/feeder/default.rss"),
        ("BBC India", "https://feeds.bbci.co.uk/news/world/asia/india/rss.xml"),
    ],
    "world": [
        ("BBC", "https://feeds.bbci.co.uk/news/world/rss.xml"),
        ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
    ],
    "tech": [
        ("BBC", "https://feeds.bbci.co.uk/news/technology/rss.xml"),
        ("The Verge", "https://www.theverge.com/rss/index.xml"),
        ("The Hindu", "https://www.thehindu.com/sci-tech/technology/feeder/default.rss"),
    ],
    "business": [
        ("BBC", "https://feeds.bbci.co.uk/news/business/rss.xml"),
        ("The Hindu", "https://www.thehindu.com/business/feeder/default.rss"),
    ],
    "science": [
        ("BBC", "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml"),
        ("ScienceDaily", "https://www.sciencedaily.com/rss/top/science.xml"),
    ],
    "sports": [
        ("BBC", "https://feeds.bbci.co.uk/sport/rss.xml"),
        ("The Hindu", "https://www.thehindu.com/sport/feeder/default.rss"),
    ],
    "cricket": [
        ("BBC", "https://feeds.bbci.co.uk/sport/cricket/rss.xml"),
        ("The Hindu", "https://www.thehindu.com/sport/cricket/feeder/default.rss"),
    ],
}

LABELS = {
    "top": "the top news from India", "india": "the news from India", "world": "the world news",
    "tech": "the tech news", "business": "the business news", "science": "the science news",
    "sports": "the sports news", "cricket": "the cricket news",
}

TOPIC_URL = "https://news.google.com/rss/search"      # news about any subject (headlines only)

PER_FEED = 5                 # headlines taken from each feed
MAX_ITEMS = 8                # headlines given to the model (the small model does best with few)
FETCH_TIMEOUT = 10
SUMMARY_CHARS = 220          # how much of each story's blurb the model sees
MAX_ARTICLE_CHARS = 5000     # how much of one article the model reads for a follow-up
MIN_ARTICLE_CHARS = 500      # shorter than this and the page is not a real article

FOLLOWUP_WINDOW = 600        # seconds after the last news answer during which follow-ups work

DEFAULT_HOUR = 9             # the daily summary comes after this hour (24h clock)...
LATEST_HOUR = 20             # ...and never after this one

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# ----------------------------------------------------------------------------
# Reading feeds
# ----------------------------------------------------------------------------
_TAG_RE = re.compile(r"<[^>]+>")


def _local(tag):
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _text(el):
    """All the text inside an element, tags removed, entities decoded, spaces tidied."""
    raw = "".join(el.itertext()) if el is not None else ""
    raw = html.unescape(_TAG_RE.sub(" ", html.unescape(raw)))
    return " ".join(raw.split())


def parse_feed(xml_bytes, source):
    """RSS or Atom -> list of {'title','summary','source','link'}. Bad XML gives an empty list."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    items = []
    for node in root.iter():
        if _local(node.tag) not in ("item", "entry"):
            continue
        fields = {}
        for child in node:
            name = _local(child.tag)
            if name == "link":                                   # RSS: <link>text</link>; Atom: <link href=.../>
                link = (child.get("href") or "".join(child.itertext())).strip()
                if link and (not fields.get("link") or child.get("rel") in (None, "alternate")):
                    fields["link"] = link
            elif name in ("title", "description", "summary", "content", "encoded") and name not in fields:
                fields[name] = _text(child)
        title = fields.get("title", "")
        if not title:
            continue
        blurb = fields.get("description") or fields.get("summary") or fields.get("encoded") or fields.get("content") or ""
        if blurb.lower().startswith(title.lower()[:40]):        # some feeds repeat the headline
            blurb = blurb[len(title):].strip(" .:-")
        items.append({"title": title, "summary": blurb[:SUMMARY_CHARS].strip(), "source": source,
                      "link": fields.get("link", "")})
    return items


def fetch_feed(url, source, timeout=FETCH_TIMEOUT):
    """Downloads one feed. Raises requests exceptions on network problems."""
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    r.raise_for_status()
    return parse_feed(r.content, source)


def _norm(title):
    return re.sub(r"[^a-z0-9]", "", title.lower())[:60]


def gather(category="top", limit=MAX_ITEMS, fetcher=fetch_feed):
    """The newest headlines for a category, mixed across sources, without duplicates.

    Returns a list (empty if no feed had anything). If EVERY feed failed because of the network,
    the first network error is raised so the caller can say "I can't reach the internet"."""
    feeds = FEEDS.get(category) or FEEDS["top"]
    errors = []

    def one(feed):
        source, url = feed
        try:
            return fetcher(url, source)[:PER_FEED]
        except Exception as e:                      # one broken feed must not stop the others
            errors.append(e)
            return []

    with ThreadPoolExecutor(max_workers=len(feeds)) as pool:
        results = list(pool.map(one, feeds))

    mixed, seen = [], set()
    for rank in range(PER_FEED):                    # take 1st of each feed, then 2nd of each, ...
        for items in results:
            if rank < len(items):
                key = _norm(items[rank]["title"])
                if key and key not in seen:
                    seen.add(key)
                    mixed.append(items[rank])
    mixed = mixed[:limit]

    if not mixed and errors:
        net = [e for e in errors if isinstance(e, requests.exceptions.RequestException)]
        if len(net) == len(feeds):
            raise net[0]
    return mixed


def topic_items(topic, n=MAX_ITEMS, get=None):
    """Recent headlines about ANY subject from Google News. Headlines only (Google's links can't be read
    as articles), so these items are marked no_fetch. Raises requests errors when offline."""
    get = get or requests.get
    r = get(TOPIC_URL, params={"q": topic, "hl": "en-IN", "gl": "IN", "ceid": "IN:en"},
            headers={"User-Agent": USER_AGENT}, timeout=FETCH_TIMEOUT)
    r.raise_for_status()
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError:
        return []
    out, seen = [], set()
    for node in root.iter("item"):
        title = (node.findtext("title") or "").strip()
        source = (node.findtext("source") or "").strip()
        if source and title.endswith(" - " + source):
            title = title[: -len(source) - 3]
        key = _norm(title)
        if not title or key in seen:
            continue
        seen.add(key)
        out.append({"title": title, "summary": "", "source": source or "Google News",
                    "link": (node.findtext("link") or "").strip(), "no_fetch": True})
        if len(out) >= n:
            break
    return out


# ----------------------------------------------------------------------------
# Words: which words matter when comparing a question to the news
# ----------------------------------------------------------------------------
_STOP = set("""about above after again against also another because been before being between both but could does
doing down during each from further have having here into itself just more most much must only other over same
should some such than that their them then there these they this those through under until very what when where
which while will with would your you are was were has had how why who for and the its out off not new now one two
say says said get got may too use way can did all any our his her him she tell give more tell""".split())


def sig(text):
    """The words that matter in a sentence (no little words), roughly stemmed, as a set."""
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w[:-1] if len(w) > 4 and w.endswith("s") else w for w in words if len(w) >= 3 and w not in _STOP}


# ----------------------------------------------------------------------------
# Turning headlines into speech
# ----------------------------------------------------------------------------
def build_prompt(items, category="top", label=None):
    """The question for the local model: summarize these headlines."""
    lines = []
    for it in items:
        blurb = f" {it['summary']}" if it.get("summary") else ""
        lines.append(f"- {it['title']}.{blurb}")
    return (
        f"You are a news reader speaking out loud. Using ONLY the headlines below, write a spoken summary of "
        f"{label or LABELS.get(category, 'the news')} in 4 or 5 short, plain sentences covering the biggest stories. "
        "No lists, no bullet points, no markdown, no emojis, and do not add any fact that is not in the headlines. "
        "Start straight with the news, with no introduction.\n\nHeadlines:\n" + "\n".join(lines))


def build_followup_prompt(title, text, question=None, mode="more"):
    """The question for the local model about ONE story (or a set of headlines).
    mode 'more' = tell me more; mode 'ask' = answer this question."""
    rules = ("Use ONLY the news text below and do not add any fact that is not in it. "
             "No lists, no bullet points, no markdown, no emojis. Start straight with the answer.")
    if mode == "ask" and question:
        head = ("You are a news reader answering a listener's question out loud. " + rules +
                " If the text does not answer the question, say you do not have that detail. "
                "Answer in 2 to 4 short, plain sentences.\n\nQuestion: " + question)
    else:
        head = ("You are a news reader speaking out loud. " + rules +
                " Tell the listener more about this story: details that go beyond the headline, "
                "in 3 to 5 short, plain sentences.")
    return f"{head}\n\nNews text ({title}):\n{text}"


def clean_summary(text):
    """Tidies the model's answer: no markdown, no 'Here is your summary:' line."""
    text = re.sub(r"[*#_`>]+", "", text or "")
    text = re.sub(r"^\s*(?:[-•]|\d+[.)])\s+", "", text, flags=re.MULTILINE)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if lines and len(lines[0]) < 90 and lines[0].endswith(":"):
        lines = lines[1:]
    return " ".join(lines).strip()


def fallback_text(items, count=4):
    """If the model is not available: just read the first headlines."""
    return "Here are the top headlines. " + " ".join(it["title"].rstrip(".") + "." for it in items[:count])


def headline_log(items):
    """One numbered line per story, for the transcript (the numbers are what you say in follow-ups)."""
    return "\n".join(f"{i}. {it['title']}  ({it['source']})" for i, it in enumerate(items, 1))


def list_text(items, ask=False, n=6):
    """The stories as speech: 'Number 1, ... Number 2, ...'"""
    parts = [f"Number {i}, {it['title'].rstrip('.')}." for i, it in enumerate(items[:n], 1)]
    return ("Which one? " if ask else "Here are the stories. ") + " ".join(parts)


# ----------------------------------------------------------------------------
# Reading one story for a follow-up
# ----------------------------------------------------------------------------
def _mentions_title(page, title):
    words = sig(title)
    if not words:
        return True
    have = sig(page)
    return len(words & have) >= min(2, len(words))


def story_text(item, page_text=None):
    """(text, full) - the best text there is for one story. full=False means only the headline and blurb
    (the article page could not be read: paywall, cookie wall, or a link that is not an article).
    page_text(url) -> the readable text of a web page. The result is kept on the item."""
    if item.get("text"):
        return item["text"], bool(item.get("full"))
    text = (item["title"] + ". " + item.get("summary", "")).strip()
    full = False
    if page_text and item.get("link") and not item.get("no_fetch"):
        try:
            page = page_text(item["link"]) or ""
        except Exception:
            page = ""
        # keep paragraph-like lines only (menus and buttons are short)
        body = " ".join(ln.strip() for ln in page.splitlines() if len(ln.strip()) >= 60)
        if len(body) >= MIN_ARTICLE_CHARS and _mentions_title(body, item["title"]):
            text, full = body[:MAX_ARTICLE_CHARS], True
    item["text"], item["full"] = text, full
    return text, full


class NewsContext:
    """What Vortex last read to you, so you can ask follow-up questions about it."""

    def __init__(self, window=FOLLOWUP_WINDOW):
        self.window = window
        self.clear()

    def clear(self):
        self.items, self.label, self.summary = [], "", ""
        self.active, self.topic, self.t = None, False, 0.0

    def set(self, items, label="", summary="", topic=False):
        self.items, self.label, self.summary, self.topic = list(items), label, summary, topic
        self.active, self.t = None, time.time()

    def touch(self):
        self.t = time.time()

    def fresh(self, now=None):
        return bool(self.items) and ((now if now is not None else time.time()) - self.t) < self.window

    def context_text(self):
        return "\n".join(f"{i}. {it['title']}. {it.get('summary', '')}".strip() for i, it in enumerate(self.items, 1))


# ----------------------------------------------------------------------------
# Understanding what you said
# ----------------------------------------------------------------------------
_CAT_WORDS = {
    "tech": "tech", "technology": "tech",
    "business": "business", "market": "business", "markets": "business", "finance": "business",
    "economy": "business", "science": "science", "sports": "sports", "sport": "sports",
    "cricket": "cricket", "india": "india", "indian": "india", "national": "india",
    "world": "world", "international": "world", "global": "world",
}
_CAT = r"(?:(" + "|".join(sorted(_CAT_WORDS, key=len, reverse=True)) + r") )?"
_NEWS_RES = [
    re.compile(r"^(?:(?:give|tell|read|show|get) (?:me )?)?(?:the |today(?:'s| s|s)? |my )?"
               r"(?:latest |top |daily |morning |current )?" + _CAT +
               r"(?:news|headlines)(?: (?:summary|briefing|update|roundup|headlines))?"
               r"(?: for today| today| please| now)?$"),
    re.compile(r"^what(?:'s| is| s) (?:the |today(?:'s| s|s)? )?(?:latest |top |current )?" + _CAT +
               r"news(?: today| now| right now)?$"),
    re.compile(r"^(?:any|is there any|is there some) (?:new |latest |big )?" + _CAT + r"news(?: today| now)?$"),
    re.compile(r"^(?:summari[sz]e|summary of) (?:the |today(?:'s| s|s)? )?" + _CAT + r"news$"),
]
_INDIA_RES = [
    re.compile(r"^(?:(?:give|tell|read|show|get) (?:me )?|what(?:'s| is| s) )?(?:the )?"
               r"(?:latest |top |today(?:'s| s|s)? |current )?(?:news|headlines) (?:of|from|in|about) "
               r"(?:india|indian)(?: today| now)?$"),
    re.compile(r"^(?:the )?(?:latest |top )?india(?:'s| s)? (?:news|headlines)(?: today| now)?$"),
]
_TOPIC_RES = [
    re.compile(r"^(?:(?:what(?:'s| is| s) )?the )?(?:latest )?news (?:about|on|for|from|regarding) (.+)$"),
    re.compile(r"^(?:any )?(?:latest )?news (?:about|on|for|from|regarding) (.+)$"),
    re.compile(r"^(?:what(?:'s| is| s) )?(?:the )?latest (?:news )?(?:about|on|for|regarding) (.+)$"),
    re.compile(r"^(?:any |the |what(?:'s| is| s) the )?(?:latest )?updates? (?:about|on|for|regarding) (.+)$"),
]
_ON_OFF_RES = [
    re.compile(r"^(?:turn |switch )?(on|off) (?:the )?(?:daily|morning) news(?: summary| briefing)?$"),
    re.compile(r"^(?:daily|morning) news(?: summary| briefing)? (on|off)$"),
    re.compile(r"^(stop|disable) (?:the )?(?:daily|morning) news(?: summary| briefing)?$"),
    re.compile(r"^(enable|start) (?:the )?(?:daily|morning) news(?: summary| briefing)?$"),
]
_NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
              "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
_HOUR_RE = re.compile(
    r"^(?:set |change )?(?:the )?(?:daily|morning) news(?: summary| briefing)? (?:to|at|for) "
    r"(\d{1,2}|" + "|".join(_NUM_WORDS) + r")(?:[: ]?\d\d)?\s*(a m|p m|am|pm)?$")


def _tidy(c):
    return " ".join((c or "").lower().replace("please", "").split())


def parse(c):
    """Returns {'kind':'summary','cat':...}, {'kind':'topic','topic':...}, {'kind':'daily','on':bool},
    {'kind':'hour','hour':int} or None."""
    c = _tidy(c)
    if not c:
        return None
    for rx in _ON_OFF_RES:
        m = rx.match(c)
        if m:
            return {"kind": "daily", "on": m.group(1) in ("on", "enable", "start")}
    m = _HOUR_RE.match(c)
    if m:
        raw, ampm = m.group(1), (m.group(2) or "").replace(" ", "")
        hour = _NUM_WORDS.get(raw) or int(raw)
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        return {"kind": "hour", "hour": hour}
    if any(rx.match(c) for rx in _INDIA_RES):
        return {"kind": "summary", "cat": "india"}
    for rx in _NEWS_RES:
        m = rx.match(c)
        if m:
            word = m.group(1) if m.groups() else None
            return {"kind": "summary", "cat": _CAT_WORDS.get(word, "top")}
    for rx in _TOPIC_RES:
        m = rx.match(c)
        if m:
            topic = re.sub(r"\s+(?:today|now|right now)$", "", m.group(1).strip())
            if topic in _CAT_WORDS:
                return {"kind": "summary", "cat": _CAT_WORDS[topic]}
            return {"kind": "topic", "topic": topic} if topic else None
    return None


# ---------------- follow-ups ----------------
_ORD_WORDS = {"first": 0, "second": 1, "third": 2, "fourth": 3, "fifth": 4, "sixth": 5, "seventh": 6,
              "eighth": 7, "last": -1}
_ORD_FILLER = {"the", "story", "stories", "headline", "article", "item", "news", "one", "about", "on", "of",
               "please", "that", "a", "number", "no", "this", "tell", "me", "more"}
_LABELS = {"number", "no", "story", "headline", "article", "item"}

_LIST_RE = re.compile(r"^(?:list|read|say|show) (?:them|the stories|those|the titles|the headlines again|them again)"
                      r"(?: out| again)?$|^which (?:stories|ones|headlines)$|^what (?:stories|headlines) "
                      r"(?:do you have|are there|were there)$")
_NEXT_RE = re.compile(r"^(?:the )?next (?:story|headline|article)$|^(?:go to |move to |read )(?:the )?next "
                      r"(?:story|headline|article)$")
_MORE_RE = re.compile(
    r"^(?:(?:can you |could you )?(?:tell|give|read|show) me |i (?:want|would like|need) |go |dig )?"
    r"(?:some |a little |a bit )?(?:more|deeper|details?|the details|the full story|the whole story|more details?|"
    r"more info(?:rmation)?|more on)(?: (?:about|on|of|regarding|into))?(?: (.*))?$")
_EXPLAIN_RE = re.compile(r"^(?:elaborate|expand|explain|dig)(?: more)?(?: (?:on|about|into))?(?: (.*))?$")
_PICK_RE = re.compile(r"^(?:what about|and|how about|read|open|go to|the)? ?(.*)$")
_QSTART = re.compile(r"^(?:and |but |so |ok |okay |wait |hmm )?(?:why|how|who|whom|whose|when|where|what|which|is|are|"
                     r"was|were|does|do|did|will|would|can|could|should|has|have)\b")
_REF = {"that", "this", "they", "them", "their", "these", "those", "he", "she", "his", "her", "story",
        "article", "report", "incident"}
_STANDALONE = {
    "why", "why is that", "why did that happen", "why did this happen", "how come", "what happened",
    "what does that mean", "what does this mean", "is that serious", "is it serious", "how serious is it",
    "how serious is that", "how bad is it", "how bad is that", "who is responsible", "who did it",
    "who is behind it", "who is behind that", "what next", "what happens next", "what is next", "what's next",
    "whats next", "when did that happen", "where did that happen", "who said that", "any updates",
    "what are the details", "explain that", "explain this", "explain it", "what else", "anything else",
    "is that true", "is this true", "what is the reason", "what's the reason", "how did that happen",
    "how did this happen", "who was involved", "who is involved", "how many people", "where is that",
}


def _ordinal(rest):
    """'the second one' -> 1, 'number three' -> 2, 'the last story' -> -1, else None."""
    labelled = bool(set(rest.split()) & _LABELS)
    idx, extra = None, []
    for w in rest.split():
        w2 = re.sub(r"(st|nd|rd|th)$", "", w) if re.fullmatch(r"\d+(?:st|nd|rd|th)", w) else w
        if w2 in _ORD_WORDS:
            idx = _ORD_WORDS[w2]
        elif w2.isdigit() and 1 <= int(w2) <= 20:
            idx = int(w2) - 1
        elif w2 in _NUM_WORDS and labelled:
            idx = _NUM_WORDS[w2] - 1
        elif w2 in _ORD_FILLER:
            continue
        else:
            extra.append(w)
    return idx if idx is not None and not extra else None


def match_story(rest, items):
    """Which story does 'delhi air quality' mean? Index of the best matching headline, or None."""
    words = sig(rest)
    if not words:
        return None
    best, best_i = 0, None
    for i, it in enumerate(items):
        score = len(words & sig(it["title"]))
        if score > best:
            best, best_i = score, i
    if best >= 2 or (best >= 1 and best >= 0.6 * len(words)):
        return best_i
    return None


_THIS = {"that", "this", "it", "that story", "this story", "the story", "that one", "this one", "that article"}


def parse_followup(c, ctx):
    """What you said after a news answer.
    {'kind':'list'} | {'kind':'more'} | {'kind':'story','index':i or 'next'} |
    {'kind':'topic','topic':t} | {'kind':'ask','question':q} | None (not a follow-up)."""
    c = _tidy(c)
    if not c or ctx is None or not ctx.fresh():
        return None
    if _LIST_RE.match(c):
        return {"kind": "list"}
    if _NEXT_RE.match(c):
        return {"kind": "story", "index": "next"}

    for rx, allow_topic in ((_MORE_RE, True), (_EXPLAIN_RE, False)):
        m = rx.match(c)
        if not m:
            continue
        rest = (m.group(1) or "").strip()
        rest = re.sub(r"^(?:more )?(?:details?|info(?:rmation)?)(?: (?:about|on|of))? ?", "", rest).strip()
        if not rest or rest in _THIS:
            return {"kind": "more"}
        idx = _ordinal(rest)
        if idx is None:
            idx = match_story(rest, ctx.items)
        if idx is not None:
            return {"kind": "story", "index": idx}
        if allow_topic:
            topic = re.sub(r"^(?:the )?(?:story|news|latest|stories)? ?(?:about|on|of) ", "", rest).strip()
            return {"kind": "topic", "topic": topic}
        return None

    pick = _PICK_RE.match(c)                        # just "the second one", "number three", "what about the last one"
    if pick:
        idx = _ordinal(pick.group(1).strip())
        if idx is not None:
            return {"kind": "story", "index": idx}

    if c in _STANDALONE:
        return {"kind": "ask", "question": c}
    if _QSTART.match(c):
        mine, theirs = sig(c), sig(ctx.context_text() + " " + ctx.summary)
        overlap = len(mine & theirs)
        if overlap >= 2 or (overlap >= 1 and set(c.split()) & _REF):
            return {"kind": "ask", "question": c}
    return None


# ----------------------------------------------------------------------------
# Daily summary helpers
# ----------------------------------------------------------------------------
def hour_ok(hour):
    return 0 <= hour < LATEST_HOUR


def hour_phrase(hour):
    return f"{hour % 12 or 12} {'AM' if hour < 12 else 'PM'}"


def daily_due(now, hour, last_date):
    """True if today's summary has not been given yet and it is between `hour` and LATEST_HOUR.
    now = time.localtime(); last_date = 'YYYY-MM-DD' of the last delivery (or None)."""
    return last_date != time.strftime("%Y-%m-%d", now) and hour <= now.tm_hour < LATEST_HOUR


if __name__ == "__main__":
    # Terminal test: prints the headlines (the model is only used inside Jarvis).
    args = sys.argv[1:]
    try:
        if args and args[0] == "topic":
            found = topic_items(" ".join(args[1:]) or "india")
        else:
            found = gather(args[0] if args else "top")
    except requests.exceptions.RequestException as e:
        print("No internet:", e)
        sys.exit(1)
    print(headline_log(found) if found else "No headlines found. Check the feed links in FEEDS.")