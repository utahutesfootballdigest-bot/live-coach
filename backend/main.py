import asyncio
import base64
import json
import os
import sys
import random
import requests
from datetime import datetime, timezone

# Ensure backend modules are importable when run from project root
sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse

# Load from .env.example locally; on Railway, env vars are set directly
_env_path = os.path.join(os.path.dirname(__file__), "..", ".env.example")
if os.path.exists(_env_path):
    load_dotenv(dotenv_path=_env_path)

from transcriber import Transcriber
from coach import CoachingEngine
from roleplay import RoleplayCustomer
from transcript_store import save_transcript, get_latest_tuning

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
DEEPGRAM_API_KEY = os.environ["DEEPGRAM_API_KEY"]

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")
_STAGE_ORDER = ["intro", "discovery", "collect_info", "build_system", "closing"]
_SERVER_STARTED_AT = datetime.now(timezone.utc).isoformat()


def _allowed_stage_advance(current: str, target: str) -> str | None:
    """Enforce single-step stage advancement. Returns the allowed next stage,
    or None if the jump is not allowed. Never skips more than one stage.
    E.g. discovery can only go to collect_info, never directly to build_system or closing."""
    if current not in _STAGE_ORDER or target not in _STAGE_ORDER:
        return None
    cur_idx = _STAGE_ORDER.index(current)
    tgt_idx = _STAGE_ORDER.index(target)
    if tgt_idx <= cur_idx:
        return None  # can't go backward or stay same
    if tgt_idx == cur_idx + 1:
        return target  # one step forward — allowed
    # Multi-step jump — clamp to next stage only
    next_stage = _STAGE_ORDER[cur_idx + 1]
    print(f"[stage] BLOCKED skip {current} → {target}, clamped to {next_stage}")
    return next_stage

@app.get("/")
async def serve_index():
    return FileResponse(os.path.join(_FRONTEND_DIR, "index.html"))


@app.post("/api/feedback")
async def post_feedback(request: Request):
    """Receive post-call feedback via REST (reliable even after WebSocket session ends).
    Finds the most recent transcript and attaches the feedback."""
    import json as _json
    from transcript_store import TRANSCRIPTS_DIR

    body = await request.json()
    feedback = body.get("feedback", "")

    # Find the most recent transcript that has no feedback yet
    files = sorted(TRANSCRIPTS_DIR.glob("transcript_*.json"), reverse=True)
    attached = False
    for f in files[:5]:
        try:
            t = _json.loads(f.read_text())
            if not t.get("user_feedback"):
                t["user_feedback"] = feedback
                f.write_text(_json.dumps(t, indent=2))
                print(f"[feedback] attached to {f.name}: {feedback[:100]}")
                attached = True
                break
        except Exception:
            continue

    if not attached and feedback:
        print(f"[feedback] no pending transcript found, feedback lost: {feedback[:100]}")

    return {"ok": True, "attached": attached}



@app.post("/api/claim-sale")
async def claim_sale(request: Request):
    """Record a claimed sale. Stores locally and will push to SharePoint when configured."""
    body = await request.json()
    rep = body.get("rep", "").strip()
    account_id = body.get("account_id", "").strip()
    phone = body.get("phone", "").strip()
    channel = body.get("channel", "").strip()

    if not rep or not account_id or not channel:
        return {"error": "rep, account_id, and channel are required."}, 400

    from datetime import datetime, timezone
    sale = {
        "rep": rep,
        "date": datetime.now(timezone.utc).strftime("%m/%d/%Y"),
        "account_id": account_id,
        "phone": phone,
        "channel": channel,
        "claimed_at": datetime.now(timezone.utc).isoformat(),
    }

    # Store locally in a JSON file
    sales_file = os.path.join(os.path.dirname(__file__), "claimed_sales.json")
    existing = []
    if os.path.exists(sales_file):
        try:
            with open(sales_file, "r") as f:
                existing = json.loads(f.read())
        except Exception:
            existing = []
    existing.append(sale)
    with open(sales_file, "w") as f:
        f.write(json.dumps(existing, indent=2))

    print(f"[sale] claimed: {rep} | {account_id} | {phone} | {channel}")
    return {"ok": True, "sale": sale}


@app.get("/api/claimed-sales")
async def get_claimed_sales():
    """Get all claimed sales (for admin/export)."""
    sales_file = os.path.join(os.path.dirname(__file__), "claimed_sales.json")
    if not os.path.exists(sales_file):
        return {"sales": []}
    try:
        with open(sales_file, "r") as f:
            return {"sales": json.loads(f.read())}
    except Exception:
        return {"sales": []}


@app.get("/api/transcripts/download")
async def download_transcripts():
    """Download all transcripts as a single JSON array, then delete them."""
    from transcript_store import TRANSCRIPTS_DIR
    import json as _json
    files = sorted(TRANSCRIPTS_DIR.glob("transcript_*.json"))
    all_transcripts = []
    for f in files:
        try:
            all_transcripts.append(_json.loads(f.read_text()))
        except Exception:
            continue

    # Delete after reading so next pull only gets new ones
    for f in files:
        try:
            f.unlink()
        except Exception:
            pass
    # Reset counter
    counter_file = TRANSCRIPTS_DIR / "_counter.json"
    if counter_file.exists():
        try:
            counter_file.unlink()
        except Exception:
            pass

    count = len(all_transcripts)
    print(f"[transcripts] downloaded and cleared {count} transcripts")

    from starlette.responses import Response
    return Response(
        content=_json.dumps(all_transcripts, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=all_transcripts.json"},
    )


@app.get("/api/insights")
async def get_insights():
    """Return all tuning analyses and recent transcript stats."""
    from transcript_store import TUNING_FILE, TRANSCRIPTS_DIR
    import json as _json
    analyses = []
    if TUNING_FILE.exists():
        try:
            data = _json.loads(TUNING_FILE.read_text())
            analyses = data if isinstance(data, list) else [data]
        except Exception:
            pass
    # Count transcripts and gather override/feedback stats
    transcript_files = sorted(TRANSCRIPTS_DIR.glob("transcript_*.json"), reverse=True)
    transcript_stats = []
    for f in transcript_files[:20]:
        try:
            t = _json.loads(f.read_text())
            transcript_stats.append({
                "timestamp": t.get("timestamp", ""),
                "mode": t.get("mode", ""),
                "stage_reached": t.get("stage_reached", ""),
                "turn_count": t.get("turn_count", 0),
                "rep_overrides": t.get("rep_overrides", []),
                "user_feedback": t.get("user_feedback", ""),
            })
        except Exception:
            continue
    return {
        "analyses": analyses,
        "transcripts": transcript_stats,
        "server_started_at": _SERVER_STARTED_AT,
    }


# ── Utilities (stateless) ─────────────────────────────────────────────────

def _preprocess_tts(text: str) -> str:
    import re
    text = text.replace("—", ", ").replace("–", ", ")
    text = text.replace("...", ",").replace("…", ",")
    text = re.sub(r'\ba\b', 'a,', text)
    text = re.sub(r',\s*,', ',', text)
    text = re.sub(r' {2,}', ' ', text)
    return text.strip()


async def _tts(text: str, voice: str = "aura-asteria-en") -> str | None:
    processed = _preprocess_tts(text)
    def _call():
        r = requests.post(
            f"https://api.deepgram.com/v1/speak?model={voice}",
            headers={"Authorization": f"Token {DEEPGRAM_API_KEY}", "Content-Type": "application/json"},
            json={"text": processed},
            timeout=10,
        )
        r.raise_for_status()
        return base64.b64encode(r.content).decode()
    try:
        return await asyncio.to_thread(_call)
    except Exception as e:
        print(f"[tts] error: {e}")
        return None


import re as _re

# Aggressive pattern to strip ANY leading acknowledgement/warmth from next_step
# when an opener was already shown. This runs unconditionally when opener exists.
_LEADING_FLUFF_PATTERN = _re.compile(
    r"^("
    # Single-word affirmatives
    r"perfect|awesome|great|amazing|wonderful|excellent|fantastic|absolutely|"
    r"beautiful|brilliant|nice|cool|sure|definitely|alright|right|"
    # Short phrases
    r"love it|love that|got it|got that|sounds good|sounds great|sure thing|"
    r"no worries|no problem|no worries at all|of course|for sure|you bet|"
    # "That's X" patterns
    r"that's great|that's awesome|that's perfect|that's wonderful|that's helpful|"
    r"that's good|that's fair|that's smart|that's exciting|that's so exciting|"
    r"that's a great question|that's a good question|that's a really good question|"
    r"that's totally fine|that's really good|that's really helpful|"
    # "I X" patterns
    r"i totally hear you|i hear you|i hear you on that|i hear that|"
    r"i understand|i totally understand|i completely understand|"
    r"i appreciate that|i appreciate you sharing that|"
    r"i love that|i love it|i love to hear that|"
    # "Good/great to X" patterns
    r"good to know|good to hear|great to know|great to hear|"
    r"glad to hear that|good stuff|"
    # Congratulatory
    r"congrats|congratulations|congrats on the new place|"
    # Empathy phrases
    r"totally understandable|that makes sense|that makes complete sense|"
    r"i totally understand that feeling|"
    # "Ok" variants
    r"ok(?:ay)?|ok great|okay great"
    r")"
    # Eat trailing punctuation and whitespace
    r"[!.,;:\-—–\s]*",
    _re.IGNORECASE,
)

# Secondary pattern: strip full leading sentences that are pure acknowledgement
# e.g. "I totally hear you on that, and honestly that's a really smart way to think about it."
_LEADING_SENTENCE_FLUFF = _re.compile(
    r"^[^.!?]*?\b("
    r"hear you|understand|appreciate|makes sense|good to know|glad to hear|"
    r"totally get that|completely get that|smart move|great idea|love that|"
    r"no worries|no problem|i can help|i'll help|i'll take care|let me help"
    r")\b[^.!?]*?[.!?]\s*",
    _re.IGNORECASE,
)


def _strip_fluff_for_opener(opener: str, next_step: str) -> str:
    """Strip leading affirmative/acknowledgement fluff from next_step when an
    opener was already shown. The rep reads opener then next_step as one
    paragraph — so next_step must jump straight to substance."""
    if not opener or not next_step:
        return next_step

    # Pass 1: strip known affirmative starters (may need multiple passes)
    cleaned = next_step
    for _ in range(3):
        attempt = _LEADING_FLUFF_PATTERN.sub("", cleaned, count=1).strip()
        if attempt == cleaned or not attempt:
            break
        cleaned = attempt

    # Pass 2: if it still starts with a warm-up sentence, strip it
    attempt = _LEADING_SENTENCE_FLUFF.sub("", cleaned, count=1).strip()
    if attempt:
        cleaned = attempt

    # Pass 3: detect semantic overlap — if next_step's first sentence says
    # essentially the same thing as the opener, strip it
    opener_lower = opener.lower()
    first_sentence_end = None
    for i, ch in enumerate(cleaned):
        if ch in ".!?" and i > 10:
            first_sentence_end = i + 1
            break
    if first_sentence_end and len(cleaned) > first_sentence_end + 10:
        first_sentence = cleaned[:first_sentence_end].lower()
        # Check for significant word overlap (3+ shared content words)
        _SKIP_WORDS = {"i", "a", "the", "to", "and", "or", "is", "it", "that", "so",
                        "for", "of", "in", "on", "you", "your", "we", "me", "my",
                        "do", "can", "will", "just", "let", "be", "are", "was", "have"}
        opener_words = {w for w in _re.findall(r'\w+', opener_lower) if w not in _SKIP_WORDS and len(w) > 2}
        first_words = {w for w in _re.findall(r'\w+', first_sentence) if w not in _SKIP_WORDS and len(w) > 2}
        overlap = opener_words & first_words
        if len(overlap) >= 3:
            cleaned = cleaned[first_sentence_end:].strip()

    # Fix capitalization
    if cleaned and cleaned[0].islower():
        cleaned = cleaned[0].upper() + cleaned[1:]

    return cleaned if cleaned else next_step


# Opener tracking — global set, only cleared on explicit new call (not reconnection).
# WebSocket reconnections create new Session objects, so per-session tracking
# gets wiped on every reconnect. Global set survives reconnections.
_active_opener_set: set[str] = set()

def _reset_opener_tracking():
    """Clear opener history. Only call when user starts a NEW call from setup screen."""
    _active_opener_set.clear()
    print(f"[opener] tracking reset — new call")

def _pick(options: list[str]) -> str | None:
    """Pick an unused opener. Returns None if all options already used."""
    available = [o for o in options if o not in _active_opener_set]
    if not available:
        return None
    choice = random.choice(available)
    _active_opener_set.add(choice)
    return choice

def _try_unique(opener: str) -> str | None:
    """Return the opener if it hasn't been used this session, else None."""
    if opener in _active_opener_set:
        return None
    _active_opener_set.add(opener)
    print(f"[opener] NEW: {opener[:60]}")
    return opener


def _quick_opener(text: str, current_stage: str, last_rep_text: str = "") -> str:
    """Generate a unique Say First opener. NEVER repeats within a session.
    Every return goes through _try_unique. No keyword insertion — it produces garbage."""
    t = text.lower().strip()

    # ── SHORT FILLER: skip Say First for non-discovery stages ──
    _short = t.rstrip(".,!?").strip()
    _SHORT_FILLERS = {
        "yes", "yeah", "yep", "sure", "absolutely", "of course", "correct",
        "right", "that's right", "yes sir", "yes ma'am", "mhmm", "uh huh",
        "sounds good", "that works", "that makes sense", "okay", "ok", "alright",
        "go ahead", "yea", "no", "nope", "no sir", "no ma'am", "not really",
        "no thank you", "no thanks", "nah", "i'm good", "no i don't",
        "not right now", "perfect", "awesome", "cool", "great", "nice",
        "for sure", "mm hmm", "yep yep", "yes please", "no problem",
        "i'm doing good", "i'm doing pretty good", "i'm doing great",
        "good how are you", "doing good", "doing well", "doing fine",
    }
    if _short in _SHORT_FILLERS and current_stage not in ("intro", "discovery"):
        return ""

    # ── Detect specific situations and match with a natural response ──

    # Emotional / life situations
    if any(w in t for w in ["break in", "broken into", "robbery", "robbed", "burglar", "stolen"]):
        return _try_first([
            "I'm really sorry to hear that — let's make sure that never happens again.",
            "That's terrible — we're gonna get you fully protected.",
            "I'm sorry you went through that — let's get you covered right away.",
        ])
    if any(w in t for w in ["scared", "nervous", "worried", "anxious", "afraid"]):
        return _try_first([
            "I completely understand that feeling — that's exactly why we're here.",
            "Your safety is our top priority — I'll make sure you're taken care of.",
            "That's exactly the right reason to get set up — let's give you that peace of mind.",
        ])
    if any(w in t for w in ["baby", "newborn", "toddler", "pregnant", "expecting"]):
        return _try_first([
            "Congratulations! A security system is perfect timing with a little one.",
            "That's so exciting — keeping the baby safe is exactly what this is for.",
        ])
    if any(w in t for w in [" kids", "children", "my son", "my daughter", "teenager"]):
        return _try_first([
            "Protecting the kids is the number one reason people call us.",
            "Family safety is huge — I'll make sure we set you up with the right features.",
            "Keeping the family safe is what it's all about.",
        ])
    if any(w in t for w in ["moved", "new house", "just bought", "new home", "new place", "moving", "just move"]):
        return _try_first([
            "Congrats on the new place — this is the perfect time to get set up.",
            "That's exciting — let's get your new home protected from day one.",
            "A lot of our customers set up right when they move in — smart move.",
        ])
    if any(w in t for w in ["live alone", "by myself", "on my own"]):
        return _try_first([
            "Peace of mind when you're on your own is so important — I've got you.",
            "That extra layer of protection makes a huge difference on your own.",
        ])
    if any(w in t for w in ["neighbor", "down the street", "next door"]):
        return _try_first([
            "When it's that close to home, you can't wait — let's get you covered.",
            "I don't blame you — that would make anyone want to take action.",
        ])
    if any(w in t for w in ["travel", "work nights", "gone a lot", "away from home", "deployed", "out of town"]):
        return _try_first([
            "Being able to check on things from anywhere — that's what this is built for.",
            "We'll make sure you have eyes on your home no matter where you are.",
        ])
    if any(w in t for w in ["never had", "first time", "don't have one", "no never", "never before",
                             "this is my first"]):
        return _try_first([
            "No worries — I'll walk you through everything step by step.",
            "That's totally fine — you're in good hands, I do this all day.",
            "Perfect — I'll make this super easy for you.",
        ])
    # Competitors
    _competitors = {"vivint": "Vivint", "adt": "ADT", "simplisafe": "SimpliSafe", "ring": "Ring",
                    "alder": "Alder", "brinks": "Brinks", "frontpoint": "Frontpoint"}
    for _ck, _cv in _competitors.items():
        if _ck in t:
            return _try_first([
                f"Good to know you had {_cv} — I'll show you what makes Cove different.",
                f"We get a lot of people coming from {_cv} — you're gonna love the switch.",
            ])
    if any(w in t for w in ["i had", "i was with", "i used to have", "we had"]):
        return _try_first([
            "That experience will definitely help — this should be a breeze.",
            "Good to know — we'll make sure we set you up even better this time.",
        ])
    if any(w in t for w in ["contract", "locked in", "stuck with", "cancel"]):
        return _try_first([
            "Great news — no contracts with Cove, it's completely month to month.",
            "You'll love this — you can cancel anytime, no commitment.",
        ])
    if any(w in t for w in ["talk to my", "ask my wife", "ask my husband", "spouse", "partner"]):
        return _try_first([
            "Totally understandable — I'd want to check with my partner too.",
            "Of course — let me give you all the info so that conversation is easy.",
        ])
    if any(w in t for w in ["think about it", "call back", "not sure", "not ready"]):
        return _try_first([
            "No pressure at all — I want you to feel good about it.",
            "That's fair — take your time, I'll make sure you have everything you need.",
        ])

    # Collect info
    if current_stage == "collect_info":
        if any(w in t for w in ["@", "gmail", "yahoo", "hotmail", ".com"]):
            return _try_first(["Got it, I have your email.", "Perfect, email is saved."]) or ""
        if any(c.isdigit() for c in t) and sum(c.isdigit() for c in t) >= 7:
            return _try_first(["Got it, I have your number.", "Perfect, phone number is saved."]) or ""
        if any(w in t for w in ["street", "drive", "avenue", "road", "lane"]):
            return _try_first(["Got it — let me verify coverage in your area.",
                                "Perfect — let me check that we can service that area."]) or ""

    # Build system
    if current_stage == "build_system":
        if any(w in t for w in ["don't need", "no thanks", "don't want", "skip"]):
            return _try_first(["No problem — I only want you to have what you actually need.",
                                "Totally fine — we'll skip that one."]) or ""

    # Closing
    if current_stage == "closing" and any(w in t for w in ["placed the order", "went through"]):
        return _try_first(["Congratulations and welcome to the Cove family!",
                            "That's awesome — welcome to Cove!"]) or ""

    # Customer question
    if "?" in text or any(w in t for w in ["what is", "how does", "can i", "can you"]):
        return _try_first(["Great question — let me explain.",
                            "Good question — here's how it works.",
                            "Of course — let me break that down."]) or ""

    # ── MASTER FALLBACK — protection-focused, empathetic acknowledgements ──
    # Every one reinforces: "I heard you, and I'm going to protect you."
    return _try_first([
        "I hear you — we're gonna make sure you and your family are fully protected.",
        "That's exactly why I'm here — let's get you completely covered.",
        "I totally understand — your safety is my top priority, I'll take great care of you.",
        "I appreciate that — it helps me make sure we build the right system for you.",
        "That's great to know — I'm going to make sure we get you fully set up.",
        "I hear you on that — let me make sure every part of your home is covered.",
        "Thank you for sharing that — it's going to help me get you the best protection.",
        "I understand completely — let's make sure you feel safe and secure.",
        "That's really helpful — I'll use that to make sure your system is perfect for you.",
        "I appreciate you telling me that — we'll get you taken care of.",
        "That makes total sense — I'm going to get you fully protected.",
        "I hear you — and that's exactly why getting this set up now is so smart.",
        "Thank you — knowing that helps me build the right system for your situation.",
        "I totally get it — let's make sure your home is completely secure.",
        "That's important — I'll make sure we address that when we build your system.",
        "I appreciate that — I'm going to take really good care of you.",
        "I understand — we'll make sure you have complete peace of mind.",
        "That helps a lot — I want to make sure this system is exactly right for you.",
        "I hear you — your safety matters and I'm going to make sure we get this right.",
        "Thank you for that — it really helps me understand how to best protect you.",
        "I appreciate you sharing that — let's get you set up with everything you need.",
        "That's exactly what I needed to know — I'll make sure you're fully covered.",
        "I understand where you're coming from — your protection is what this is all about.",
        "That's great context — it's going to make a big difference in your system.",
        "I hear you — I'm going to make sure you don't have to worry about a thing.",
        "Thank you — I'll keep that in mind as we put your system together.",
        "I appreciate that — let's make sure your home is safe from every angle.",
        "That makes sense — I'm here to make sure you get the best protection possible.",
        "I totally understand — and I'm going to make sure we take care of everything.",
        "That's helpful — let me make sure we get you the right coverage.",
    ]) or ""


def _try_first(options: list[str]) -> str:
    """Try each option in order, return the first unused one. Empty string if all used."""
    random.shuffle(options)
    for opt in options:
        result = _try_unique(opt)
        if result:
            return result
    return ""


# ── Fallback next steps when Claude returns empty ─────────────────────────

def _trim_long_suggestion(text: str, max_words: int = 120) -> str:
    """Trim overly long suggestions to keep them readable for the rep.
    Cuts at the nearest sentence boundary before max_words."""
    if not text:
        return text
    words = text.split()
    if len(words) <= max_words:
        return text
    # Find the last sentence-ending punctuation before max_words
    truncated = " ".join(words[:max_words])
    # Try to cut at last sentence boundary
    for end in [". ", "? ", "! "]:
        last_pos = truncated.rfind(end)
        if last_pos > len(truncated) // 2:  # don't cut too short
            return truncated[:last_pos + 1]
    # Fallback: cut at max_words and add "..."
    return truncated




def _stage_transition(stage: str) -> str:
    """Return a natural transition opener when moving to a new stage."""
    transitions = {
        "discovery": "Let me learn a little more about your situation.",
        "collect_info": "Alright, I'm just going to grab some info from you before we get started.",
        "build_system": "Great. It looks like we have fantastic coverage out there so we can definitely help you out.",
        "closing": "Ok great news. I think I'm going to be able to get you a lot of extra discounts. Let me see what I can do for you.",
    }
    return transitions.get(stage, "")


def _spoken_to_digits(text: str) -> str:
    """Convert spoken number words to digit string for phone numbers.
    'zero nine three eight' -> '0938', 'eight zero one' -> '801'"""
    _WORD_TO_DIGIT = {
        "zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3",
        "four": "4", "five": "5", "six": "6", "seven": "7",
        "eight": "8", "nine": "9",
    }
    result = []
    for word in text.lower().split():
        if word in _WORD_TO_DIGIT:
            result.append(_WORD_TO_DIGIT[word])
        elif word.isdigit():
            result.append(word)
    return "".join(result)


def _spoken_numbers_to_numerals(text: str) -> str:
    """Convert spoken numbers in addresses to numerals.
    'six five zero east eight hundred south' -> '650 East 800 South'
    Handles single digits, teens, tens, hundreds."""
    _ONES = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4,
             "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9}
    _TEENS = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
              "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
              "eighteen": 18, "nineteen": 19}
    _TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
             "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
    _ALL_NUM_WORDS = set(_ONES) | set(_TEENS) | set(_TENS) | {"hundred", "thousand", "oh"}

    words = text.lower().split()
    result = []
    i = 0
    while i < len(words):
        w = words[i]
        if w not in _ALL_NUM_WORDS:
            result.append(words[i])  # preserve original case from text
            i += 1
            continue

        # Accumulate number — concatenate single digits, combine tens+ones
        digits_str = ""
        while i < len(words):
            w = words[i]
            if w in _ONES:
                if i + 1 < len(words) and words[i + 1] == "hundred":
                    digits_str += str(_ONES[w] * 100)
                    i += 2
                else:
                    digits_str += str(_ONES[w])
                    i += 1
            elif w == "oh":
                digits_str += "0"
                i += 1
            elif w in _TEENS:
                digits_str += str(_TEENS[w])
                i += 1
            elif w in _TENS:
                val = _TENS[w]
                # Check if next word is a ones digit (e.g., "twenty three" = 23)
                if i + 1 < len(words) and words[i + 1] in _ONES:
                    val += _ONES[words[i + 1]]
                    i += 2
                else:
                    i += 1
                digits_str += str(val)
            elif w == "hundred" and digits_str:
                # standalone "hundred" after digits
                digits_str += "00"
                i += 1
            elif w == "thousand" and digits_str:
                digits_str += "000"
                i += 1
            else:
                break
        result.append(digits_str)

    # Title-case non-number words
    final = []
    for r in result:
        if r.isdigit():
            final.append(r)
        else:
            final.append(r.capitalize() if r.lower() not in ("n", "s", "e", "w") else r.upper())
    return " ".join(final)


