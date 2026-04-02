import asyncio
import base64
import json
import os
import sys
import random
import requests

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
_STAGE_ORDER = ["intro", "discovery", "collect_info", "build_system", "recap", "closing"]

@app.get("/")
async def serve_index():
    return FileResponse(os.path.join(_FRONTEND_DIR, "index.html"))


@app.post("/api/feedback")
async def post_feedback(request: Request):
    """Receive post-call feedback via REST (reliable even after WebSocket session ends).
    Finds the most recent transcript and attaches the feedback."""
    import json as _json
    from transcript_store import TRANSCRIPTS_DIR, _run_analysis_safe

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

    # Trigger analysis after feedback is attached
    if attached:
        asyncio.create_task(_run_analysis_safe())

    return {"ok": True, "attached": attached}


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
    return {"analyses": analyses, "transcripts": transcript_stats}


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

    # Fix capitalization
    if cleaned and cleaned[0].islower():
        cleaned = cleaned[0].upper() + cleaned[1:]

    return cleaned if cleaned else next_step


# Per-session opener tracking — NEVER repeat an opener within a session.
# The global list is reset when session starts. Tracks usage ORDER so when
# all options are exhausted, the least-recently-used one is picked instead
# of a random repeat.
_session_used_openers: list[str] = []

def _reset_opener_tracking():
    """Call when a new session starts."""
    _session_used_openers.clear()

def _pick(options: list[str]) -> str:
    available = [o for o in options if o not in _session_used_openers]
    if not available:
        # All used — pick the least-recently-used option (earliest in the list)
        for used in _session_used_openers:
            if used in options:
                choice = used
                # Move to end of list (mark as most recently used)
                _session_used_openers.remove(choice)
                _session_used_openers.append(choice)
                return choice
        # Shouldn't happen, but fallback
        available = options
    choice = random.choice(available)
    _session_used_openers.append(choice)
    return choice