def _extract_email(text: str) -> str:
    """Extract email from spoken text like 'joe at gmail dot com'."""
    t = text.lower().strip()
    # Remove filler prefixes — be aggressive since email has no spaces anyway
    for filler in ["my email is ", "email is ", "the email is ", "it's ", "it is ",
                    "yeah it's ", "yes it's ", "yep it's ", "yep its ", "yep ",
                    "yeah ", "yes ", "sure it's ", "sure its ", "so it's ", "so its ",
                    "that's ", "yes that's ", "yeah that's ", "that is ", "yes that is ",
                    "alright it's ", "okay it's ", "ok it's ",
                    "yeah that would be ", "that would be ", "so that's "]:
        if t.startswith(filler):
            t = t[len(filler):]
    # Convert spoken digits to numerals in email (e.g. "eight two" → "82")
    _EMAIL_DIGITS = {"zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
                     "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9"}
    _email_words = t.split()
    _rebuilt = []
    for w in _email_words:
        if w in _EMAIL_DIGITS:
            _rebuilt.append(_EMAIL_DIGITS[w])
        else:
            _rebuilt.append(w)
    t = " ".join(_rebuilt)
    # Fix common speech-to-text splits and stutters
    t = t.replace("g mail", "gmail").replace("hot mail", "hotmail")
    t = t.replace("out look", "outlook").replace("ya hoo", "yahoo")
    # Fix stutters: "g gmail" -> "gmail", "y yahoo" -> "yahoo"
    t = t.replace("g gmail", "gmail").replace("y yahoo", "yahoo")
    t = t.replace("h hotmail", "hotmail").replace("o outlook", "outlook")
    # Replace spoken email patterns
    t = t.replace(" at ", "@").replace(" dot ", ".")
    # If no "@" but has a domain (gmail, yahoo, aol, etc.), try to insert @
    # This handles STT dropping "at" — e.g. "kyle alder dot com" → "kyle@alder.com"
    if "@" not in t:
        for domain in ["gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com"]:
            if domain in t:
                idx = t.index(domain)
                # Find the space before the domain to insert @
                before = t[:idx].rstrip()
                if before:
                    # Find the last space — everything after it is the domain prefix
                    last_space = before.rfind(" ")
                    if last_space >= 0:
                        t = before[:last_space + 1] + before[last_space + 1:] + "@" + domain
                    else:
                        t = before + "@" + domain
                break
        else:
            # No known domain — try inserting @ before last word before .com/.org etc
            import re as _re_email2
            m = _re_email2.search(r'(\S+)\s+(\S+\.com|\.org|\.net|\.edu)', t)
            if m:
                t = t[:m.start()] + m.group(1) + "@" + m.group(2)
    # Remove remaining spaces (email has no spaces)
    result = t.replace(" ", "")
    # Final dedup: "ggmail" -> "gmail", "yyahoo" -> "yahoo"
    for domain in ["gmail", "yahoo", "hotmail", "outlook", "aol"]:
        while domain[0] + domain in result:
            result = result.replace(domain[0] + domain, domain)
    if "@" in result:
        # Safety net: strip any leading non-email words that got mashed in
        # Valid email chars before @ are: alphanumeric, dots, underscores, hyphens, plus
        import re as _re_email
        match = _re_email.search(r'[a-zA-Z0-9][a-zA-Z0-9._+\-]*@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', result)
        if match:
            return match.group(0)
        return result
    return text.strip()


def _extract_name(text: str) -> str:
    """Extract just the name from customer speech like 'my name is joe bonnie'
    or 'yeah my first name is joe and my last name is bonnie'
    or spelled out: 'k y l e then my last name is b o n n i e'."""
    import re as _re_name
    t = text.lower().strip()

    # ── Handle letter-by-letter spelling ──
    # Detect patterns like "k y l e" (single letters separated by spaces)
    # or "it is j e o f f" — common when customer spells their name
    _letters = _re_name.findall(r'\b[a-zA-Z]\b', text)
    if len(_letters) >= 3:
        # Transition/filler words to skip between spelled letter clusters
        _SPELL_FILLER = {"then", "and", "my", "last", "name", "is", "that's", "it",
                         "first", "it's", "that", "yeah", "yes", "the", "of", "course",
                         "yep", "okay", "ok", "sure", "so", "well", "um", "uh", "a"}
        words = text.split()
        spelled_parts = []
        current_spell = []
        for w in words:
            clean = w.strip(".,!?").lower()
            if len(clean) == 1 and clean.isalpha():
                current_spell.append(clean)
            else:
                if len(current_spell) >= 2:
                    spelled_parts.append("".join(current_spell).capitalize())
                current_spell = []
                # Skip filler/transition words
                if clean in _SPELL_FILLER:
                    continue
        if len(current_spell) >= 2:
            spelled_parts.append("".join(current_spell).capitalize())
        if spelled_parts:
            return " ".join(spelled_parts[:3])  # max 3 name parts

    # ── Handle "my name is X" / "name is X" patterns ──
    for prefix in ["my name is ", "my first name is ", "first name is ", "name is ",
                    "this is ", "it is ", "that is ", "that's "]:
        if prefix in t:
            after = text[t.index(prefix) + len(prefix):].strip()
            # Remove filler words between first and last name
            for filler in [" and my last name is ", " my last name is ", " last name is ",
                           " and my last name ", " my last name ", " last name "]:
                after = after.replace(filler, " ")
            # Take the name words (stop at non-name words)
            _STOP_WORDS = {"and", "my", "the", "so", "but", "yeah", "yes", "that", "is",
                           "it", "i", "we", "at", "on", "in", "from", "with", "then",
                           "yep", "okay", "ok"}
            _NOT_NAMES = {"actually", "already", "currently", "still", "honestly",
                          "basically", "literally", "probably", "definitely", "maybe",
                          "trying", "thinking", "wondering", "hoping", "getting",
                          "having", "going", "coming", "working", "living",
                          "fine", "good", "great", "well", "ok", "okay", "alright",
                          "doing", "interested", "looking", "calling", "here", "ready",
                          "not", "just", "also", "really", "very", "glad", "happy",
                          "sure", "sorry", "curious", "new",
                          "my", "your", "his", "her", "our", "their", "its",
                          "being", "feeling", "sitting", "standing", "running",
                          "home", "here", "there", "now", "then", "about", "over",
                          "in", "on", "up", "out", "at", "from", "with", "for",
                          "so", "if", "or", "an", "no", "oh", "uh", "um"}
            words = []
            for w in after.split():
                clean = w.strip(".,!?")
                if clean.lower() in _STOP_WORDS and len(words) >= 2:
                    break
                if clean.lower() in _NOT_NAMES:
                    break  # not a real name — abort
                if clean.isalpha() and len(clean) >= 2:
                    words.append(clean.capitalize())
                if len(words) >= 3:  # max 3 name parts
                    break
            if words:
                return " ".join(words)

    # ── Fallback: if short text (≤4 words), might just be the name ──
    parts = text.strip().split()
    if len(parts) <= 4:
        _FILLER = {"my", "is", "the", "yeah", "yes", "it", "its", "be",
                   "that", "then", "yep", "okay", "ok", "and", "gonna",
                   "would", "will", "just", "well", "so", "a", "an", "of",
                   "to", "for", "i", "i'm", "i'd", "let", "me", "hi"}
        words = [w.capitalize() for w in parts if w.isalpha() and len(w) >= 2
                 and w.lower() not in _FILLER]
        if words:
            return " ".join(words)

    return text.strip()