def _quick_opener(text: str, current_stage: str, last_rep_text: str = "") -> str:
    t = text.lower().strip()
    _last_rep = last_rep_text.lower() if last_rep_text else ""

    # ── Context-aware: if rep just asked about website, short "no" = not on website ──
    if _last_rep and any(w in _last_rep for w in ["website", "covesmart", "cove smart", "the site"]):
        if t in ("no", "no sir", "no ma'am", "nope", "not yet", "no not yet", "no i'm not",
                 "no not right now", "not right now", "should i be", "no should i"):
            return "No problem! Could you go ahead and pull up covesmart.com so I can walk you through the process?"
        if any(w in t for w in ["yes", "yeah", "yep", "i'm on", "i am on", "pulled it up", "i have it"]):
            return _pick(["Awesome! I'll walk you through the whole thing.",
                           "Perfect! So you can see everything as we go through it."])

    # ── Context guard: during discovery, if the customer is describing a prior
    # system, don't let "monthly", "bill", "contract", "expensive" etc. trigger
    # billing/objection openers — they're talking about their OLD provider. ──
    _prior_system_context = current_stage in ("intro", "discovery") and any(
        w in t for w in ["i had", "i was with", "we had", "i used to", "i've had",
                         "previous", "old system", "last system", "before",
                         "liked about", "i liked", "didn't like", "the thing about",
                         "it was okay", "it was fine", "nothing special",
                         "was pretty good", "was decent", "pretty good but",
                         "were pretty good", "cameras were", "was too long",
                         "was too high"])

    # ── Emotional / situational triggers (highest priority) ──

    if any(w in t for w in ["break in", "broken into", "robbery", "robbed", "burglar", "stolen", "theft", "broke in"]):
        return _pick(["Oh no, I'm so sorry to hear that. I'll make sure we get you fully protected.",
                       "That's terrible — I'm really sorry you're dealing with that. Let's make sure that doesn't happen again.",
                       "I'm sorry that happened. We're gonna make sure you feel safe from here on out."])
    if any(w in t for w in ["scared", "nervous", "worried", "anxious", "afraid", "terrified", "freaked"]):
        return _pick(["I totally understand that feeling, and that's exactly why we're here to help.",
                       "That makes complete sense — your safety is our top priority and I'll take great care of you.",
                       "I hear you — and that's exactly why it's smart to get something in place now."])
    if any(w in t for w in ["baby", "newborn", "toddler", "infant", "pregnant", "expecting"]):
        return _pick(["Congratulations! Keeping the little one safe is exactly what we do.",
                       "That's so exciting — we'll make sure that baby is well protected.",
                       "That's wonderful! A security system is perfect timing with a little one on the way."])
    if any(w in t for w in [" kids", "children", "my son", "my daughter", "teenager"]):
        return _pick(["Keeping the family safe is what it's all about.",
                       "That's great — we've got some awesome features that'll be perfect for your family.",
                       "I love that. Protecting the kids is the number one reason people call us.",
                       "Family safety is huge — I'll make sure we get you set up right."])
    if any(w in t for w in ["moved", "new house", "just bought", "new home", "new place", "just purchased", "moving", "looking to move"]):
        return _pick(["Congrats on the new place! A new home is the perfect time to get set up.",
                       "That's exciting — let's make sure your new place is fully protected from day one.",
                       "Perfect timing! Getting security set up before you're all settled in is the smartest move.",
                       "That's great — a lot of our customers set up right when they move in."])
    if any(w in t for w in ["neighbor", "down the street", "next door", "in the area", "in my neighborhood"]):
        return _pick(["That's really unsettling when it's that close to home. I'll make sure we get you covered.",
                       "I don't blame you — that would make anyone want to take action.",
                       "That definitely hits different when it's right in your neighborhood."])
    if any(w in t for w in ["live alone", "by myself", "on my own", "here alone", "here by myself"]):
        return _pick(["Having that extra layer of protection makes a huge difference when you're on your own.",
                       "That's smart — peace of mind when you're on your own is so important.",
                       "I hear that a lot — and that's exactly the right reason to get set up."])
    if any(w in t for w in ["travel", "work nights", "gone a lot", "away from home", "long hours", "deployed",
                             "while i'm working", "while i work", "when i'm at work", "protect my family",
                             "out of town", "not home a lot"]):
        return _pick(["That makes a lot of sense — being able to keep an eye on things from anywhere is key.",
                       "I hear that all the time. We'll make sure you have peace of mind no matter where you are.",
                       "That's exactly why the smartphone access is huge — you'll see everything from your phone."])

    # ── Website response ──

    if any(w in t for w in ["not on the website", "not on the site", "should i be", "i'm not on",
                             "haven't pulled it up", "no i'm not", "not yet", "not on it",
                             "no sir", "no ma'am", "no i am not", "no not yet"]):
        return "No problem! Could you go ahead and pull up covesmart.com so I can walk you through the process?"
    if any(w in t for w in ["i'm on the website", "i'm on it", "i have it pulled up", "i have it up",
                             "yes i'm on", "yeah i'm on", "i got it pulled up", "i'm looking at it",
                             "i'm on the site", "i'm on cove"]):
        return _pick(["Awesome! I'll walk you through the whole thing.",
                       "Perfect! So you can see everything as we go through it."])

    # ── Competitor / switching triggers ──

    if any(w in t for w in ["vivint", "adt", "simplisafe", "ring", "alder", "brinks", "frontpoint"]):
        return _pick(["Good to know — I'll make sure we get you something that works even better.",
                       "I hear that a lot. A lot of folks are switching over and loving it.",
                       "That's helpful context — let me show you what makes Cove different.",
                       "No problem — we get a lot of people coming over from them."])
    if not _prior_system_context and any(w in t for w in ["too expensive", "paying too much", "overcharging", "cheaper", "better deal", "better price"]):
        return _pick(["I totally hear you on that — let me see what I can do to make this work for you.",
                       "I understand where you're coming from — let's find the right fit for your budget.",
                       "That's actually one of the biggest reasons people switch to Cove.",
                       "I hear you — nobody wants to overpay. Let me break down what we can do."])
    if not _prior_system_context and any(w in t for w in ["contract", "locked in", "stuck with", "cancel", "cancellation"]):
        return _pick(["Great news — we don't do contracts here, it's completely month to month.",
                       "You'll love this — no contracts with Cove, you can cancel anytime.",
                       "That's one of the best things about us — no contract, no commitment."])

    # ── Existing system / takeover triggers ──

    if any(w in t for w in ["already have", "already installed", "existing system", "previous owner", "came with the house"]):
        return _pick(["No problem at all — we can definitely work with what you already have there.",
                       "That's actually pretty common — a lot of our customers have existing equipment.",
                       "Good news — we can usually keep what's already installed and just get you a new account."])

    # ── Objection / hesitation triggers ──

    if not _prior_system_context and any(w in t for w in ["expensive", "too much", "cost", "afford", "pricey", "price", "budget", "how much"]):
        return _pick(["I totally hear you on that — let me see what I can do to make this work.",
                       "I understand where you're coming from. Let me break it down for you.",
                       "That's a fair concern — let me walk you through exactly what you're getting."])
    if any(w in t for w in ["talk to my", "ask my wife", "ask my husband", "spouse", "partner"]):
        return _pick(["Totally understandable — I'd want to check with my partner too.",
                       "No worries, I completely get that. Let me share a few things that might help the conversation."])
    if any(w in t for w in ["think about it", "call back", "not sure", "not ready", "shopping around", "comparing"]):
        return _pick(["No worries at all, I want you to feel good about it.",
                       "I understand — take your time. Let me give you all the info you need.",
                       "That's totally fair. A lot of people compare us to other companies and come back."])
    if any(w in t for w in ["install", "set it up", "how do i", "hard to install", "complicated", "technician", "professional"]):
        return _pick(["Great question — the good news is everything is wireless, so it's super easy.",
                       "No worries — it's all wireless and most people have it up in about 20 minutes.",
                       "That's one of the things people love — it's all DIY and really straightforward."])

    # ── Website / checkout process triggers ──

    if any(w in t for w in ["promo code", "promotion code", "coupon", "discount code"]):
        return _pick(["I've got you covered on the promo code — I'll walk you through it.",
                       "No worries, I'll get you the best code we have available right now.",
                       "Perfect — I'll make sure you get the best deal before you check out."])
    if any(w in t for w in ["won't let me", "error", "not working", "having trouble", "can't get it", "won't go through"]):
        return _pick(["No problem — that happens sometimes. Let me walk you through it step by step.",
                       "No worries at all, I'll help you get past that right now.",
                       "That's okay — let's troubleshoot this together real quick."])
    if any(w in t for w in ["payment", "debit card", "credit card", "card declined", "card information"]):
        return _pick(["No problem — we accept any major credit or debit card.",
                       "That's fine — just make sure it's a standard credit or debit card and we'll be good.",
                       "No worries — let me help you get that sorted out."])

    # ── Billing / monitoring questions ──

    if not _prior_system_context and any(w in t for w in ["monthly", "per month", "month to month", "every month", "autopay", "billing"]):
        return _pick(["Great question — let me explain exactly how the billing works.",
                       "I'll break that down for you — it's really straightforward.",
                       "That's a really common question — let me walk you through it."])
    if any(w in t for w in ["wifi", "wi-fi", "internet", "cellular", "power goes out", "no power"]):
        return _pick(["Great question — the panel actually runs on cellular so you're still protected even without wifi.",
                       "Love that question — the system stays connected through cellular backup.",
                       "That's one of the best features — it works even if your power or internet goes out."])
    if any(w in t for w in ["warranty", "break", "replacement", "defective", "stop working"]):
        return _pick(["Great news — everything comes with a lifetime warranty on the premium plan.",
                       "No worries on that — we've got lifetime warranty so we'll replace anything that goes bad."])

    # ── Shipping / timeline questions ──

    if any(w in t for w in ["shipping", "how long", "when will", "arrive", "delivery", "get here", "business days"]):
        return _pick(["Great question — shipping is usually 3 to 7 business days.",
                       "You'll get a tracking number as soon as it ships — usually arrives in about a week.",
                       "Most packages arrive within a week. You'll get tracking info right away."])

    # ── Stage-specific openers ──

    if current_stage in ("intro", "discovery"):
        if any(w in t for w in ["never had", "no i haven", "first time", "don't have one", "no system",
                                 "no never", "no this is", "nope", "this would be my first",
                                 "no not yet", "never before"]):
            return _pick(["No worries at all — I'll walk you through everything and make it super easy.",
                           "That's totally fine — I'll take great care of you step by step.",
                           "Perfect — you're in good hands, I do this all day."])
        if any(w in t for w in ["i had", "i was with", "i used to have", "we had", "i've had", "used to"]):
            return _pick(["Good to know — that experience will definitely help us get you set up right.",
                           "That's helpful. We'll make sure we match or beat what you had before.",
                           "Perfect — since you've been through this before, this should be a breeze."])
        if any(w in t for w in ["looking", "information", "find out", "curious", "wondering", "interested"]):
            return _pick(["Absolutely — I'll walk you through everything you need to know.",
                           "Of course! I'll make sure you have all the info to make the best decision.",
                           "Perfect — I'm happy to answer any questions you have."])

    if current_stage == "collect_info":
        # Check if customer just gave specific info types for a tailored response
        if any(w in t for w in ["@", "gmail", "yahoo", "hotmail", "aol", "outlook", ".com", "dot com",
                                 "at gmail", "at yahoo", "at hotmail", "at aol", "at outlook"]):
            return _pick(["Got it, I have your email.",
                           "Perfect, I've got that email down.",
                           "Alright, email is saved."])
        if any(c.isdigit() for c in t) and len([c for c in t if c.isdigit()]) >= 7:
            return _pick(["Got it, I have your number.",
                           "Perfect, I've got that number down.",
                           "Alright, phone number is saved."])
        if any(w in t for w in ["street", "drive", "avenue", "road", "lane", "boulevard", "way", "circle", "court"]):
            return _pick(["Got it, let me verify coverage in your area.",
                           "Perfect, let me check that we have coverage there.",
                           "Alright, let me make sure we can service that area."])
        return _pick(["Got it, thank you.",
                       "Perfect, I've got that down.",
                       "Alright, got it.",
                       "Thank you for that.",
                       "Got it, appreciate that."])

    if current_stage == "build_system":
        # Customer gives a number (doors, windows, etc.)
        if any(c.isdigit() for c in t) or any(w in t for w in ["one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"]):
            return _pick(["Perfect, I'll get those covered for you.",
                           "Got it — I'll make sure all of those are protected.",
                           "Alright, I'll get that taken care of.",
                           "Great — I'll get those added to your system.",
                           "Perfect — let me get that set up for you.",
                           "Awesome, I'll get those locked in."])
        if any(w in t for w in ["sounds good", "that works", "that makes sense", "yes", "yeah", "yep", "okay", "ok", "of course", "absolutely", "mhmm"]):
            return _pick(["Awesome — let me keep building this out for you.",
                           "Love it. Let me get you set up with the next piece.",
                           "Perfect, glad that makes sense. Moving right along.",
                           "Great, you're easy to work with — I love it.",
                           "Alright — let me show you the next piece.",
                           "Perfect — let me keep going here.",
                           "Great — on to the next one."])
        if any(w in t for w in ["what about", "do you have", "can i get", "i also need", "i want", "add"]):
            return _pick(["Absolutely — I can definitely add that for you.",
                           "Great thinking — let me get that added to your system.",
                           "For sure — I'll throw that in for you."])
        if any(w in t for w in ["don't need", "no thanks", "i'm good on", "skip", "don't want", "no i don't", "i hate"]):
            return _pick(["No problem at all — we'll skip that one.",
                           "Totally fine — I only want you to have what you actually need.",
                           "Got it — moving on."])
        if any(w in t for w in ["sliding door", "glass door", "patio door", "garage"]):
            return _pick(["Good call — that's an important entry point to cover.",
                           "Definitely want to get that covered — those are common entry points.",
                           "Smart — a lot of people forget about that one."])

    if current_stage in ("recap", "closing"):
        if any(w in t for w in ["sounds good", "that works", "let's do it", "yes", "yeah", "i'm ready", "let's go"]):
            return _pick(["Awesome! Let me see what I can do for you on the pricing.",
                           "Love it — let me get this wrapped up for you.",
                           "Perfect — I think you're gonna love this deal."])
        if any(w in t for w in ["no thank", "i'm good", "that's it", "nothing else", "that's all", "that should be good"]):
            return _pick(["Sounds good — I think we've got you fully covered.",
                           "No problem — personally I think we've got everything you need.",
                           "Perfect — I feel really good about this setup for you."])
        if any(w in t for w in ["order complete", "placed the order", "went through", "it worked"]):
            return _pick(["Congratulations and welcome to the Cove family!",
                           "That's awesome — welcome to Cove! Let me share a few quick things.",
                           "Perfect, I see it on my end — welcome to the Cove family!"])

    # ── Generic affirmatives (with more variety) ──

    if any(w in t for w in ["yes", "yeah", "yep", "that's right", "correct", "sure", "absolutely"]):
        return _pick(["Perfect, let me take care of that for you.",
                       "Awesome, I'll get that handled.",
                       "Great — moving right along.",
                       "Got it, no problem at all.",
                       "Sounds good, let me keep going."])

    if any(w in t for w in ["okay", "ok", "alright", "mhmm", "uh huh", "go ahead"]):
        return _pick(["Alright, here's what we'll do.",
                       "Perfect, let me walk you through this.",
                       "Sounds good — here's the next step."])

    # ── Questions from customer ──

    if any(w in t for w in ["what is", "what's", "how does", "how do", "can i", "can you", "do you", "is there", "does it", "will it"]):
        return _pick(["Great question — let me explain.",
                       "That's a really common question — here's how it works.",
                       "Good question! So here's the deal.",
                       "Of course — let me break that down for you."])

    # ── Fallback — contextual to stage ──

    if current_stage == "discovery":
        return _pick(["That's great to know — thank you for sharing that.",
                       "I appreciate that — it helps me understand what you're looking for.",
                       "Good to know — that's really helpful."])
    if current_stage == "build_system":
        return _pick(["Alright — let me keep building this out for you.",
                       "Got it — let me add the next piece to your system.",
                       "Perfect — let's keep going here."])
    if current_stage in ("recap", "closing"):
        return _pick(["Alright — let me pull everything together for you.",
                       "Perfect — let me get you the final numbers.",
                       "Sounds good — let me wrap this up."])

    return _pick(["Absolutely, I'll take care of you.",
                   "No problem at all — I've got you.",
                   "Perfect — let me help you out with that.",
                   "Of course — I'm happy to help."])


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
        "collect_info": "I'm just going to get some information from you before we start building out the system.",
        "build_system": "We do have fantastic coverage in your area, so I can definitely help you out. Let's go ahead and build your system.",
        "recap": "Let me quickly recap everything we've got for you.",
        "closing": "Awesome — let me see what I can do for you on the pricing.",
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
    # Remove filler prefixes first
    for filler in ["my email is ", "email is ", "the email is ", "it's ", "it is ",
                    "yeah it's ", "yes it's ", "yeah ", "yes ", "sure it's "]:
        if t.startswith(filler):
            t = t[len(filler):]
    # Fix common speech-to-text splits and stutters
    t = t.replace("g mail", "gmail").replace("hot mail", "hotmail")
    t = t.replace("out look", "outlook").replace("ya hoo", "yahoo")
    # Fix stutters: "g gmail" -> "gmail", "y yahoo" -> "yahoo"
    t = t.replace("g gmail", "gmail").replace("y yahoo", "yahoo")
    t = t.replace("h hotmail", "hotmail").replace("o outlook", "outlook")
    # Replace spoken email patterns
    t = t.replace(" at ", "@").replace(" dot ", ".")
    # Remove remaining spaces (email has no spaces)
    result = t.replace(" ", "")
    # Final dedup: "ggmail" -> "gmail", "yyahoo" -> "yahoo"
    for domain in ["gmail", "yahoo", "hotmail", "outlook", "aol"]:
        while domain[0] + domain in result:
            result = result.replace(domain[0] + domain, domain)
    return result if "@" in result else text.strip()


def _extract_name(text: str) -> str:
    """Extract just the name from customer speech like 'my name is joe bonnie'
    or 'yeah my first name is joe and my last name is bonnie'."""
    t = text.lower().strip()

    # Try to extract after "my name is" / "name is" / "it's" / "i'm"
    for prefix in ["my name is ", "my first name is ", "first name is ", "name is ",
                    "it's ", "i'm ", "this is "]:
        if prefix in t:
            after = text[t.index(prefix) + len(prefix):].strip()
            # Remove filler words between first and last name
            for filler in [" and my last name is ", " my last name is ", " last name is ",
                           " and my last name ", " my last name ", " last name "]:
                after = after.replace(filler, " ")
            # Take the name words (stop at non-name words)
            _STOP_WORDS = {"and", "my", "the", "so", "but", "yeah", "yes", "that", "is",
                           "it", "i", "we", "at", "on", "in", "from", "with"}
            words = []
            for w in after.split():
                clean = w.strip(".,!?")
                if clean.lower() in _STOP_WORDS and len(words) >= 2:
                    break
                if clean.isalpha() and len(clean) >= 2:
                    words.append(clean.capitalize())
                if len(words) >= 3:  # max 3 name parts
                    break
            if words:
                return " ".join(words)

    # Fallback: if short text (≤4 words), might just be the name
    parts = text.strip().split()
    if len(parts) <= 4:
        words = [w.capitalize() for w in parts if w.isalpha() and len(w) >= 2
                 and w.lower() not in {"my", "is", "the", "yeah", "yes", "it", "its"}]
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
    "closing_pitch": ("It looks like I'm going to be able to get you a lot of extra discounts here. "
                      "First, here at Cove we have no contracts — it's completely month to month, and we have some of the best customer service in the industry. "
                      "We don't charge anything for installation because everything is wireless — we'll send all the equipment straight to you and you can set it up yourself in about 20 minutes. "
                      "We also have a 60-day risk-free trial, so you can try everything out and if it's not the right fit, you can return it for a full refund."),
    "closing_pricing": ("On the monthly monitoring, for the first six months it'll just be $29.99 per month. "
                        "After that, it goes to the standard rate of $32.99. "
                        "And the equipment — with all the discounts and promotions today, I'm gonna get your total down to a great price."),
    "closing_commitment": "Does that sound like it will work for you?",
    "closing_checkout": "Go ahead and put your payment info in. Let me know once you've placed the order and I'll confirm on my side.",
    "closing_welcome": ("Congratulations and welcome to the Cove family! "
                        "You'll get tracking info as soon as your package ships — usually 3 to 7 business days. "
                        "If you need a technician, we have a third-party service starting at $129. "
                        "And if you have home insurance, request an alarm certificate from us for a discount. "
                        "Is there anything else I can help you with?"),
}