def _customer_mentioned_kids(coach) -> bool:
    """Check if customer mentioned kids but hasn't specified ages yet.
    Returns False if customer explicitly says they DON'T have kids."""
    if not coach or not coach._customer_facts:
        return False
    all_text = " ".join(coach._customer_facts).lower()
    # Check for negative mentions first — "don't have kids", "no kids", etc.
    no_kids = any(w in all_text for w in [
        "don't have kids", "don't have children", "no kids", "no children",
        "don't have any kids", "we don't have", "i don't have",
    ])
    if no_kids:
        return False
    has_kids = any(w in all_text for w in ["kids", "children", "my son", "my daughter", "kid", "child"])
    # Already specified age?
    has_age = any(w in all_text for w in [
        "little kids", "little ones", "toddler", "teenager", "teenagers",
        "three and", "four and", "five and", "six and", "seven and", "eight and",
        "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen",
        "year old", "years old", "elementary", "middle school", "high school",
        "baby", "newborn", "infant",
    ])
    return has_kids and not has_age


def _get_discovery_context(coach) -> dict:
    """Extract personalization context from discovery phase customer facts."""
    if not coach or not coach._customer_facts:
        return {}
    all_text = " ".join(coach._customer_facts).lower()
    ctx = {}
    if any(w in all_text for w in ["little kids", "little ones", "toddler", "three and five",
                                     "young kids", "small kids", "six and seven", "four and six"]):
        ctx["kids"] = "little"
    elif any(w in all_text for w in ["teenager", "thirteen", "fifteen", "seventeen", "sixteen", "fourteen"]):
        ctx["kids"] = "teens"
    elif any(w in all_text for w in ["kids", "children", "my son", "my daughter", "kid"]):
        ctx["kids"] = "kids"
    if any(w in all_text for w in ["baby", "newborn", "infant", "new baby"]):
        ctx["baby"] = True
    if any(w in all_text for w in ["wife", "husband", "spouse", "girlfriend", "boyfriend", "partner"]):
        ctx["spouse"] = True
    if any(w in all_text for w in ["dog", "cat", "pet", "dogs", "cats", "pets"]):
        ctx["pets"] = True
    if any(w in all_text for w in ["break in", "broken into", "robbery", "burglar", "stolen", "theft"]):
        ctx["break_in"] = True
    if any(w in all_text for w in ["travel", "work nights", "night shift", "gone a lot", "away from home",
                                     "out of town", "not home", "at work"]):
        ctx["away_often"] = True
    if any(w in all_text for w in ["just moved", "new house", "new home", "new place", "just bought"]):
        ctx["just_moved"] = True
    if any(w in all_text for w in ["live alone", "by myself", "on my own", "just me"]):
        ctx["lives_alone"] = True
    return ctx


def _personalize_chime(ctx: dict, name: str) -> str:
    """Generate a personalized chime feature description."""
    suffix = f", {name}" if name else ""
    if ctx.get("kids") == "little":
        return f"With you having little ones, the chime feature is huge — even when the system's unarmed, it'll say 'front door open' every time a door opens. So if one of your kids were to get outside without you knowing, you'd be alerted right away. Crisis averted. Does that make sense{suffix}?"
    if ctx.get("kids") == "teens":
        return f"With teenagers in the house, the chime feature is really helpful — even when the system's unarmed, it'll say 'front door open' or 'back door open' anytime someone comes or goes. So if one of your teenagers tries to sneak out, not that they would, you'll know right away. Does that make sense{suffix}?"
    if ctx.get("baby"):
        return f"With a baby at home, the chime feature gives you that extra peace of mind — even when the system's unarmed, it'll say 'front door open' anytime someone comes or goes. You'll always know who's entering or leaving. Does that make sense{suffix}?"
    if ctx.get("spouse") and ctx.get("away_often"):
        return f"The chime feature is great for when you're away — even when the system's unarmed, it'll say 'front door open' or 'back door open' anytime someone comes or goes. That way you always know what's happening at home. Does that make sense{suffix}?"
    return f"Now with all those door and window sensors, when the system is armed they'll trigger the alarm. But even when the system is unarmed, they activate the chime feature — so it'll say 'front door open' or 'back door open' anytime someone comes or goes. Does that make sense{suffix}?"


def _personalize_camera(ctx: dict, name: str) -> str:
    """Generate a personalized indoor camera description."""
    suffix = f", {name}" if name else ""
    base = "I'm also going to give you a free indoor camera — it's live HD with recording, night vision, two-way audio, and a built-in motion sensor."
    if ctx.get("kids"):
        return f"{base} With your kids at home, you'll always have eyes and ears on the house — so whether you're at work or running errands, you can check in anytime right from your phone. Does that make sense{suffix}?"
    if ctx.get("baby"):
        return f"{base} With a new baby at home, this is perfect — you can check in on them from anywhere, hear what's going on, and even talk through the camera. Does that make sense{suffix}?"
    if ctx.get("away_often"):
        return f"{base} Since you travel a lot, this is huge — you'll always be able to pull up your camera right on your phone and see what's going on at home no matter where you are. Does that make sense{suffix}?"
    if ctx.get("lives_alone"):
        return f"{base} Living on your own, having that extra set of eyes gives you real peace of mind — you can check in on your place from anywhere. Does that make sense{suffix}?"
    return f"{base} So wherever you are, you'll always have eyes and ears on your home. Does that make sense{suffix}?"


def _inject_personalization(text: str, coach) -> str:
    """Post-process Claude's output to add personalization if it's generic.
    Checks if the text mentions chime/camera/panel without personalizing."""
    ctx = _get_discovery_context(coach)
    if not ctx:
        return text
    t_lower = text.lower()
    name = coach.customer_name or ""

    # If text mentions chime/door sensor but doesn't reference kids/family/baby
    if ("chime" in t_lower or "front door open" in t_lower) and ctx.get("kids"):
        # Check if already personalized
        if not any(w in t_lower for w in ["little ones", "teenagers", "kids", "children", "baby", "your son", "your daughter"]):
            if ctx["kids"] == "little":
                text += " This way if your kids get outside without you knowing about it, you'll be alerted right away. Crisis averted."
            elif ctx["kids"] == "teens":
                text += " So if one of your teenagers tries to sneak out, not that they would, you'll know right away."

    # If text mentions camera but doesn't reference family context
    if ("indoor camera" in t_lower or "eyes and ears" in t_lower) and not any(w in t_lower for w in ["kids", "children", "baby", "little", "family", "travel", "work"]):
        if ctx.get("kids"):
            text += " With your kids at home, you'll always be able to check in on them from anywhere."
        elif ctx.get("baby"):
            text += " With a new baby, this is perfect — you can check in from anywhere."
        elif ctx.get("away_often"):
            text += " Since you're away from home a lot, you'll always know what's going on."

    return text


def _personalize_panel(ctx: dict, name: str) -> str:
    """Generate a personalized panel/hub description."""
    suffix = f", {name}" if name else ""
    base = "I'm also going to get you the hub — that's the brain of the system that connects everything. It runs on cellular, so even if your power or Wi-Fi goes down, your home is still protected 24/7 with police, medical, and fire support. And you'll get a 7-inch color touchscreen panel to navigate everything."
    if ctx.get("kids"):
        return f"{base} With kids in the house, knowing you've got 24/7 backup even during a power outage is huge. Does that make sense{suffix}?"
    if ctx.get("break_in"):
        return f"{base} Given what happened in your neighborhood, having that cellular backup means your home is always protected — even if someone cuts your power or internet. Does that make sense{suffix}?"
    return f"{base} Does that make sense{suffix}?"


# Master prompt table — maps checklist keys to their script lines.
# This is the single source of truth for what the Then box should show.
_CHECKLIST_PROMPTS = {
    # Discovery
    "existing_customer": "Are you already a Cove customer, or are you looking to get a security system?",
    "had_system_before": "Have you ever had a security system before?",
    "why_security": "What has you looking into security? Did something happen, did you just move, what's going on?",
    "who_protecting": "Who are we looking to protect — is it just you or is there anyone else living there with you?",
    "kids_age": "Are we talking about little kids or teenagers?",
    "on_website": "Are you currently on the Cove website? If not — no problem, go ahead and pull up covesmart.com so I can walk you through the process.",
    # Collect info
    "full_name": "Could you please spell your first and last name for me?",
    "phone_number": "And what's your best phone number?",
    "email": "And your email so I can send all this information over to you?",
    "address": "What's the address you're looking to get the security set up at?",
    # Build system
    "door_sensors": "How many doors go in and out of your home?",
    "window_sensors": "How many windows are on the ground floor of your house that are accessible?",
    "extra_equip": "We also have a motion detector, glass break detector, and carbon monoxide detector. Do you think you'd need any of those?",
    "indoor_camera": None,  # uses personalization — filled dynamically
    "outdoor_camera": "We also have a doorbell camera and a solar-powered outdoor camera. The outdoor camera is 50% off right now. Would you like to add either of those?",
    "panel_hub": None,  # uses personalization — filled dynamically
    "yard_sign": "I'm also going to throw in a free yard sign and window stickers — that way everyone knows you have security in place. Plus you'll have full smartphone access so you can arm and disarm the system, view cameras, and control everything from your phone no matter where you are.",
    # Recap
    "recap_done": None,  # dynamic — generated from equipment list at runtime
    # Closing
    "closing_pitch": ("So how it will work, we have a 60-day risk-free trial, so you can try everything out and if it's not the right fit, you can return it for a full refund. "
                      "Here at Cove we have no contracts — it's completely month to month, and we have some of the best customer service in the industry. "
                      "We don't charge anything for installation because everything is wireless — we'll send all the equipment straight to you and you can set it up yourself in about 20 minutes."),
    "closing_pricing": None,  # dynamic — calculated from equipment counts at runtime
    "closing_cart": "Perfect. Have you already put all the equipment in your cart, or do you need me to read it all back to you?",
    "closing_checkout": "Go ahead and put your payment info in on the website. Let me know once you've placed the order and I'll confirm everything on my side.",
    "closing_welcome": ("If you need a technician, we have a third-party service starting at $129. "
                        "And if you have home insurance, request an alarm certificate from us for a discount. "
                        "Congratulations and welcome to the Cove family! "
                        "You'll get tracking info as soon as your package ships — usually 3 to 7 business days. "
                        "Is there anything else I can help you with before I let you go?"),
}

# Ordered checklist keys per stage — defines the sequence items should be covered
_STAGE_ITEM_ORDER = {
    "discovery": ["existing_customer", "had_system_before", "why_security", "who_protecting", "kids_age", "on_website"],
    "collect_info": ["full_name", "phone_number", "email", "address"],
    "build_system": ["door_sensors", "window_sensors", "extra_equip", "indoor_camera", "outdoor_camera", "panel_hub", "yard_sign", "recap_done"],
    "closing": ["closing_pitch", "closing_pricing", "closing_cart", "closing_checkout", "closing_welcome"],
}


def _build_context_from_transcript(checked_topic: str, coach) -> str:
    """Scan recent transcript for context related to a just-checked build_system item.
    Returns a natural acknowledgement prefix like '2 door sensors — got it.' or empty string."""
    _SPOKEN_NUMBERS = {
        "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
        "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
        "eleven": "11", "twelve": "12",
    }
    # What to look for in customer speech per topic
    _TOPIC_CONTEXT = {
        "door_sensors": {"label": "door sensors", "look_for_number": True},
        "window_sensors": {"label": "window sensors", "look_for_number": True},
        "extra_equip": {"label": None, "look_for_number": False},
        "indoor_camera": {"label": None, "look_for_number": False},
        "outdoor_camera": {"label": None, "look_for_number": False},
    }
    config = _TOPIC_CONTEXT.get(checked_topic)
    if not config or not config.get("look_for_number"):
        return ""
    # Scan last 6 turns of customer speech for a number
    for turn in reversed(coach._history[-12:]):
        if turn["speaker"] != "customer":
            continue
        t = turn["text"].lower()
        # Try spoken numbers first
        for word, digit in _SPOKEN_NUMBERS.items():
            if word in t.split():
                return f"{digit.capitalize()} {config['label']} — got it."
        # Try actual digits
        import re as _re2
        nums = _re2.findall(r'\d+', t)
        if nums:
            return f"{nums[0]} {config['label']} — got it."
    return ""


# ── Equipment pricing table ───────────────────────────────────────────
# Prices reflect the website's automatic pre-sale discounts:
#   - Sensors & accessories: 70% off retail (retail prices noted in comments)
#   - Indoor camera: free
#   - Hub + panel: $45 each ($90 total, down from $250 retail)
#   - Outdoor/doorbell cameras: 50% off retail
_EQUIPMENT_PRICES = {
    "door_sensors": 4.50,       # retail $15, pre-sale price
    "window_sensors": 4.50,     # retail $15, pre-sale price
    "motion_sensor": 15.00,     # retail $50, 70% off
    "glass_break": 15.00,       # retail $50, 70% off
    "co_detector": 37.50,       # retail $125, 70% off
    "smoke_detector": 28.50,    # retail $95, 70% off
    "indoor_camera": 0.00,      # free with system
    "outdoor_camera": 79.99,    # retail $159.99, 50% off
    "doorbell_camera": 49.99,   # retail $99.99, 50% off
    "panel_hub": 90.00,         # hub $45 + panel $45 (retail $250)
    "yard_sign": 0.00,          # free
    "key_fob": 9.00,            # retail $30, 70% off
    "panic_button": 9.00,       # retail $30, 70% off
    "flood_sensor": 18.00,      # retail $60, 70% off
    "medical_pendant": 9.00,    # retail $30, 70% off
    "secondary_siren": 45.00,   # retail $150, 70% off
}
_MONITORING_PRICES = {
    "plus": {"promo": 29.99, "standard": 32.99},  # LOWRATE takes to $26.99
    "basic": {"promo": 17.99, "standard": 22.99},  # LOWRATE takes to $14.99
}
_PROMO_MONTHS = 6  # first N months at promo rate

# ── Coupon codes ─────────────────────────────────────────────────────────
# Each coupon has a type and value. Types:
#   "free_hub_panel" — removes hub+panel cost (min order required)
#   "flat_off"       — flat dollar amount off equipment total
#   "monitoring_off" — dollar amount off monthly monitoring for N months
_COUPON_CODES = {
    "SYSTEM4FREE":    {"type": "free_hub_panel", "min_order": 99, "label": "Free Hub & Panel"},
    "LABORDAY":       {"type": "free_hub_panel", "min_order": 99, "label": "Free Hub & Panel"},
    "PRIMEFLASHDEAL": {"type": "free_hub_panel", "min_order": 99, "label": "Free Hub & Panel"},
    "BIGSPRINGDEAL":  {"type": "free_hub_panel", "min_order": 99, "label": "Big Spring Deal — Free Hub & Panel"},
    "LDEXTRA50":      {"type": "flat_off", "amount": 50, "label": "$50 off order"},
    "COVE15":         {"type": "flat_off", "amount": 15, "label": "$15 off order"},
    "LOWRATE":        {"type": "monitoring_off", "amount": 3, "months": 6, "label": "$3/mo off monitoring (6 months)"},
}