# Ordered checklist keys per stage — defines the sequence items should be covered
_STAGE_ITEM_ORDER = {
    "discovery": ["existing_customer", "had_system_before", "why_security", "who_protecting", "kids_age", "on_website"],
    "collect_info": ["full_name", "phone_number", "email", "address"],
    "build_system": ["door_sensors", "window_sensors", "extra_equip", "indoor_camera", "outdoor_camera", "panel_hub", "yard_sign"],
    "recap": ["recap_done"],
    "closing": ["closing_pitch", "closing_pricing", "closing_commitment", "closing_checkout", "closing_welcome"],
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
    if counts.get("indoor_camera", 0) > 0:
        parts.append("a free indoor camera")
    if counts.get("outdoor_camera", 0) > 0:
        parts.append("an outdoor camera")
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
        self._build_current_item: str | None = None  # which build_system item is being pitched next
        self._equipment_counts: dict = {}  # e.g. {"door_sensors": 2, "window_sensors": 5}
        self._user_feedback: str = ""  # post-call feedback from rep

    async def send(self, msg: dict):
        try:
            await self.ws.send_text(json.dumps(msg))
        except Exception:
            pass

    # ── Session lifecycle ──

    async def start_live(self):
        if self.running:
            return
        _reset_opener_tracking()
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
        _reset_opener_tracking()
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
            asyncio.create_task(self._tts_safety_reset(5))
        await self.send({"type": "roleplay_speech", "text": opening, "audio_b64": audio_b64})
        await self._on_transcript("customer", opening, True, True)

    async def stop(self):
        self.tts_active = False
        was_running = self.running
        was_roleplay = self.roleplay_mode

        if not self.running:
            self.current_stage = "intro"
            self.opener_shown = False
            self.intro_turns = 0
            self._collect_info_done = set()
            self._rep_overrides = set()
            self._build_current_item = None
            self._equipment_counts = {}
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
                    user_feedback="",
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
        "recap_done": "recap", "anything_else": "recap",
        "closing_pitch": "closing", "closing_pricing": "closing",
        "closing_commitment": "closing", "closing_checkout": "closing",
        "closing_welcome": "closing",
    }

    async def send_checklist(self):
        """Broadcast current checklist state to the frontend.
        Only sends items for the current stage and completed stages.
        Respects rep overrides."""
        if not self.coach:
            return

        # Determine which stages are allowed (current + all before it)
        cur_idx = _STAGE_ORDER.index(self.current_stage) if self.current_stage in _STAGE_ORDER else 0
        allowed_stages = set(_STAGE_ORDER[:cur_idx + 1])

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

        await self.send({"type": "checklist_update", "topics": topics})

    async def send_profile(self):
        """Broadcast current customer profile to the frontend."""
        await self.send({"type": "profile_update", **self._profile})

    async def update_profile_field(self, field: str, value: str):
        """Rep edited a profile field."""
        if field in self._profile:
            self._profile[field] = value
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
            }
            info = _MAP.get(e)
            if info:
                qty = self._equipment_counts.get(info["key"], 1)
                items.append({"key": info["key"], "label": info["label"], "qty": qty})
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
                                "collect_info": "build_system", "build_system": "recap",
                                "recap": "closing"}
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
        # Use the master prompt table — single source of truth
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
                                 "opener": "Let me get that again.", "next_step": prompt})
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
                                     "opener": "Let me go back to that.", "next_step": fallback})
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
                                         "opener": "Let me go back.", "next_step": fallback})
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

            # Always include opener so frontend never shows a Then without a Say First
            opener_used = self.coach._last_opener if self.coach else ""
            guidance_msg = {"type": "call_guidance", "call_stage": new_stage, "next_step": cleaned_next}
            if opener_used:
                guidance_msg["opener"] = opener_used
            await self.send(guidance_msg)
            await self.send_checklist()
            # Update profile equipment list
            self._profile["equipment"] = self._build_equipment_list()
            await self.send_profile()

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
                asyncio.create_task(self._tts_safety_reset(5))
            await self.send({"type": "roleplay_speech", "text": ai_text, "audio_b64": audio_b64})
            await self._on_transcript("customer", ai_text, True, True)
        except asyncio.CancelledError:
            raise
        except Exception:
            import traceback
            print(f"[roleplay] send/tts error:\n{traceback.format_exc()}")
            self.tts_active = False

    async def _delayed_roleplay_response(self):
        try:
            await asyncio.sleep(0.6)
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
        if self.roleplay_mode and self.tts_active and speaker == "rep":
            await self.send({"type": "transcript", "speaker": speaker, "text": text, "is_final": is_final})
            return

        await self.send({"type": "transcript", "speaker": speaker, "text": text, "is_final": is_final})

        # ── Fast-track intro ──
        if speaker == "customer" and is_final and self.coach is not None and self.current_stage == "intro" and self.intro_turns < 2:
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
                await self.send({"type": "call_guidance", "call_stage": "discovery",
                    "next_step": "Perfect, well I'll be the one to walk you through the process and help you get set up. Have you ever had a security system before?"})
                await self.send_checklist()
                return
            # Customer said something else (greeting, question, etc.) —
            # stay in intro. Don't consume intro_turns for non-answers
            # so rapid greetings don't eat up the counter.
            # Still show checklist + suggestion so the UI isn't blank.
            self.intro_turns = max(self.intro_turns - 1, 0)
            await self.send({"type": "call_guidance", "call_stage": "intro",
                "next_step": "Are you already a Cove customer, or are you looking to get a security system?"})
            await self.send_checklist()
            return

        # ── Fast-track collect_info ──
        # During collect_info, responses come rapid-fire (name, phone, email, address).
        # Claude coaching gets cancelled before completing, so use instant hardcoded suggestions.
        # IMPORTANT: Validate what the customer actually said before advancing.
        if speaker == "customer" and is_final and self.coach is not None and self.current_stage == "collect_info":
            self.coach.add_turn(speaker, text)
            self.customer_buffer = []  # clear so _fire_coaching doesn't also fire
            if self._coach_task and not self._coach_task.done():
                self._coach_task.cancel()
            t = text.lower()

            # Detect what TYPE of info the customer just gave
            # Count digits — both actual digits AND spoken number words
            _SPOKEN_DIGITS = {"zero", "one", "two", "three", "four", "five",
                              "six", "seven", "eight", "nine", "oh"}
            _digit_count = sum(c.isdigit() for c in t)
            _digit_count += sum(1 for w in t.split() if w in _SPOKEN_DIGITS)

            _NOT_NAMES = {"yes", "no", "yeah", "yep", "nope", "okay", "ok", "sure", "hello", "hi",
                         "yes sir", "no sir", "yes ma'am", "no ma'am", "little kids", "teenagers",
                         "my family", "my kids", "my wife", "my husband", "just me", "me and my",
                         "not yet", "not sure", "i think", "let me", "give me", "hold on",
                         "sounds good", "that works", "thank you", "thanks", "alright", "fine"}
            _is_short_alpha = len(t.split()) <= 4 and t.replace(" ", "").isalpha()
            _has_name_words = any(w in t for w in ["my name is", "first name", "last name"]) or (
                _is_short_alpha and t.strip() not in _NOT_NAMES and not any(phrase in t for phrase in _NOT_NAMES)
            )
            _has_phone_digits = _digit_count >= 7
            _has_email = "@" in t or any(w in t for w in ["gmail", "yahoo", "hotmail", "aol", "outlook",
                                                           "dot com", "at gmail", "at yahoo", "at hotmail",
                                                           "at aol", "at outlook"])
            _has_address = any(w in t for w in ["street", "drive", "avenue", "road", "lane", "boulevard",
                                                 "way", "circle", "court", "north", "south", "east", "west"]) or _digit_count >= 4

            # Only advance if the customer gave the expected info type
            opener = _quick_opener(text, "collect_info")
            self.coach.set_opener(opener)
            next_step = None

            if "full_name" not in self._collect_info_done:
                if _has_name_words or (not _has_phone_digits and not _has_email and not _has_address):
                    self._collect_info_done.add("full_name")
                    self._profile["name"] = _extract_name(text)
                    next_step = "And what's your best phone number?"
            elif "phone_number" not in self._collect_info_done:
                if _has_phone_digits:
                    self._collect_info_done.add("phone_number")
                    # Extract digits from both numerals and spoken words
                    digits = _spoken_to_digits(text)
                    if not digits:
                        digits = "".join(c for c in text if c.isdigit())
                    if len(digits) == 10:
                        self._profile["phone"] = f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
                    elif len(digits) >= 7:
                        self._profile["phone"] = digits
                    else:
                        self._profile["phone"] = text.strip()
                    next_step = "And your email so I can send all this information over to you by the end of the call?"
            elif "email" not in self._collect_info_done:
                if _has_email:
                    self._collect_info_done.add("email")
                    self._profile["email"] = _extract_email(text)
                    next_step = "And before we get ahead of ourselves, I just want to verify we have coverage. What's the address you're looking to get the security set up at?"
            elif "address" not in self._collect_info_done:
                if _has_address:
                    self._collect_info_done.add("address")
                    # Clean up address — remove filler, convert spoken numbers
                    addr = text.strip()
                    for filler in ["the address is ", "address is ", "it is ", "it's ",
                                   "this is ", "we're at ", "i'm at ", "yeah it's ",
                                   "yeah ", "yes "]:
                        if addr.lower().startswith(filler):
                            addr = addr[len(filler):]
                    self._profile["address"] = _spoken_numbers_to_numerals(addr.strip())
                    # Don't auto-advance to build_system — rep must click
                    # "INFO COMPLETE" to advance. Just show coverage confirmation.
                    opener = _pick(["Awesome, we have fantastic coverage in your area.",
                                    "Great news — we have great coverage out there.",
                                    "Perfect, we can definitely service that area."])
                    self.coach.set_opener(opener)
                    next_step = "We have great coverage in your area, so I can definitely help you out. Go ahead and click INFO COMPLETE when you're ready to build the system."
                    self.coach._topics_done.add("full_name")
                    self.coach._topics_done.add("phone_number")
                    self.coach._topics_done.add("email")
                    self.coach._topics_done.add("address")

            if next_step:
                await self.send({"type": "call_guidance", "call_stage": self.current_stage, "opener": opener, "next_step": next_step})
                await self.send_checklist()
                await self.send_profile()
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
        if speaker == "customer" and is_final and self.coach is not None and self.current_stage == "build_system":
            self.coach.add_turn(speaker, text)
            self.customer_buffer = []
            if self._coach_task and not self._coach_task.done():
                self._coach_task.cancel()

            t = text.lower()
            words = t.split()

            # ── Parse number from customer speech ──
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

            elif _cur == "extra_equip" and (_is_yes or _is_no):
                self.coach._topics_done.add("extra_equip")
                if "motion sensor" not in self.coach._equipment_mentioned:
                    self.coach._equipment_mentioned.append("motion sensor")
                self._equipment_counts["motion_sensor"] = 1 if _is_yes else 0
                self._build_current_item = "indoor_camera"
                next_step = _fallback_next_step("build_system", self.coach, session=self)
                build_handled = True

            elif _cur in ("indoor_camera", "outdoor_camera", "panel_hub", "yard_sign") and (_is_yes or _is_no):
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
                    next_step = _fallback_next_step("recap", self.coach, session=self)
                if next_step and name:
                    next_step = next_step.replace("[NAME]", name)
                if next_step:
                    next_step = _trim_long_suggestion(next_step)
                await self.send({"type": "call_guidance", "call_stage": self.current_stage,
                                 "opener": _quick_opener(text, "build_system") if not build_handled else opener,
                                 "next_step": next_step or ""})
                self._profile["equipment"] = self._build_equipment_list()
                await self.send_checklist()
                await self.send_profile()
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
        _skip_opener = self.current_stage in ("intro",)
        if speaker == "customer" and not self.opener_shown and self.coach is not None and not _skip_opener:
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
        self.coach.add_turn(speaker, text)

        # ── Auto-detect closing items from rep speech ──
        if self.current_stage == "closing" and is_final:
            t = text.lower()
            _closing_detected = []
            if any(w in t for w in ["no contract", "month to month", "cancel anytime", "wireless",
                                     "twenty minutes", "20 minutes", "sixty day", "60 day",
                                     "risk free", "risk-free", "full refund"]):
                _closing_detected.append("closing_pitch")
            if any(w in t for w in ["29.99", "twenty nine", "32.99", "thirty two",
                                     "per month", "monthly monitoring", "total", "discounts",
                                     "promotions", "equipment cost", "hundred"]):
                _closing_detected.append("closing_pricing")
            if any(w in t for w in ["work for you", "sound good", "does that work", "gonna work"]):
                _closing_detected.append("closing_commitment")
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
                # Auto-advance: show the next closing item immediately
                # so the rep can continue the monologue without waiting
                # for a customer response
                if speech_final:
                    next_closing = _fallback_next_step("closing", self.coach, session=self)
                    if next_closing:
                        if self.coach.customer_name:
                            next_closing = next_closing.replace("[NAME]", self.coach.customer_name)
                        await self.send({"type": "call_guidance", "call_stage": "closing",
                                         "next_step": next_closing})

        if not speech_final:
            # In roleplay, buffer is_final text but DON'T trigger yet —
            # wait for speech_final (600ms pause) so the AI doesn't cut
            # in while the rep is mid-sentence.
            if self.roleplay_mode and self.roleplay_customer and is_final:
                self.rep_buffer.append(text)
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
    try:
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                break

            # Binary frame: audio
            raw_bytes = message.get("bytes")
            if raw_bytes and len(raw_bytes) >= 2:
                _audio_chunk_count += 1
                if _audio_chunk_count == 1:
                    print(f"[ws] first audio chunk, len={len(raw_bytes)}, mic_queue={'set' if session.mic_queue else 'None'}")
                label = raw_bytes[0]
                pcm = raw_bytes[1:]
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
                        print(f"[equipment] rep updated {key} = {qty}")
                    elif action == "advance_stage":
                        new_stage = msg.get("stage", "")
                        if new_stage in _STAGE_ORDER:
                            session.current_stage = new_stage
                            if new_stage == "build_system":
                                session._build_current_item = "door_sensors"
                            print(f"[stage] rep advanced to: {new_stage}")
                            # Show the first suggestion for the new stage
                            fallback = _fallback_next_step(new_stage, session.coach, session=session)
                            if fallback and session.coach and session.coach.customer_name:
                                fallback = fallback.replace("[NAME]", session.coach.customer_name)
                            transition = _stage_transition(new_stage)
                            await session.send({"type": "call_guidance", "call_stage": new_stage,
                                                "opener": transition, "next_step": fallback or ""})
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
            session._pending_transcript = None


# Serve frontend static files (must be after all route definitions)
app.mount("/", StaticFiles(directory=_FRONTEND_DIR), name="static")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8765))
    uvicorn.run(app, host="0.0.0.0", port=port)