def _calculate_pricing(session) -> dict:
    """Calculate full pricing breakdown including coupon discounts."""
    # Base equipment total
    subtotal = 0.0
    for key, qty in session._equipment_counts.items():
        if qty > 0 and key in _EQUIPMENT_PRICES:
            subtotal += _EQUIPMENT_PRICES[key] * qty
    # Panel + hub always included unless explicitly 0
    if "panel_hub" not in session._equipment_counts or session._equipment_counts.get("panel_hub", 0) > 0:
        if "panel_hub" not in session._equipment_counts:
            subtotal += _EQUIPMENT_PRICES["panel_hub"]

    equipment_total = subtotal
    discount_total = 0.0
    discount_label = ""
    plan = session._plan if hasattr(session, '_plan') else "plus"
    monthly_promo = _MONITORING_PRICES[plan]["promo"]
    monthly_standard = _MONITORING_PRICES[plan]["standard"]
    monitoring_discount = 0.0

    # Apply coupons
    for code in session._applied_coupons:
        coupon = _COUPON_CODES.get(code)
        if not coupon:
            continue
        if coupon["type"] == "free_hub_panel":
            if subtotal >= coupon["min_order"]:
                hub_panel_cost = _EQUIPMENT_PRICES["panel_hub"]
                discount_total += hub_panel_cost
                discount_label = coupon["label"]
        elif coupon["type"] == "flat_off":
            discount_total += coupon["amount"]
            discount_label = coupon["label"]
        elif coupon["type"] == "monitoring_off":
            monitoring_discount = coupon["amount"]

    # Minimum equipment total is $99
    equipment_total = max(99.00, subtotal - discount_total)
    adjusted_monthly = monthly_promo - monitoring_discount

    return {
        "subtotal": subtotal,
        "discount_total": discount_total,
        "discount_label": discount_label,
        "equipment_total": equipment_total,
        "monthly_promo": adjusted_monthly,
        "monthly_standard": monthly_standard,
        "monitoring_discount": monitoring_discount,
        "applied_coupons": list(session._applied_coupons),
    }


def _build_pricing_prompt(session) -> str:
    """Build a dynamic closing pricing prompt with real equipment totals."""
    pricing = _calculate_pricing(session)
    name = session.coach.customer_name if session.coach else ""
    suffix = f", {name}" if name else ""

    parts = []
    if pricing['monthly_promo'] < pricing['monthly_standard']:
        parts.append(
            f"On the monthly monitoring, for the first {_PROMO_MONTHS} months it'll just be "
            f"${pricing['monthly_promo']:.2f} per month. "
            f"After that, it goes to the standard rate of ${pricing['monthly_standard']:.2f}. "
        )
    else:
        parts.append(
            f"On the monthly monitoring, it's just ${pricing['monthly_standard']:.2f} per month. "
        )

    if pricing["discount_total"] > 0:
        parts.append(
            f"And the equipment — with all the discounts and promotions today, "
            f"your one-time equipment cost comes out to just ${pricing['equipment_total']:.2f} "
            f"— that's ${pricing['discount_total']:.0f} off{suffix}. "
        )
    else:
        parts.append(
            f"And the equipment — with all the discounts and promotions today, "
            f"your one-time equipment cost is going to come out to ${pricing['equipment_total']:.2f}{suffix}. "
        )

    parts.append("So does that sound like something that will work for you?")
    return "".join(parts)


def _build_recap_prompt(session) -> str:
    """Build a dynamic recap prompt from the equipment list."""
    counts = session._equipment_counts
    name = session.coach.customer_name if session.coach else ""
    parts = []
    if counts.get("door_sensors", 0) > 0:
        parts.append(f"{counts['door_sensors']} door sensor{'s' if counts['door_sensors'] != 1 else ''}")
    if counts.get("window_sensors", 0) > 0:
        parts.append(f"{counts['window_sensors']} window sensor{'s' if counts['window_sensors'] != 1 else ''}")
    if counts.get("motion_sensor", 0) > 0:
        parts.append("a motion detector")
    if counts.get("glass_break", 0) > 0:
        parts.append("a glass break detector")
    if counts.get("co_detector", 0) > 0:
        parts.append("a carbon monoxide detector")
    if counts.get("indoor_camera", 0) > 0:
        parts.append("a free indoor camera")
    if counts.get("outdoor_camera", 0) > 0:
        parts.append("an outdoor camera")
    if counts.get("doorbell_camera", 0) > 0:
        parts.append("a doorbell camera")
    if counts.get("smoke_detector", 0) > 0:
        parts.append("a smoke detector")
    if counts.get("key_fob", 0) > 0:
        n = counts["key_fob"]
        parts.append(f"{n} key fob{'s' if n != 1 else ''}")
    if counts.get("medical_pendant", 0) > 0:
        parts.append("a medical pendant")
    if counts.get("flood_sensor", 0) > 0:
        parts.append("a flood sensor")
    # Always include these
    parts.append("the hub and touchscreen panel")
    parts.append("a yard sign and window stickers")
    parts.append("full smartphone access")

    if parts:
        equip_list = ", ".join(parts)
        suffix = f", {name}" if name else ""
        return (f"Let me quickly recap what I have for you: {equip_list}. "
                f"Personally I believe we've got you fully protected — "
                f"but is there anything else you were hoping I could add{suffix}?")
    return "Let me quickly recap everything we've got for you. Is there anything else you'd like to add?"


def _fallback_next_step(stage: str, coach, session=None) -> str:
    """Find the first unchecked item in the current stage and return its prompt.
    Simple, predictable, always gives the rep the right next question."""
    if not coach:
        return ""

    done = coach._topics_done
    items = _STAGE_ITEM_ORDER.get(stage, [])
    name = coach.customer_name or ""
    ctx = _get_discovery_context(coach)

    for key in items:
        # Skip kids_age if no kids mentioned
        if key == "kids_age" and not _customer_mentioned_kids(coach):
            continue
        if key not in done:
            prompt = _CHECKLIST_PROMPTS.get(key, "")
            # Dynamic recap from equipment list
            if key == "recap_done" and session:
                prompt = _build_recap_prompt(session)
            # Dynamic pricing from equipment counts
            elif key == "closing_pricing" and session:
                prompt = _build_pricing_prompt(session)
            # Dynamic personalization for certain build_system items
            elif key == "indoor_camera":
                prompt = _personalize_camera(ctx, name)
            elif key == "panel_hub":
                prompt = _personalize_panel(ctx, name)
            elif prompt and name:
                prompt = prompt.replace("[NAME]", name)
            if prompt:
                return prompt

    return ""


# ── Per-connection session ────────────────────────────────────────────────

class Session:
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.mic_queue: asyncio.Queue | None = None
        self.loopback_queue: asyncio.Queue | None = None
        self.transcriber: Transcriber | None = None
        self.coach: CoachingEngine | None = None
        self.roleplay_customer: RoleplayCustomer | None = None
        self.running = False
        self.roleplay_mode = False
        self.tts_active = False
        self.customer_buffer: list[str] = []
        self.rep_buffer: list[str] = []
        self.pending_rep_buffer: list[str] = []
        self._coach_task: asyncio.Task | None = None
        self._roleplay_task: asyncio.Task | None = None
        self.pending_evaluation: dict | None = None
        self.session_scores: list[int] = []
        self.current_stage = "intro"
        self.opener_shown = False
        self.intro_turns = 0
        self._collect_info_done: set[str] = set()
        self._rep_overrides: set[str] = set()  # items the rep unchecked — don't auto-recheck
        self._profile: dict = {"name": "", "phone": "", "email": "", "address": "", "equipment": []}
        self._profile_edits: set[str] = set()  # profile fields the rep manually corrected
        self._build_current_item: str | None = None  # which build_system item is being pitched next
        self._equipment_counts: dict = {}  # e.g. {"door_sensors": 2, "window_sensors": 5}
        self._equipment_edits: set[str] = set()  # equipment keys the rep manually corrected
        self._applied_coupons: list[str] = []  # coupon codes applied to this session
        self._pitch_keywords_said: set[str] = set()  # closing pitch keywords rep already said
        self._closing_pitch_groups_said: set[str] = set()  # tracks 3 closing_pitch sub-groups
        self._plan: str = "plus"  # "plus" or "basic"
        self._user_feedback: str = ""  # post-call feedback from rep
        self._opener_feedback: list[dict] = []  # [{opener, rating}] from rep thumbs up/down
        # opener tracking is now global (_active_opener_set) to survive WS reconnections

    async def send(self, msg: dict):
        try:
            await self.ws.send_text(json.dumps(msg))
        except Exception:
            pass

    # ── Session lifecycle ──

    async def start_live(self):
        if self.running:
            return
        # DON'T clear openers here — WS reconnections call start_live()
        # and would wipe all opener history. Cleared in stop() instead.
        self.roleplay_mode = False
        self.mic_queue = asyncio.Queue()
        self.loopback_queue = asyncio.Queue()
        self.coach = CoachingEngine(ANTHROPIC_API_KEY)
        self.transcriber = Transcriber(DEEPGRAM_API_KEY)
        self.transcriber.start(self.mic_queue, self.loopback_queue, self._on_transcript)
        self.running = True
        await self.send({"type": "status", "state": "recording"})

    async def start_roleplay(self):
        if self.running:
            return
        # DON'T clear openers here — cleared in stop() instead.
        self.roleplay_mode = True
        self.mic_queue = asyncio.Queue()
        self.coach = CoachingEngine(ANTHROPIC_API_KEY)
        self.roleplay_customer = RoleplayCustomer(ANTHROPIC_API_KEY)
        self.transcriber = Transcriber(DEEPGRAM_API_KEY)
        self.transcriber.start_mic_only(self.mic_queue, self._on_transcript)
        self.running = True

        await self.send({"type": "status", "state": "recording"})
        await self.send({"type": "roleplay_mode", "active": True})

        try:
            opening = await self.roleplay_customer.opening_line()
            print(f"[roleplay] opening line: {opening[:80]}")
        except Exception as e:
            import traceback
            print(f"[roleplay] opening_line FAILED: {e}\n{traceback.format_exc()}")
            opening = "Hi, yeah I'm calling about getting a home security system."
            # Seed roleplay history so responses still work
            self.roleplay_customer._history = [
                {"role": "user", "content": "You just called Cove Smart security. Give your short, natural opening line as the customer."},
                {"role": "assistant", "content": opening},
            ]

        audio_b64 = await _tts(opening, self.roleplay_customer.voice)
        if audio_b64:
            self.tts_active = True
            asyncio.create_task(self._tts_safety_reset(3))
        await self.send({"type": "roleplay_speech", "text": opening, "audio_b64": audio_b64})
        await self._on_transcript("customer", opening, True, True)

    async def stop(self):
        self.tts_active = False
        was_running = self.running
        was_roleplay = self.roleplay_mode
        _reset_opener_tracking()  # clear for next call

        if not self.running:
            self.current_stage = "intro"
            self.opener_shown = False
            self.intro_turns = 0
            self._collect_info_done = set()
            self._rep_overrides = set()
            self._profile_edits = set()
            self._build_current_item = None
            self._equipment_counts = {}
            self._equipment_edits = set()
            self._closing_pitch_groups_said = set()
            self._plan = "plus"
            return

        # ── Save transcript immediately (feedback attached later via REST) ──
        if self.coach and self.coach._history:
            try:
                save_transcript(
                    mode="roleplay" if was_roleplay else "live",
                    history=list(self.coach._history),
                    stage_reached=self.current_stage,
                    topics_done=list(self.coach._topics_done),
                    equipment_mentioned=list(self.coach._equipment_mentioned),
                    customer_name=self.coach.customer_name or "",
                    scores=list(self.session_scores),
                    profile=dict(self._profile),
                    scenario=(self.roleplay_customer._persona if self.roleplay_customer else ""),
                    rep_overrides=list(self._rep_overrides),
                    profile_edits=list(self._profile_edits),
                    equipment_edits=list(self._equipment_edits),
                    user_feedback="",
                    opener_feedback=list(self._opener_feedback),
                )
            except Exception as e:
                print(f"[transcript] save failed: {e}")

        # ── Cancel tasks ──
        if self._coach_task and not self._coach_task.done():
            self._coach_task.cancel()
        if self._roleplay_task and not self._roleplay_task.done():
            self._roleplay_task.cancel()
        self._coach_task = None
        self._roleplay_task = None
        self.customer_buffer = []
        self.rep_buffer = []
        self.pending_rep_buffer = []
        self.pending_evaluation = None
        self.session_scores = []
        self.current_stage = "intro"
        self.opener_shown = False
        self.intro_turns = 0
        self._collect_info_done = set()
        self._rep_overrides = set()
        self._build_current_item = None
        self._equipment_counts = {}
        self._closing_pitch_groups_said = set()

        if self.mic_queue:
            self.mic_queue.put_nowait(None)
        if self.loopback_queue:
            self.loopback_queue.put_nowait(None)
        self.mic_queue = None
        self.loopback_queue = None
        if self.transcriber:
            await self.transcriber.stop()
            self.transcriber = None
        if self.coach:
            self.coach.reset()
            self.coach = None
        if self.roleplay_customer:
            self.roleplay_customer.reset()
            self.roleplay_customer = None

        self.running = False
        self.roleplay_mode = False
        await self.send({"type": "status", "state": "idle"})
        await self.send({"type": "roleplay_mode", "active": False})

    # ── Checklist state broadcast ──

    # Maps internal topic/equipment keys to frontend checklist keys
    _TOPIC_TO_CHECKLIST = {
        "existing_customer": "existing_customer",
        "why_security": "why_security",
        "had_system_before": "had_system_before",
        "who_protecting": "who_protecting",
        "on_website": "on_website",
        "kids_age": "kids_age",
        "prior_provider": "had_system_before",  # counts as part of "had system"
        "full_name": "full_name",
        "phone_number": "phone_number",
        "email": "email",
        "address": "address",
    }
    _EQUIP_TO_CHECKLIST = {
        "door sensor": "door_sensors",
        "window sensor": "window_sensors",
        "motion sensor": "extra_equip",
        "camera": "indoor_camera",
        "outdoor camera": "outdoor_camera",
        "panel": "panel_hub",
        "monitoring": "panel_hub",
        "yard sign": "yard_sign",
        "smartphone": "yard_sign",
        "smoke detector": "extra_equip",
        "glass break": "extra_equip",
        "co detector": "extra_equip",
        "chime": "door_sensors",  # chime is part of door/window sensor pitch
    }

    # Which checklist keys belong to which stage
    _STAGE_FOR_KEY = {
        "existing_customer": "discovery", "why_security": "discovery",
        "had_system_before": "discovery",
        "who_protecting": "discovery", "kids_age": "discovery", "on_website": "discovery",
        "full_name": "collect_info", "phone_number": "collect_info",
        "email": "collect_info", "address": "collect_info",
        "door_sensors": "build_system", "window_sensors": "build_system",
        "extra_equip": "build_system", "indoor_camera": "build_system",
        "outdoor_camera": "build_system", "panel_hub": "build_system",
        "yard_sign": "build_system",
        "recap_done": "build_system", "anything_else": "build_system",
        "closing_pitch": "closing", "closing_pricing": "closing",
        "closing_cart": "closing", "closing_checkout": "closing",
        "closing_welcome": "closing",
    }

    async def send_checklist(self):
        """Broadcast current checklist state to the frontend.
        Only sends items for the current stage and completed stages.
        Respects rep overrides."""
        if not self.coach:
            return

        # Determine which stages are allowed (current + all before it)
        # Include discovery always since the checklist shows from the start
        cur_idx = _STAGE_ORDER.index(self.current_stage) if self.current_stage in _STAGE_ORDER else 0
        allowed_stages = set(_STAGE_ORDER[:cur_idx + 1])
        # Always allow discovery items since the checklist is visible from intro
        allowed_stages.add("discovery")

        topics = {}
        # Collect_info items — ONLY from _collect_info_done (not _topics_done)
        # This prevents Claude's aggressive topic tracking from auto-checking
        _COLLECT_KEYS = {"full_name", "phone_number", "email", "address"}
        for internal_key, checklist_key in self._TOPIC_TO_CHECKLIST.items():
            if checklist_key in self._rep_overrides:
                continue
            stage_for = self._STAGE_FOR_KEY.get(checklist_key)
            if stage_for and stage_for not in allowed_stages:
                continue
            if checklist_key in _COLLECT_KEYS:
                # Only mark if the fast-track actually confirmed this info
                if internal_key in self._collect_info_done:
                    topics[checklist_key] = True
            else:
                if internal_key in self.coach._topics_done:
                    topics[checklist_key] = True
        # Build system equipment
        for internal_key, checklist_key in self._EQUIP_TO_CHECKLIST.items():
            if checklist_key in self._rep_overrides:
                continue
            stage_for = self._STAGE_FOR_KEY.get(checklist_key)
            if stage_for and stage_for not in allowed_stages:
                continue
            if internal_key in self.coach._equipment_mentioned:
                topics[checklist_key] = True

        # Show kids_age checkbox only when kids are mentioned
        if _customer_mentioned_kids(self.coach) or "kids_age" in self.coach._topics_done:
            topics["_show_kids_age"] = True

        if topics:
            print(f"[checklist] sending topics={topics} (stage={self.current_stage}, topics_done={sorted(self.coach._topics_done)})")
        await self.send({"type": "checklist_update", "topics": topics})

    async def send_profile(self):
        """Broadcast current customer profile to the frontend."""
        await self.send({"type": "profile_update", **self._profile})

    async def send_pricing(self):
        """Broadcast current pricing breakdown to the frontend."""
        items = []
        for key, qty in self._equipment_counts.items():
            if qty > 0 and key in _EQUIPMENT_PRICES:
                price = _EQUIPMENT_PRICES[key]
                label = key.replace("_", " ").title()
                items.append({"key": key, "label": label, "qty": qty,
                              "unit_price": price, "line_total": price * qty})
        # Always include panel+hub if not explicitly set to 0
        if "panel_hub" not in self._equipment_counts or self._equipment_counts.get("panel_hub", 0) > 0:
            if not any(i["key"] == "panel_hub" for i in items):
                items.append({"key": "panel_hub", "label": "Panel + Hub",
                              "qty": 1, "unit_price": _EQUIPMENT_PRICES["panel_hub"],
                              "line_total": _EQUIPMENT_PRICES["panel_hub"]})

        pricing = _calculate_pricing(self)
        await self.send({
            "type": "pricing_update",
            "items": items,
            "subtotal": pricing["subtotal"],
            "discount_total": pricing["discount_total"],
            "discount_label": pricing["discount_label"],
            "equipment_total": pricing["equipment_total"],
            "monthly_promo": pricing["monthly_promo"],
            "monthly_standard": pricing["monthly_standard"],
            "monitoring_discount": pricing["monitoring_discount"],
            "promo_months": _PROMO_MONTHS,
            "applied_coupons": pricing["applied_coupons"],
            "available_coupons": {code: info["label"] for code, info in _COUPON_CODES.items()},
        })

    async def apply_coupon(self, code: str):
        """Apply a coupon code to the session."""
        code = code.upper().strip()
        if code in _COUPON_CODES and code not in self._applied_coupons:
            self._applied_coupons.append(code)
            print(f"[coupon] applied: {code} — {_COUPON_CODES[code]['label']}")
            await self.send_pricing()
        elif code not in _COUPON_CODES:
            await self.send({"type": "coupon_error", "code": code, "error": "Invalid coupon code"})

    async def remove_coupon(self, code: str):
        """Remove a coupon code from the session."""
        code = code.upper().strip()
        if code in self._applied_coupons:
            self._applied_coupons.remove(code)
            print(f"[coupon] removed: {code}")
            await self.send_pricing()

    async def update_profile_field(self, field: str, value: str):
        """Rep edited a profile field."""
        if field in self._profile:
            self._profile[field] = value
            self._profile_edits.add(field)
            print(f"[profile] rep edited {field}: {value[:30]}")

    def _build_equipment_list(self) -> list[dict]:
        """Build structured equipment list with quantities from coach state."""
        if not self.coach:
            return []
        items = []
        equip = self.coach._equipment_mentioned
        seen = set()
        for e in equip:
            if e in ("chime", "monitoring", "smartphone"):
                continue  # not separate items
            if e in seen:
                continue
            seen.add(e)
            _MAP = {
                "door sensor":    {"key": "door_sensors",    "label": "Door sensors"},
                "window sensor":  {"key": "window_sensors",  "label": "Window sensors"},
                "camera":         {"key": "indoor_camera",   "label": "Indoor camera"},
                "outdoor camera": {"key": "outdoor_camera",  "label": "Outdoor camera"},
                "panel":          {"key": "panel_hub",       "label": "Panel + hub"},
                "yard sign":      {"key": "yard_sign",       "label": "Yard sign + stickers"},
                "motion sensor":  {"key": "motion_sensor",   "label": "Motion detector"},
                "smoke detector": {"key": "smoke_detector",  "label": "Smoke detector"},
                "glass break":    {"key": "glass_break",     "label": "Glass break detector"},
                "co detector":    {"key": "co_detector",     "label": "CO detector"},
                "key fob":        {"key": "key_fob",         "label": "Key fob"},
                "medical pendant":{"key": "medical_pendant", "label": "Medical pendant"},
            }
            info = _MAP.get(e)
            if info:
                qty = self._equipment_counts.get(info["key"], 1)
                items.append({"key": info["key"], "label": info["label"], "qty": qty})
                seen.add(info["key"])
        # Also include items from _equipment_counts that weren't in _equipment_mentioned
        # (e.g., rep manually added via "Add Equipment" button)
        _COUNTS_MAP = {
            "door_sensors":    "Door sensors",
            "window_sensors":  "Window sensors",
            "motion_sensor":   "Motion detector",
            "glass_break":     "Glass break detector",
            "co_detector":     "CO detector",
            "smoke_detector":  "Smoke detector",
            "indoor_camera":   "Indoor camera",
            "outdoor_camera":  "Outdoor camera",
            "doorbell_camera": "Doorbell camera",
            "panel_hub":       "Panel + hub",
            "yard_sign":       "Yard sign + stickers",
            "key_fob":         "Key fob",
            "flood_sensor":    "Flood sensor",
            "medical_pendant": "Medical pendant",
        }
        for key, qty in self._equipment_counts.items():
            if key not in seen and key in _COUNTS_MAP:
                items.append({"key": key, "label": _COUNTS_MAP[key], "qty": qty})
        return items

    async def toggle_topic(self, topic: str, checked: bool):
        """Rep manually checked/unchecked a checklist item."""
        if not self.coach:
            return

        # Track rep overrides — if rep unchecks, don't let AI auto-recheck
        if not checked:
            self._rep_overrides.add(topic)
        else:
            self._rep_overrides.discard(topic)

        # Map frontend key back to internal keys
        reverse_topic = {v: k for k, v in self._TOPIC_TO_CHECKLIST.items()}
        reverse_equip = {v: k for k, v in self._EQUIP_TO_CHECKLIST.items()}

        if topic in reverse_topic:
            internal = reverse_topic[topic]
            if checked:
                self.coach._topics_done.add(internal)
                self._collect_info_done.add(internal)
            else:
                self.coach._topics_done.discard(internal)
                self._collect_info_done.discard(internal)
                # Also clear bridge sentinels so fallback can re-fire
                self.coach._topics_done.discard("_discovery_bridge")
                self.coach._topics_done.discard("_collect_bridge")
            print(f"[checklist] rep {'checked' if checked else 'unchecked'} topic: {topic} -> {internal}")
        elif topic in reverse_equip:
            internal = reverse_equip[topic]
            if checked:
                if internal not in self.coach._equipment_mentioned:
                    self.coach._equipment_mentioned.append(internal)
                # Also mark in _topics_done so _fallback_next_step skips this item
                self.coach._topics_done.add(topic)
                # Advance _build_current_item to next unchecked item
                _build_order = _STAGE_ITEM_ORDER.get("build_system", [])
                _ci = _build_order.index(topic) if topic in _build_order else -1
                if _ci >= 0:
                    self._build_current_item = None
                    for _next in _build_order[_ci + 1:]:
                        if _next not in self.coach._topics_done:
                            self._build_current_item = _next
                            break
            else:
                if internal in self.coach._equipment_mentioned:
                    self.coach._equipment_mentioned.remove(internal)
                self.coach._topics_done.discard(topic)
                self.coach._topics_done.discard("_build_recap")
                # Reset _build_current_item to the unchecked item
                self._build_current_item = topic
            print(f"[checklist] rep {'checked' if checked else 'unchecked'} equip: {topic} -> {internal}")
        else:
            # Closing items or custom keys — just track in topics_done
            if checked:
                self.coach._topics_done.add(topic)
            else:
                self.coach._topics_done.discard(topic)
            print(f"[checklist] rep {'checked' if checked else 'unchecked'} custom: {topic}")

        # If checking, cancel any in-flight Claude coaching (it doesn't know
        # about this check yet and would overwrite our prompt), then show the
        # next unchecked item.
        if checked:
            if self._coach_task and not self._coach_task.done():
                self._coach_task.cancel()
                print(f"[checklist] cancelled in-flight coaching after rep check")
            await self.send_checklist()

            # For build_system items, scan transcript for context (e.g., customer
            # said "just two" doors) so the next prompt flows naturally.
            context_prefix = ""
            if self.current_stage == "build_system" and self.coach and self.coach._history:
                context_prefix = _build_context_from_transcript(topic, self.coach)

            # Show next prompt so the rep always knows what to ask next
            next_prompt = _fallback_next_step(self.current_stage, self.coach, session=self)
            if not next_prompt:
                # Current stage fully done — try next stage
                _next_stages = {"intro": "discovery", "discovery": "collect_info",
                                "collect_info": "build_system", "build_system": "closing"}
                _ns = _next_stages.get(self.current_stage, "")
                if _ns:
                    next_prompt = _fallback_next_step(_ns, self.coach, session=self)
            if next_prompt:
                if context_prefix:
                    next_prompt = context_prefix + " " + next_prompt
                if self.coach and self.coach.customer_name:
                    next_prompt = next_prompt.replace("[NAME]", self.coach.customer_name)
                opener = self.coach._last_opener if self.coach else ""
                guidance = {"type": "call_guidance", "call_stage": self.current_stage,
                            "next_step": next_prompt}
                if opener:
                    guidance["opener"] = opener
                await self.send(guidance)
            # Update equipment list in profile panel
            self._profile["equipment"] = self._build_equipment_list()
            await self.send_profile()
            return

        # If unchecking, show the prompt for the unchecked item directly
        # Use dynamic prompts for items that are generated at runtime
        prompt = None
        if topic == "closing_pricing":
            prompt = _build_pricing_prompt(self)
        elif topic == "recap_done":
            prompt = _build_recap_prompt(self)
        else:
            prompt = _CHECKLIST_PROMPTS.get(topic, "")
        stage_for_item = self._STAGE_FOR_KEY.get(topic, self.current_stage)
        if prompt:
            if self.coach and self.coach.customer_name:
                prompt = prompt.replace("[NAME]", self.coach.customer_name)
            await self.send({"type": "call_guidance", "call_stage": stage_for_item,
                             "next_step": prompt})
        # Force-send the unchecked state immediately so it can't be overridden
        # by a concurrent send_checklist from Claude's coaching
        await self.send({"type": "checklist_update", "topics": {topic: False}})
        await self.send_checklist()

    # ── Go Back (rep manually rewinds one step) ──

    _COLLECT_INFO_SEQUENCE = ["full_name", "phone_number", "email", "address"]
    _COLLECT_INFO_PROMPTS = {
        "full_name": "Could you please spell your first and last name for me?",
        "phone_number": "And what's your best phone number?",
        "email": "And your email so I can send all this information over to you?",
        "address": "What's the address you're looking to get the security set up at?",
    }

    async def go_back(self):
        """Rep pressed the back button — rewind one step in the current stage."""
        if not self.coach:
            return

        # In collect_info or just entered build_system: rewind collect_info sequence
        if self.current_stage in ("collect_info", "build_system"):
            # Find the last item that was marked done
            done_items = [k for k in self._COLLECT_INFO_SEQUENCE if k in self._collect_info_done]
            if done_items:
                last = done_items[-1]
                self._collect_info_done.discard(last)
                if self.coach:
                    self.coach._topics_done.discard(last)
                # If we were in build_system, go back to collect_info
                if self.current_stage == "build_system":
                    self.current_stage = "collect_info"
                prompt = self._COLLECT_INFO_PROMPTS[last]
                print(f"[go_back] rewound to {last}: {prompt[:40]}")
                await self.send({"type": "call_guidance", "call_stage": self.current_stage,
                                 "next_step": prompt})  # opener hidden
                return

        # In build_system: rewind equipment
        if self.current_stage == "build_system" and self.coach:
            equip = self.coach._equipment_mentioned
            if equip:
                removed = equip.pop()
                print(f"[go_back] rewound equipment: removed {removed}")
                fallback = _fallback_next_step("build_system", self.coach, session=self)
                if fallback:
                    await self.send({"type": "call_guidance", "call_stage": "build_system",
                                     "next_step": fallback})  # opener hidden
                return

        # In discovery: rewind topic
        if self.current_stage == "discovery" and self.coach:
            discovery_topics = ["who_protecting", "had_system_before", "why_security"]
            for topic in discovery_topics:
                if topic in self.coach._topics_done:
                    self.coach._topics_done.discard(topic)
                    self.coach._topics_done.discard("_discovery_bridge")
                    fallback = _fallback_next_step("discovery", self.coach, session=self)
                    if fallback:
                        print(f"[go_back] rewound discovery topic: {topic}")
                        await self.send({"type": "call_guidance", "call_stage": "discovery",
                                         "next_step": fallback})  # opener hidden
                    return

        print("[go_back] nothing to rewind")

    # ── Coaching ──

    async def _fire_coaching(self):
        try:
            if not self.customer_buffer or self.coach is None:
                return
            self.customer_buffer = []
            await self.send({"type": "status", "state": "processing"})
            suggestion = await self.coach.get_suggestion()
            await self.send({"type": "status", "state": "recording"})

            # Claude NEVER changes the stage — only controlled code paths
            # (fast-tracks, section-complete clicks) can advance stages.
            suggested_stage = suggestion.get("call_stage") or self.current_stage
            if suggested_stage != self.current_stage:
                print(f"[coach] IGNORED stage suggestion '{suggested_stage}', staying at '{self.current_stage}'")
            new_stage = self.current_stage
            suggestion["call_stage"] = new_stage

            self.opener_shown = False
            raw_next = suggestion.get("next_step", "")

            if raw_next:
                # Check if Claude is re-asking a question already covered
                repeated = self.coach.check_repeated_topic(raw_next) if self.coach else None
                if repeated:
                    print(f"[coach] BLOCKED repeated topic '{repeated}' in next_step, using fallback")
                    raw_next = ""  # fall through to fallback

            if raw_next:
                opener_used = self.coach._last_opener if self.coach else ""
                cleaned_next = _strip_fluff_for_opener(opener_used, raw_next)
                suggestion["next_step"] = cleaned_next
            else:
                # P0 FIX: Fallback for empty Then — generate a stage-appropriate
                # next step so the rep never sees an opener with no follow-up.
                cleaned_next = _fallback_next_step(new_stage, self.coach, session=self)
                if not cleaned_next:
                    # All items in current stage done — try next stage's first item
                    _next_stages = {"intro": "discovery", "discovery": "collect_info",
                                    "collect_info": "build_system", "build_system": "recap",
                                    "recap": "closing"}
                    _ns = _next_stages.get(new_stage, "")
                    if _ns:
                        cleaned_next = _fallback_next_step(_ns, self.coach, session=self)
                if not cleaned_next:
                    # Last resort: generic stage-appropriate prompt
                    cleaned_next = {
                        "intro": "Are you already a Cove customer, or are you looking to get a security system?",
                        "discovery": "Is there anything else about your situation I should know before we move on?",
                        "collect_info": "Let me verify I have everything — is there anything I missed?",
                        "build_system": "Is there anything else you'd like to add to the system?",
                        "recap": "Does everything look good? Is there anything else you'd like me to add?",
                        "closing": "Does that sound like it will work for you?",
                    }.get(new_stage, "Is there anything else I can help you with?")
                suggestion["next_step"] = cleaned_next

            if self.coach and self.coach.customer_name and cleaned_next:
                cleaned_next = cleaned_next.replace("[NAME]", self.coach.customer_name)
                suggestion["next_step"] = cleaned_next

            # Personalize Claude's output if it mentions equipment generically
            if cleaned_next and self.coach and new_stage == "build_system":
                cleaned_next = _inject_personalization(cleaned_next, self.coach)

            # P1 FIX: Trim overly long suggestions so the rep can read them
            # P2 FIX: Remove "$____" placeholders that Claude sometimes outputs
            if cleaned_next:
                cleaned_next = _trim_long_suggestion(cleaned_next)
                cleaned_next = cleaned_next.replace("$____", "your discounted price")
                suggestion["next_step"] = cleaned_next

            if cleaned_next and self.coach and self.current_stage != "build_system":
                self.coach.track_equipment_from_text(cleaned_next)

            opener_used = self.coach._last_opener if self.coach else ""
            guidance_msg = {"type": "call_guidance", "call_stage": new_stage, "next_step": cleaned_next}
            if opener_used:
                guidance_msg["opener"] = opener_used
            await self.send(guidance_msg)
            await self.send_checklist()
            # Update profile equipment list
            self._profile["equipment"] = self._build_equipment_list()
            await self.send_profile()
            await self.send_pricing()

            if suggestion.get("triggered"):
                await self.send({"type": "coaching", **suggestion})
                self.coach.mark_addressed(suggestion.get("objection_type", ""))
                self.pending_evaluation = {
                    "objection_type": suggestion.get("objection_type", ""),
                    "objection_summary": suggestion.get("objection_summary", ""),
                }
        except Exception as e:
            import traceback
            print(f"[coach] _fire_coaching error: {e}\n{traceback.format_exc()}")
            await self.send({"type": "status", "state": "recording"})

    async def _delayed_coaching(self):
        try:
            await self._fire_coaching()
        except asyncio.CancelledError:
            await self.send({"type": "status", "state": "recording"})
        except Exception:
            await self.send({"type": "status", "state": "recording"})

    # ── Roleplay ──

    async def _fire_roleplay_response(self):
        if not self.rep_buffer or not self.roleplay_customer:
            print(f"[roleplay] _fire skipped: buffer={len(self.rep_buffer)}, customer={'set' if self.roleplay_customer else 'None'}")
            return
        combined = " ".join(self.rep_buffer)
        self.rep_buffer = []
        print(f"[roleplay] rep said ({len(combined)} chars): {combined[:200]}")

        # Try the API call, retry once on failure
        ai_text = None
        for attempt in range(2):
            try:
                ai_text = await self.roleplay_customer.respond(combined)
                print(f"[roleplay] AI response: {ai_text[:120]}")
                break
            except asyncio.CancelledError:
                raise
            except Exception:
                import traceback
                print(f"[roleplay] API attempt {attempt+1} failed:\n{traceback.format_exc()}")
                if attempt == 0:
                    # Fix dangling user message before retry
                    if (self.roleplay_customer._history
                            and self.roleplay_customer._history[-1]["role"] == "user"):
                        self.roleplay_customer._history.pop()
                    await asyncio.sleep(0.5)

        if not ai_text:
            ai_text = "Yeah, that sounds good."
            print(f"[roleplay] using fallback response")
            # Fix history for the fallback
            if (self.roleplay_customer._history
                    and self.roleplay_customer._history[-1]["role"] == "user"):
                self.roleplay_customer._history.append(
                    {"role": "assistant", "content": ai_text})

        try:
            audio_b64 = await _tts(ai_text, self.roleplay_customer.voice)
            if audio_b64:
                self.tts_active = True
                asyncio.create_task(self._tts_safety_reset(3))
            await self.send({"type": "roleplay_speech", "text": ai_text, "audio_b64": audio_b64})
            await self._on_transcript("customer", ai_text, True, True)
        except asyncio.CancelledError:
            raise
        except Exception:
            import traceback
            print(f"[roleplay] send/tts error:\n{traceback.format_exc()}")
            self.tts_active = False

    async def _delayed_roleplay_response(self, delay: float = 0.6):
        try:
            await asyncio.sleep(delay)
            await self._fire_roleplay_response()
        except asyncio.CancelledError:
            pass
        except Exception:
            self.tts_active = False

    async def _tts_safety_reset(self, seconds: int):
        await asyncio.sleep(seconds)
        if self.tts_active:
            print("[roleplay] tts_active safety reset fired — unmuting mic")
            self.tts_active = False

    # ── Transcript callback ──

    async def _on_transcript(self, speaker: str, text: str, is_final: bool, speech_final: bool):
        # In roleplay, mic is muted during TTS (echo prevention), so rep
        # transcripts here are just pipeline stragglers. Show them but skip
        # roleplay processing — real speech arrives after TTS ends.
        # However, if we get a substantial rep is_final while tts_active,
        # TTS likely already ended but the flag wasn't cleared (browser
        # SpeechSynthesis fallback doesn't send tts_playing:false, or
        # the WebSocket message was lost). Auto-clear and process.
        if self.roleplay_mode and self.tts_active and speaker == "rep":
            if is_final and len(text.strip().split()) >= 3:
                # Substantial speech detected — TTS must be done
                print(f"[roleplay] auto-clearing tts_active (rep speaking: {text[:50]})")
                self.tts_active = False
                # Fall through to process normally
            else:
                await self.send({"type": "transcript", "speaker": speaker, "text": text, "is_final": is_final})
                return

        await self.send({"type": "transcript", "speaker": speaker, "text": text, "is_final": is_final})

        # ── Fast-track intro ──
        if speaker == "customer" and is_final and self.coach is not None and self.current_stage == "intro":
            # Always check for "wants system" regardless of intro_turns.
            # Deepgram duplicate transcripts can exhaust intro_turns before the real answer arrives.
            self.intro_turns += 1
            self.coach.add_turn(speaker, text)
            self.customer_buffer = []  # clear so _fire_coaching doesn't also fire
            # Cancel any pending coaching task to prevent it overwriting our guidance
            if self._coach_task and not self._coach_task.done():
                self._coach_task.cancel()
            t = text.lower()
            _wants_system = any(w in t for w in [
                "looking to get", "looking for", "interested in", "i want", "i need",
                "i'm looking", "im looking", "get a system", "get a security",
                "set up", "get this", "security system", "protect",
                "planning to get", "planning to switch", "planning on getting",
                "want to get", "want to switch", "thinking about", "thinking of getting",
                "get one", "looking into", "ready to", "need a system",
                "switch to", "switching to", "not yet", "not a customer",
            ])
            _is_existing = any(w in t for w in [
                "already a customer", "existing customer", "i already have",
                "already have cove", "current customer",
            ])
            if _wants_system or _is_existing:
                # Customer said they want a system — advance to discovery
                self.current_stage = "discovery"
                self.intro_turns = 2
                self.coach._topics_done.add("existing_customer")
                opener = _quick_opener(text, "discovery")
                self.coach.set_opener(opener)
                guidance = {"type": "call_guidance", "call_stage": "discovery",
                    "next_step": "I'll be the one to help you with that, and I'm going to make sure you get a really good deal. Have you ever had a security system before?"}
                if opener:
                    guidance["opener"] = opener
                await self.send(guidance)
                await self.send_checklist()
                return
            # ── Mid-call join detection ──
            # If customer says something that looks like a name, number, or address
            # (not a greeting), we likely reconnected mid-call. Jump to collect_info.
            _GREETING_WORDS = {"hi", "hey", "hello", "good", "fine", "great", "well",
                               "doing", "pretty", "morning", "afternoon", "evening",
                               "how", "are", "you", "thanks", "thank"}
            stripped = text.strip()
            _words = stripped.lower().split()
            _is_greeting = all(w in _GREETING_WORDS for w in _words) or len(_words) == 0
            _has_digits = any(c.isdigit() for c in stripped)
            _is_name_like = (len(_words) <= 3 and stripped.replace(" ", "").isalpha()
                             and not _is_greeting and len(stripped) >= 3)

            if (_has_digits or _is_name_like) and not _is_greeting:
                # Looks like a mid-call reconnection — customer is giving info
                print(f"[intro] mid-call join detected (customer giving info): {text[:40]}")
                self.current_stage = "collect_info"
                # Re-process this text through the collect_info fast-track
                # by NOT returning — let it fall through to the collect_info handler below
                pass
            elif len(_words) <= 3 and self.intro_turns < 6:
                # Short greeting — show intro prompt
                opener = _quick_opener(text, "intro")
                self.coach.set_opener(opener)
                guidance = {"type": "call_guidance", "call_stage": "intro",
                    "next_step": "Are you already a Cove customer, or are you looking to get a security system?"}
                if opener:
                    guidance["opener"] = opener
                await self.send(guidance)
                await self.send_checklist()
                return
            # Substantive non-matching utterance — let Claude handle it.
            # The turn is already in coach history, so fall through to the
            # general coaching flow below instead of returning early.
            self.intro_turns = 2  # prevent re-entering fast-track
            print(f"[intro] customer said something non-standard, falling through to Claude coaching: {text[:80]}")

        # ── Fast-track collect_info ──
        # During collect_info, responses come rapid-fire (name, phone, email, address).
        # Claude coaching gets cancelled before completing, so use instant hardcoded suggestions.
        # IMPORTANT: Validate what the customer actually said before advancing.
        if speaker == "customer" and is_final and self.coach is not None and self.current_stage == "collect_info":
            self.coach.add_turn(speaker, text)
            self.customer_buffer = []  # clear so _fire_coaching doesn't also fire
            if self._coach_task and not self._coach_task.done():
                self._coach_task.cancel()

            # ── Accumulate recent customer turns since the last rep question ──
            # Customers split info across multiple segments ("seven zero two"
            # then "six one five eight two eight two"). Analyze them together.
            _recent_customer_texts = []
            for _h in reversed(self.coach._history[-10:]):
                if _h["speaker"] == "customer":
                    _recent_customer_texts.insert(0, _h["text"])
                elif _h["speaker"] == "rep":
                    break  # stop at the rep's last turn
            _combined = " ".join(_recent_customer_texts)
            _combined_lower = _combined.lower()

            # Use combined text for detection, single turn for fallback
            t = text.lower()

            # Detect what TYPE of info the customer just gave
            # Count digits — both actual digits AND spoken number words
            _SPOKEN_DIGITS = {"zero", "one", "two", "three", "four", "five",
                              "six", "seven", "eight", "nine", "oh"}
            # Count on combined text so split phone numbers get full digit count
            _digit_count = sum(c.isdigit() for c in _combined_lower)
            _digit_count += sum(1 for w in _combined_lower.split() if w in _SPOKEN_DIGITS)

            _NOT_NAMES = {"yes", "no", "yeah", "yep", "nope", "okay", "ok", "sure", "hello", "hi",
                         "yes sir", "no sir", "yes ma'am", "no ma'am", "little kids", "teenagers",
                         "my family", "my kids", "my wife", "my husband", "just me", "me and my",
                         "not yet", "not sure", "i think", "let me", "give me", "hold on",
                         "sounds good", "that works", "thank you", "thanks", "alright", "fine",
                         "of course", "yeah of course", "yes of course", "sure thing",
                         "go ahead", "absolutely", "right", "correct", "yea", "mhmm",
                         "no problem", "no worries", "one second", "one moment",
                         "yeah sure", "yes please", "please", "i will", "i can",
                         "yeah i can", "sure can", "that's me", "that's correct",
                         "you got it", "you got it yep", "that is correct",
                         "go ahead", "let me", "hold on", "one sec", "one moment",
                         "i'm good", "i'm here", "i'm ready", "i'm on"}
            # Also block individual filler words that aren't names —
            # if ALL words in the response are in this set, reject as name
            _NOT_NAME_WORDS = {"yeah", "yes", "no", "nope", "sure", "okay", "ok",
                               "of", "course", "right", "well", "just", "alright",
                               "please", "absolutely", "totally", "definitely",
                               "mhmm", "hmm", "um", "uh",
                               "you", "got", "it", "yep", "yea", "that", "this",
                               "i", "we", "he", "she", "they", "me", "my", "the",
                               "is", "was", "are", "am", "be", "do", "did", "can",
                               "a", "an", "so", "if", "or", "and", "but", "not",
                               "too", "to", "for", "at", "in", "on", "up", "oh",
                               "go", "ahead", "let", "now", "here", "there",
                               "thank", "thanks", "sir", "ma'am", "hi", "hey",
                               "will", "would", "should", "shall", "could",
                               "hold", "one", "second", "moment", "good", "great",
                               "fine", "nice", "cool", "perfect", "awesome",
                               "what", "how", "when", "where", "why", "who"}
            _is_short_alpha = len(t.split()) <= 4 and t.replace(" ", "").isalpha()
            # Check if ALL words are filler — if so, it's not a name
            _all_filler = all(w in _NOT_NAME_WORDS for w in t.split())
            _has_name_words = any(w in _combined_lower for w in ["my name is", "first name", "last name", "name is"]) or (
                _is_short_alpha and not _all_filler
                and t.strip() not in _NOT_NAMES
                and not any(phrase in t for phrase in _NOT_NAMES)
            )
            # Use combined text for phone/email/address so split responses are captured
            _has_phone_digits = _digit_count >= 7
            _has_email = "@" in _combined_lower or any(w in _combined_lower for w in [
                "gmail", "yahoo", "hotmail", "aol", "outlook",
                "dot com", "at gmail", "at yahoo", "at hotmail",
                "at aol", "at outlook"])
            _has_address = any(w in _combined_lower for w in [
                "street", "drive", "avenue", "road", "lane", "boulevard",
                "way", "circle", "court", "north", "south", "east", "west"]) or _digit_count >= 4

            # Only advance if the customer gave the expected info type
            opener = _quick_opener(text, "collect_info")
            self.coach.set_opener(opener)
            next_step = None

            if "full_name" not in self._collect_info_done:
                # Only accept as name if it explicitly has name words, OR
                # it's a short alphabetic phrase that doesn't match other info types
                # AND doesn't contain equipment/number words (avoids "a windows would be six")
                _EQUIP_WORDS = {"door", "doors", "window", "windows", "sensor", "sensors",
                                "camera", "panel", "motion", "detector", "hub"}
                # Check combined text for name indicators (customer may say
                # "my name is" in one turn and the actual name in the next)
                _has_name_in_combined = any(w in _combined_lower for w in [
                    "my name is", "first name", "last name", "name is"])
                _looks_like_name = (
                    _has_name_words or _has_name_in_combined or
                    (_is_short_alpha and not _has_phone_digits and not _has_email
                     and not _has_address and _digit_count == 0
                     and not any(w in t.split() for w in _EQUIP_WORDS))
                )
                if _looks_like_name:
                    self._collect_info_done.add("full_name")
                    # Use combined text if it has name context, otherwise just current turn
                    _name_source = _combined if _has_name_in_combined else text
                    self._profile["name"] = _extract_name(_name_source)
                    next_step = "And what's your best phone number?"
            elif "phone_number" not in self._collect_info_done:
                if _has_phone_digits:
                    self._collect_info_done.add("phone_number")
                    # Extract digits from combined customer turns (handles split segments)
                    digits = _spoken_to_digits(_combined)
                    if not digits:
                        digits = "".join(c for c in _combined if c.isdigit())
                    if len(digits) == 10:
                        self._profile["phone"] = f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
                    elif len(digits) >= 7:
                        self._profile["phone"] = digits
                    else:
                        self._profile["phone"] = _combined.strip()
                    next_step = "And your email so I can send all this information over to you by the end of the call?"
            elif "email" not in self._collect_info_done:
                if _has_email:
                    self._collect_info_done.add("email")
                    # Use accumulated customer turns (already combined above)
                    self._profile["email"] = _extract_email(_combined)
                    next_step = "And before we get ahead of ourselves, I just want to verify we have coverage. What's the address you're looking to get the security set up at?"
            elif "address" not in self._collect_info_done:
                if _has_address:
                    self._collect_info_done.add("address")
                    # Use combined customer turns for address (often split across
                    # 3-4 segments: "five five" / "fitch meadow lane" / "south windsor" / "zero six...")
                    addr = _combined.strip()
                    for filler in ["the address is ", "address is ", "it is ", "it's ",
                                   "this is ", "we're at ", "i'm at ", "yeah it's ",
                                   "yeah ", "yes ", "alright ", "okay ", "let me ",
                                   "give me a second ", "that will be ", "that would be ",
                                   "alright that will be ", "alright let me ",
                                   "of course ", "of course that is ", "of course it's ",
                                   "sure ", "sure it's ", "yep ", "yep it's ",
                                   "so that's ", "so it's ", "that's "]:
                        if addr.lower().startswith(filler):
                            addr = addr[len(filler):]
                    self._profile["address"] = _spoken_numbers_to_numerals(addr.strip())
                    # Don't auto-advance to build_system — rep must click
                    # "INFO COMPLETE" to advance. Just show coverage confirmation.
                    opener = _pick(["Perfect, we actually have fantastic coverage in your area so I can definitely help you out.",
                                    "Great news — we have great coverage out there, so I can definitely take care of you.",
                                    "Awesome — we can definitely service that area, so you're in great shape."])
                    if not opener:
                        opener = "Perfect, we have great coverage in your area."
                    self.coach.set_opener(opener)
                    next_step = "Let's go ahead and build your system. How many doors go in and out of your home?"
                    self.coach._topics_done.add("full_name")
                    self.coach._topics_done.add("phone_number")
                    self.coach._topics_done.add("email")
                    self.coach._topics_done.add("address")

            if next_step:
                guidance = {"type": "call_guidance", "call_stage": self.current_stage, "next_step": next_step}
                if opener:
                    guidance["opener"] = opener
                await self.send(guidance)
                await self.send_checklist()
                await self.send_profile()
                await self.send_pricing()
                return
            else:
                # Customer said something during collect_info that didn't match
                # the expected info type (e.g. "yes sir" confirming a spelling).
                # Suppress Claude coaching to prevent it jumping ahead to
                # build_system while we're still collecting info.
                return

        # ── Fast-track build_system ──
        # Fully handles ALL customer speech during build_system — Claude is
        # never called. Uses _build_current_item to track what's being pitched.
        # IMPORTANT: Only act on speech_final (customer finished their full thought)
        # to prevent suggestions from jumping ahead mid-sentence.
        if speaker == "customer" and is_final and self.coach is not None and self.current_stage == "build_system":
            self.coach.add_turn(speaker, text)
            self.customer_buffer = []
            if self._coach_task and not self._coach_task.done():
                self._coach_task.cancel()

            t = text.lower()
            words = t.split()

            # ── Parse number from customer speech ──
            # Numbers are processed IMMEDIATELY (no speech_final wait) because
            # "just one" or "three" are complete answers.
            _SPOKEN_NUMS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                            "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
                            "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15}
            # Transcription homophones — only match in short responses to avoid false positives
            _HOMOPHONE_NUMS = {"too": 2, "to": 2, "for": 4, "ate": 8, "won": 1, "tree": 3}
            _cust_number = None
            for word, val in _SPOKEN_NUMS.items():
                if word in words:
                    _cust_number = val
                    break
            if _cust_number is None:
                import re as _re3
                _nums = _re3.findall(r'\d+', t)
                if _nums:
                    _cust_number = int(_nums[0])
            if _cust_number is None and len(words) <= 3:
                # Try homophones only for very short responses
                for word, val in _HOMOPHONE_NUMS.items():
                    if word in words:
                        _cust_number = val
                        break

            # ── speech_final gate for non-number responses ──
            # Numbers are processed immediately. Yes/no and longer responses
            # wait for speech_final to prevent mid-sentence jumps.
            if _cust_number is None and not speech_final:
                await self.send_checklist()
                return

            # ── Detect yes/no/ok responses ──
            _is_yes = any(w in t for w in ["yes", "yeah", "yep", "sure", "absolutely", "okay", "ok",
                                            "sounds good", "that works", "makes sense", "mhmm", "of course",
                                            "yes sir", "yes ma'am", "yea", "alright"])
            _is_no = any(w in t for w in ["no", "nope", "don't need", "don't want", "i'm good",
                                           "skip", "no thanks", "not really", "not right now",
                                           "no sir", "no ma'am", "no thank"])

            build_handled = False
            next_step = None
            name = self.coach.customer_name or ""
            ctx = _get_discovery_context(self.coach)

            # Use _build_current_item to determine context — much more reliable
            # than scanning rep speech keywords
            _cur = self._build_current_item

            # If no current item set yet, default to first unchecked build item
            if _cur is None:
                for _key in _STAGE_ITEM_ORDER.get("build_system", []):
                    if _key not in self.coach._topics_done:
                        _cur = _key
                        break

            if _cur == "door_sensors" and _cust_number is not None:
                self.coach._topics_done.add("door_sensors")
                self.coach._topics_done.add("how_many_doors")
                if "door sensor" not in self.coach._equipment_mentioned:
                    self.coach._equipment_mentioned.append("door sensor")
                self._equipment_counts["door_sensors"] = _cust_number
                suffix = f", {name}" if name else ""
                next_step = (f"{_cust_number} doors — I'll get you {_cust_number} door sensors so all your entry points are covered. "
                             f"And how many windows are on the ground floor of your house that are accessible{suffix}?")
                self._build_current_item = "window_sensors"
                build_handled = True

            elif _cur == "window_sensors" and _cust_number is not None:
                self.coach._topics_done.add("window_sensors")
                self.coach._topics_done.add("how_many_windows")
                if "window sensor" not in self.coach._equipment_mentioned:
                    self.coach._equipment_mentioned.append("window sensor")
                self._equipment_counts["window_sensors"] = _cust_number
                chime = _personalize_chime(ctx, name)
                next_step = (f"{_cust_number} windows — I'll get you {_cust_number} window sensors as well, "
                             f"that way every entry point is covered and monitored. {chime}")
                self._build_current_item = "extra_equip"
                build_handled = True

            elif _cur == "extra_equip" and (_is_yes or _is_no or
                    any(w in t for w in ["motion", "glass break", "carbon monoxide", "co detector"])):
                self.coach._topics_done.add("extra_equip")
                # Detect which specific extras the customer wants
                _wants_motion = any(w in t for w in ["motion", "motion sensor", "motion detect"])
                _wants_glass = any(w in t for w in ["glass break", "glass sensor"])
                _wants_co = any(w in t for w in ["carbon monoxide", "co detector", "c o detector", "carbon"])
                # NOTE: Do NOT default generic "yes" to motion sensor — this was
                # causing phantom motion detectors. If customer says "yes" without
                # specifying which extra, the rep should clarify. Only add what
                # the customer explicitly names.
                if _wants_motion:
                    if "motion sensor" not in self.coach._equipment_mentioned:
                        self.coach._equipment_mentioned.append("motion sensor")
                    self._equipment_counts["motion_sensor"] = 1
                if _wants_glass:
                    if "glass break" not in self.coach._equipment_mentioned:
                        self.coach._equipment_mentioned.append("glass break")
                    self._equipment_counts["glass_break"] = 1
                if _wants_co:
                    if "co detector" not in self.coach._equipment_mentioned:
                        self.coach._equipment_mentioned.append("co detector")
                    self._equipment_counts["co_detector"] = 1
                self._build_current_item = "indoor_camera"
                next_step = _fallback_next_step("build_system", self.coach, session=self)
                build_handled = True

            elif _cur == "outdoor_camera" and any(w in t for w in ["doorbell", "door bell"]):
                # Customer specifically asked for a doorbell camera — treat as yes
                self.coach._topics_done.add("outdoor_camera")
                if "outdoor camera" not in self.coach._equipment_mentioned:
                    self.coach._equipment_mentioned.append("outdoor camera")
                self._equipment_counts["outdoor_camera"] = 1
                _build_order = _STAGE_ITEM_ORDER.get("build_system", [])
                _ci = _build_order.index("outdoor_camera") if "outdoor_camera" in _build_order else -1
                self._build_current_item = _build_order[_ci + 1] if _ci + 1 < len(_build_order) else None
                next_step = _fallback_next_step("build_system", self.coach, session=self)
                build_handled = True

            elif _cur in ("indoor_camera", "outdoor_camera", "panel_hub", "yard_sign") and (_is_yes or _is_no):
                # Only advance for SHORT, clear responses — not conversational fillers
                # while the rep is still presenting. "yeah" mid-pitch shouldn't advance.
                _word_count = len(t.split())
                if _word_count > 8:
                    # Long response — customer is saying more than just yes/no.
                    # Don't advance, let the rep read the current suggestion.
                    build_handled = False
                else:
                    self.coach._topics_done.add(_cur)
                    # Add equipment to list regardless — qty=0 means declined
                    _equip_map = {"indoor_camera": "camera", "outdoor_camera": "outdoor camera",
                                  "panel_hub": "panel", "yard_sign": "yard sign"}
                    ek = _equip_map.get(_cur)
                    if ek and ek not in self.coach._equipment_mentioned:
                        self.coach._equipment_mentioned.append(ek)
                    self._equipment_counts[_cur] = 1 if _is_yes else 0
                    # Advance to next item
                    _build_order = _STAGE_ITEM_ORDER.get("build_system", [])
                    _ci = _build_order.index(_cur) if _cur in _build_order else -1
                    self._build_current_item = _build_order[_ci + 1] if _ci + 1 < len(_build_order) else None
                    next_step = _fallback_next_step("build_system", self.coach, session=self)
                    build_handled = True

            elif _cust_number is not None and _cur in ("door_sensors", "window_sensors"):
                # Number given but _cur check above didn't match (shouldn't happen,
                # but handle defensively)
                pass  # fall through to generic handler below

            # If handled, send guidance
            if build_handled:
                opener = _quick_opener(text, "build_system")
                self.coach.set_opener(opener)
                if not next_step:
                    next_step = _fallback_next_step("build_system", self.coach, session=self)
                if not next_step:
                    # All build items done — show recap prompt
                    next_step = _fallback_next_step("recap", self.coach, session=self)
                if next_step and name:
                    next_step = next_step.replace("[NAME]", name)
                if next_step:
                    next_step = _trim_long_suggestion(next_step)
                guidance = {"type": "call_guidance", "call_stage": self.current_stage,
                            "next_step": next_step or ""}
                if opener:
                    guidance["opener"] = opener
                await self.send(guidance)
                self._profile["equipment"] = self._build_equipment_list()
                await self.send_checklist()
                await self.send_profile()
                await self.send_pricing()
                return

            # Unhandled customer response — still suppress Claude, re-show current prompt
            next_step = _fallback_next_step("build_system", self.coach, session=self)
            if next_step and name:
                next_step = next_step.replace("[NAME]", name)
            if next_step:
                await self.send({"type": "call_guidance", "call_stage": self.current_stage,
                                 "next_step": next_step})
            return

        # ── Opener ──
        # Only show opener on speech_final (complete utterance) to prevent
        # the Say First from changing mid-speech as interim results come in.
        if speaker == "customer" and not self.opener_shown and self.coach is not None:
            if is_final and speech_final:
                # Get last rep text for context-aware openers (e.g., website question)
                _last_rep = ""
                if self.coach and self.coach._history:
                    for h in reversed(self.coach._history):
                        if h["speaker"] == "rep":
                            _last_rep = h["text"]
                            break
                opener = _quick_opener(text, self.current_stage, _last_rep)
                self.opener_shown = True
                self.coach.set_opener(opener)
                await self.send({"type": "call_guidance", "call_stage": self.current_stage, "opener": opener})

        if not is_final or self.coach is None:
            return

        # ── Detect new call starting mid-session ──
        # If the rep says a fresh greeting while we're deep in a call,
        # or we hear a voicemail greeting, warn the rep.
        if is_final and self.coach and len(self.coach._history) > 10:
            _t_newcall = text.lower().strip()
            _NEW_CALL_PHRASES = [
                "hi this is", "hello this is", "hey this is",
                "with cove", "with the security", "on a recorded line",
                "how are you doing today", "how are you today",
            ]
            _VOICEMAIL_PHRASES = [
                "forwarded to voice mail", "voicemail", "leave a message",
                "record your message", "at the tone", "not available",
                "mailbox is full",
            ]
            if speaker == "customer" and any(p in _t_newcall for p in _VOICEMAIL_PHRASES):
                print(f"[session] voicemail detected mid-session: {text[:60]}")
                await self.send({"type": "audio_warning",
                    "message": "Voicemail detected — this call may have ended. Click 'End Call' to save this session before starting a new one."})
            elif speaker == "rep" and any(p in _t_newcall for p in _NEW_CALL_PHRASES) and self.current_stage not in ("intro",):
                print(f"[session] new call detected mid-session: {text[:60]}")
                await self.send({"type": "audio_warning",
                    "message": "It sounds like a new call started. Click 'End Call' to save the previous session first."})

        # Filler filter (customer only)
        if speaker == "customer":
            stripped = text.strip()
            if len(stripped) < 2 or stripped.lower() in ("um", "uh", "hmm", "mm", "ah", "hm"):
                return

        # ── Customer final ──
        if speaker == "customer":
            self.coach.add_turn(speaker, text)
            # Send checklist immediately so topic detection from add_turn
            # shows up right away (don't wait for slow Claude response)
            await self.send_checklist()
            self.customer_buffer.append(text)
            if self._coach_task and not self._coach_task.done():
                self._coach_task.cancel()
            self._coach_task = asyncio.create_task(self._delayed_coaching())
            return

        # ── Rep final ──
        # Always add rep finals to coach history, even without speech_final.
        # speech_final only fires after 1.2s silence, so in fast conversation
        # many rep segments were being silently dropped.
        _topics_before = set(self.coach._topics_done)
        self.coach.add_turn(speaker, text)
        # If add_turn detected new topics (rep asked a question), update checklist immediately
        if self.coach._topics_done != _topics_before:
            await self.send_checklist()

        # ── Transition phrase detection ──
        # The ONLY way to auto-advance stages is the rep saying the specific
        # transition phrase. No keyword guessing, no auto-detect.
        # Check both the current chunk AND recent rep speech combined (Deepgram
        # often splits a sentence across 2-3 is_final chunks).
        if is_final and speaker == "rep":
            # Combine last few rep turns for phrase matching
            _recent_rep = []
            for _h in reversed(self.coach._history[-6:]):
                if _h["speaker"] == "rep":
                    _recent_rep.insert(0, _h["text"])
                else:
                    break
            _t_combined = " ".join(_recent_rep).lower()
            _t_trans = text.lower()
            _advanced = False

            # Discovery (or intro stuck) → collect_info
            _DISC_TO_INFO = [
                "grab some info", "get some info", "grab some information",
                "get some information", "grab your info", "get your info",
                "gonna grab some", "gonna get some info",
                "just going to grab", "just gonna grab",
                "grab info from you", "get info from you",
                "get some details from you", "grab some details from you",
            ]
            if self.current_stage in ("discovery", "intro") and (
                any(p in _t_trans for p in _DISC_TO_INFO) or
                any(p in _t_combined for p in _DISC_TO_INFO)
            ):
                print(f"[stage] transition phrase detected: discovery → collect_info: {_t_combined[:80]}")
                self.current_stage = "collect_info"
                _advanced = True

            # collect_info → build_system — require SPECIFIC scripted phrases only.
            # Generic phrases like "help you out" or "have coverage" are too loose.
            _INFO_TO_BUILD = [
                "fantastic coverage", "great coverage",
                "coverage out there",
                "definitely help you out", "can definitely help you out",
                "definitely take care of you",
                "dive right in", "let's build your system",
            ]
            if not _advanced and self.current_stage == "collect_info" and (
                any(p in _t_trans for p in _INFO_TO_BUILD) or
                any(p in _t_combined for p in _INFO_TO_BUILD)
            ):
                print(f"[stage] transition phrase detected: collect_info → build_system: {_t_combined[:80]}")
                self.current_stage = "build_system"
                self._build_current_item = "door_sensors"
                for ci_key in ("full_name", "phone_number", "email", "address"):
                    self.coach._topics_done.add(ci_key)
                    self._collect_info_done.add(ci_key)
                _advanced = True

            # build_system → closing
            _BUILD_TO_CLOSE = [
                "extra discount", "lot of discount",
                "lot of extra discount", "a lot of discounts",
                "get you some discounts", "get you a lot of discount",
                "able to get you a lot",
            ]
            if not _advanced and self.current_stage == "build_system" and (
                any(p in _t_trans for p in _BUILD_TO_CLOSE) or
                any(p in _t_combined for p in _BUILD_TO_CLOSE)
            ):
                print(f"[stage] transition phrase detected: build_system → closing: {_t_combined[:80]}")
                self.current_stage = "closing"
                _advanced = True

            if _advanced:
                transition = _stage_transition(self.current_stage)
                fallback = _fallback_next_step(self.current_stage, self.coach, session=self)
                if fallback and self.coach and self.coach.customer_name:
                    fallback = fallback.replace("[NAME]", self.coach.customer_name)
                guidance = {"type": "call_guidance", "call_stage": self.current_stage,
                            "next_step": fallback or ""}
                if transition:
                    guidance["opener"] = transition
                    self.coach.set_opener(transition)
                await self.send(guidance)
                await self.send_checklist()

        # ── Extract equipment quantities from rep speech ──
        # When the rep says "three door sensors" or "six window sensors", capture
        # the number into _equipment_counts so it shows correctly in the profile.
        if is_final:
            _t_eq = text.lower()
            _SPOKEN_NUMS_EQ = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                               "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
                               "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15}
            _EQUIP_QTY_PATTERNS = {
                "door_sensors": ["door sensor"],
                "window_sensors": ["window sensor"],
                "motion_sensor": ["motion sensor", "motion detect"],
                "co_detector": ["carbon monoxide", "co detector"],
                "glass_break": ["glass break"],
            }
            for eq_key, eq_phrases in _EQUIP_QTY_PATTERNS.items():
                # Skip motion sensor in camera descriptions
                if eq_key == "motion_sensor" and any(p in _t_eq for p in [
                    "built-in motion", "built in motion", "with a motion",
                    "camera", "night vision", "two-way audio", "two way audio",
                ]):
                    continue
                if any(p in _t_eq for p in eq_phrases):
                    # Look for a number near the equipment mention
                    import re as _re_eq
                    _eq_num = None
                    # Try digit numerals first
                    _digit_matches = _re_eq.findall(r'\d+', _t_eq)
                    if _digit_matches:
                        _eq_num = int(_digit_matches[0])
                    else:
                        # Try spoken numbers
                        for word in _t_eq.split():
                            if word in _SPOKEN_NUMS_EQ:
                                _eq_num = _SPOKEN_NUMS_EQ[word]
                                break
                    if _eq_num is not None and _eq_num > 0:
                        if eq_key not in self._equipment_counts or self._equipment_counts[eq_key] <= 1:
                            self._equipment_counts[eq_key] = _eq_num
                            print(f"[equipment] qty from rep speech: {eq_key} = {_eq_num}")
                            self._profile["equipment"] = self._build_equipment_list()
                            await self.send_profile()

        # ── Track pitch keywords in ANY stage so we can skip closing_pitch if already covered ──
        if is_final:
            _t_pitch = text.lower()
            _PITCH_KEYWORDS = {
                "no_contract": ["no contract", "month to month", "cancel anytime"],
                "wireless": ["wireless", "twenty minutes", "20 minutes", "set it up yourself"],
                "trial": ["sixty day", "60 day", "risk free", "risk-free", "full refund"],
            }
            for group, phrases in _PITCH_KEYWORDS.items():
                if any(p in _t_pitch for p in phrases):
                    self._pitch_keywords_said.add(group)

        # ── Auto-detect closing items from rep speech ──
        # Only update checklist — do NOT auto-advance or show next_step.
        # The rep reads the closing script at their own pace; pushing the
        # next item mid-speech was cutting them off (user feedback 2026-04-02).
        if self.current_stage == "closing" and is_final:
            t = text.lower()
            _closing_detected = []
            # closing_pitch is a LONG multi-part script (no contract + wireless +
            # 60-day trial). Require 2+ of the 3 groups before marking done so
            # we don't advance after just one phrase.
            _CLOSING_PITCH_GROUPS = {
                "no_contract": ["no contract", "month to month", "cancel anytime"],
                "wireless": ["wireless", "twenty minutes", "20 minutes", "set it up yourself"],
                "trial": ["sixty day", "60 day", "risk free", "risk-free", "full refund"],
            }
            for group, phrases in _CLOSING_PITCH_GROUPS.items():
                if any(p in t for p in phrases):
                    self._closing_pitch_groups_said.add(group)
            if len(self._closing_pitch_groups_said) >= 2:
                _closing_detected.append("closing_pitch")
            if any(w in t for w in ["29.99", "twenty nine", "32.99", "thirty two",
                                     "per month", "monthly monitoring", "total", "discounts",
                                     "promotions", "equipment cost", "hundred"]):
                _closing_detected.append("closing_pricing")
            if any(w in t for w in ["work for you", "sound good", "does that work", "gonna work"]):
                _closing_detected.append("closing_pricing")  # "work for you" is end of pricing
            if any(w in t for w in ["in your cart", "equipment in", "read it back",
                                     "all the equipment", "need me to repeat"]):
                _closing_detected.append("closing_cart")
            if any(w in t for w in ["payment info", "card info", "card number",
                                     "checkout", "place the order", "placed the order"]):
                _closing_detected.append("closing_checkout")
            if any(w in t for w in ["congratulations", "welcome to the cove", "welcome to cove",
                                     "tracking info", "package ships", "alarm certificate"]):
                _closing_detected.append("closing_welcome")
            if _closing_detected:
                for item in _closing_detected:
                    if item not in self._rep_overrides:
                        self.coach._topics_done.add(item)
                await self.send_checklist()
                # Show the NEXT closing prompt after a speech pause so the
                # rep knows what to say next (but don't push mid-sentence).
                if speech_final:
                    next_closing = _fallback_next_step("closing", self.coach, session=self)
                    if next_closing:
                        if self.coach.customer_name:
                            next_closing = next_closing.replace("[NAME]", self.coach.customer_name)
                        await self.send({"type": "call_guidance", "call_stage": "closing",
                                         "next_step": next_closing})

        if not speech_final:
            # In roleplay, buffer is_final text but DON'T trigger yet —
            # wait for speech_final (pause) so the AI doesn't cut
            # in while the rep is mid-sentence.
            if self.roleplay_mode and self.roleplay_customer and is_final:
                self.rep_buffer.append(text)
                # Safety: if speech_final never fires (Deepgram quirk),
                # schedule a fallback trigger after 2 seconds of no new is_final
                if self._roleplay_task and not self._roleplay_task.done():
                    self._roleplay_task.cancel()
                self._roleplay_task = asyncio.create_task(self._delayed_roleplay_response(delay=2.0))
            return

        if self.pending_evaluation:
            ev = self.pending_evaluation
            self.pending_evaluation = None
            result = await self.coach.evaluate_response(
                ev["objection_type"], ev["objection_summary"], text
            )
            if result and result.get("score") is not None:
                self.session_scores.append(result["score"])
                avg = round(sum(self.session_scores) / len(self.session_scores))
                await self.send({
                    "type": "score_update",
                    "score": result["score"],
                    "feedback": result.get("feedback", ""),
                    "breakdown": result.get("breakdown", {}),
                    "session_avg": avg,
                    "total_evaluated": len(self.session_scores),
                })

        if self.roleplay_mode and self.roleplay_customer:
            self.rep_buffer.append(text)
            if self._roleplay_task and not self._roleplay_task.done():
                self._roleplay_task.cancel()
            self._roleplay_task = asyncio.create_task(self._delayed_roleplay_response())


# ── WebSocket endpoint ────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    session = Session(ws)

    await session.send({"type": "status", "state": "idle"})

    _audio_chunk_count = 0
    _mic_chunk_count = 0
    _loopback_chunk_count = 0
    _audio_warning_sent = False
    try:
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                break

            # Binary frame: audio
            raw_bytes = message.get("bytes")
            if raw_bytes and len(raw_bytes) >= 2:
                _audio_chunk_count += 1
                label = raw_bytes[0]
                pcm = raw_bytes[1:]

                if label == 0x00:
                    _mic_chunk_count += 1
                elif label == 0x01:
                    _loopback_chunk_count += 1

                if _audio_chunk_count == 1:
                    print(f"[ws] first audio chunk, label=0x{label:02x}, len={len(raw_bytes)}, mic_queue={'set' if session.mic_queue else 'None'}")

                # After 200 chunks (~3 seconds), warn if only one stream is active
                if _audio_chunk_count == 200 and not _audio_warning_sent and not session.roleplay_mode:
                    _audio_warning_sent = True
                    if _mic_chunk_count > 0 and _loopback_chunk_count == 0:
                        print(f"[ws] WARNING: only mic audio received ({_mic_chunk_count} chunks, 0 customer). Customer audio not shared?")
                        await session.send({"type": "audio_warning", "message": "Customer audio not detected — make sure you checked 'Share tab audio' when sharing your screen."})
                    elif _loopback_chunk_count > 0 and _mic_chunk_count == 0:
                        print(f"[ws] WARNING: only customer audio received (0 mic, {_loopback_chunk_count} customer). Mic not working?")
                        await session.send({"type": "audio_warning", "message": "Microphone audio not detected — check your mic permissions."})
                    elif _mic_chunk_count == 0 and _loopback_chunk_count == 0:
                        print(f"[ws] WARNING: no audio received at all")
                        await session.send({"type": "audio_warning", "message": "No audio detected from either source. Check your mic and audio share."})
                    else:
                        print(f"[ws] audio streams healthy: mic={_mic_chunk_count}, customer={_loopback_chunk_count}")

                if label == 0x00 and session.mic_queue is not None:
                    # In roleplay, mute mic during TTS to prevent echo/mixing
                    # that garbles Deepgram transcription. Rep speech after TTS
                    # ends is captured cleanly.
                    if session.roleplay_mode and session.tts_active:
                        continue
                    session.mic_queue.put_nowait(pcm)
                elif label == 0x01 and session.loopback_queue is not None:
                    session.loopback_queue.put_nowait(pcm)
                continue

            # Text frame: JSON action
            if "text" in message and message["text"]:
                msg = json.loads(message["text"])
                action = msg.get("action")

                try:
                    if action == "start":
                        await session.start_live()
                    elif action == "start_roleplay":
                        await session.start_roleplay()
                    elif action == "opener_feedback":
                        opener_text = msg.get("opener", "")
                        rating = msg.get("rating", "")
                        if opener_text and rating in ("up", "down"):
                            session._opener_feedback.append({"opener": opener_text, "rating": rating})
                            print(f"[opener] {'👍' if rating == 'up' else '👎'} {opener_text[:50]}")
                    elif action == "tts_playing":
                        session.tts_active = msg.get("active", False)
                        if not session.tts_active:
                            print("[roleplay] TTS ended — mic unmuted")
                    elif action == "update_profile":
                        await session.update_profile_field(msg.get("field", ""), msg.get("value", ""))
                    elif action == "update_equipment_count":
                        key = msg.get("key", "")
                        qty = int(msg.get("qty", 0))
                        session._equipment_counts[key] = qty
                        session._equipment_edits.add(key)
                        print(f"[equipment] rep updated {key} = {qty}")
                        # Refresh profile + pricing after manual edit
                        session._profile["equipment"] = session._build_equipment_list()
                        await session.send_profile()
                        await session.send_pricing()
                    elif action == "add_equipment":
                        key = msg.get("key", "")
                        qty = int(msg.get("qty", 1))
                        # Add to equipment tracking so it shows in profile
                        _ADD_KEY_TO_EQUIP = {
                            "door_sensors": "door sensor", "window_sensors": "window sensor",
                            "motion_sensor": "motion sensor", "glass_break": "glass break",
                            "co_detector": "co detector", "smoke_detector": "smoke detector",
                            "indoor_camera": "camera", "outdoor_camera": "outdoor camera",
                            "doorbell_camera": "outdoor camera",
                            "panel_hub": "panel", "yard_sign": "yard sign",
                            "key_fob": "key fob", "flood_sensor": "flood sensor",
                            "medical_pendant": "medical pendant",
                        }
                        equip_name = _ADD_KEY_TO_EQUIP.get(key)
                        if equip_name and session.coach:
                            if equip_name not in session.coach._equipment_mentioned:
                                session.coach._equipment_mentioned.append(equip_name)
                        session._equipment_counts[key] = qty
                        session._equipment_edits.add(key)
                        print(f"[equipment] rep manually added {key} = {qty}")
                        session._profile["equipment"] = session._build_equipment_list()
                        await session.send_profile()
                        await session.send_pricing()
                    elif action == "apply_coupon":
                        await session.apply_coupon(msg.get("code", ""))
                    elif action == "remove_coupon":
                        await session.remove_coupon(msg.get("code", ""))
                    elif action == "set_plan":
                        plan = msg.get("plan", "plus")
                        if plan in ("plus", "basic"):
                            session._plan = plan
                            print(f"[plan] switched to Cove {plan.title()}")
                            await session.send_pricing()
                    elif action == "advance_stage":
                        new_stage = msg.get("stage", "")
                        if new_stage in _STAGE_ORDER:
                            # Enforce forward-only, single-step advancement
                            allowed = _allowed_stage_advance(session.current_stage, new_stage)
                            if not allowed:
                                print(f"[stage] BLOCKED backward/skip advance {session.current_stage} → {new_stage}")
                                continue  # don't change stage
                            session.current_stage = allowed
                            if new_stage == "build_system":
                                session._build_current_item = "door_sensors"
                            # recap is now part of build_system — no separate stage
                            # When entering closing, skip closing_pitch if the rep already
                            # covered most of those talking points during the call
                            if new_stage == "closing" and session.coach:
                                if len(session._pitch_keywords_said) >= 2:
                                    session.coach._topics_done.add("closing_pitch")
                                    print(f"[closing] auto-skipped closing_pitch — rep already covered: {session._pitch_keywords_said}")
                            print(f"[stage] rep advanced to: {new_stage}")
                            # Show the first suggestion for the new stage
                            fallback = _fallback_next_step(new_stage, session.coach, session=session)
                            if fallback and session.coach and session.coach.customer_name:
                                fallback = fallback.replace("[NAME]", session.coach.customer_name)
                            transition = _stage_transition(new_stage)
                            guidance = {"type": "call_guidance", "call_stage": new_stage,
                                        "next_step": fallback or ""}
                            if transition:
                                guidance["opener"] = transition
                                if session.coach:
                                    session.coach.set_opener(transition)
                            await session.send(guidance)
                            await session.send_checklist()
                    elif action == "go_back":
                        await session.go_back()
                    elif action == "toggle_topic":
                        await session.toggle_topic(msg.get("topic", ""), msg.get("checked", False))
                    elif action == "stop":
                        await session.stop()
                except Exception:
                    import traceback
                    print(f"[ws] action error ({action!r}):\n{traceback.format_exc()}")

    except WebSocketDisconnect:
        pass
    except Exception:
        import traceback
        print(f"[ws] fatal:\n{traceback.format_exc()}")
    finally:
        await session.stop()


# Prevent browser caching of JS/HTML so deploys take effect immediately
from starlette.middleware.base import BaseHTTPMiddleware

class NoCacheMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if request.url.path.endswith((".js", ".html", ".css")):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
        return response

app.add_middleware(NoCacheMiddleware)

# Serve frontend static files (must be after all route definitions)
app.mount("/", StaticFiles(directory=_FRONTEND_DIR), name="static")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8765))
    uvicorn.run(app, host="0.0.0.0", port=port)
