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
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
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

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
DEEPGRAM_API_KEY = os.environ["DEEPGRAM_API_KEY"]

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")
_STAGE_ORDER = ["intro", "discovery", "collect_info", "build_system", "recap", "closing"]

@app.get("/")
async def serve_index():
    return FileResponse(os.path.join(_FRONTEND_DIR, "index.html"))


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
# The global list is reset when session starts. Within a session, once an
# opener is used it's gone forever.
_session_used_openers: set[str] = set()

def _reset_opener_tracking():
    """Call when a new session starts."""
    global _last_fallback
    _session_used_openers.clear()
    _last_fallback = ""

def _pick(options: list[str]) -> str:
    available = [o for o in options if o not in _session_used_openers]
    if not available:
        # All used in this session — pick least recently by falling back,
        # but this should be rare given the large pool
        available = options
    choice = random.choice(available)
    _session_used_openers.add(choice)
    return choice


def _quick_opener(text: str, current_stage: str) -> str:
    t = text.lower().strip()

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
        if any(w in t for w in ["never had", "no i haven", "first time", "don't have one", "no system"]):
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
        return _pick(["Thank you for sharing that — that really helps me understand what you need.",
                       "I appreciate you telling me that — it helps me build the right system for you.",
                       "Good to know — I'll keep that in mind as we build your system."])
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


_last_fallback: str = ""  # prevent the same fallback from firing twice in a row


def _customer_mentioned_kids(coach) -> bool:
    """Check if customer mentioned kids but hasn't specified ages yet."""
    if not coach or not coach._customer_facts:
        return False
    all_text = " ".join(coach._customer_facts).lower()
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


def _personalize_panel(ctx: dict, name: str) -> str:
    """Generate a personalized panel/hub description."""
    suffix = f", {name}" if name else ""
    base = "I'm also going to get you the hub — that's the brain of the system that connects everything. It runs on cellular, so even if your power or Wi-Fi goes down, your home is still protected 24/7 with police, medical, and fire support. And you'll get a 7-inch color touchscreen panel to navigate everything."
    if ctx.get("kids"):
        return f"{base} With kids in the house, knowing you've got 24/7 backup even during a power outage is huge. Does that make sense{suffix}?"
    if ctx.get("break_in"):
        return f"{base} Given what happened in your neighborhood, having that cellular backup means your home is always protected — even if someone cuts your power or internet. Does that make sense{suffix}?"
    return f"{base} Does that make sense{suffix}?"

def _fallback_next_step(stage: str, coach) -> str:
    """Generate a stage-appropriate fallback when Claude returns no next_step.
    This prevents the rep from seeing an opener bubble with no follow-up.
    IMPORTANT: marks topics/equipment as done so the fallback never re-asks."""
    global _last_fallback
    if not coach:
        return ""

    done = coach._topics_done
    result = ""

    if stage == "discovery":
        if "why_security" not in done:
            done.add("why_security")
            result = "What has you looking into security? Did something happen, or did you just decide it was time?"
        elif "had_system_before" not in done:
            done.add("had_system_before")
            result = "Have you ever had a security system before?"
        elif "who_protecting" not in done:
            done.add("who_protecting")
            result = "Who are we looking to protect — is it just you or is there anyone else living there with you?"
        elif "kids_age" not in done and _customer_mentioned_kids(coach):
            done.add("kids_age")
            result = "Are we talking about little kids or teenagers?"
        elif "on_website" not in done:
            done.add("on_website")
            result = "Are you currently on the Cove website? Go ahead and pull up covesmart.com whenever you're ready — I'll walk you through the whole thing."
        elif "_discovery_bridge" not in done:
            # All discovery done → bridge to collect_info with proper transition
            done.add("_discovery_bridge")
            done.add("full_name")
            result = "I'm just going to get some information from you before we start building out the system. Could you please spell your first and last name for me?"

    elif stage == "collect_info":
        if "full_name" not in done:
            done.add("full_name")
            result = "Could you please spell your first and last name for me?"
        elif "phone_number" not in done:
            done.add("phone_number")
            result = "And what's your best phone number?"
        elif "email" not in done:
            done.add("email")
            result = "And your email so I can send all this information over to you?"
        elif "address" not in done:
            done.add("address")
            result = "What's the address you're looking to get the security set up at?"
        elif "_collect_bridge" not in done:
            done.add("_collect_bridge")
            result = "Let's go ahead and build your system. How many doors go in and out of your home?"

    elif stage == "build_system":
        equip = coach._equipment_mentioned
        name = coach.customer_name or ""
        ctx = _get_discovery_context(coach)
        if "door sensor" not in equip:
            equip.append("door sensor")
            result = "How many doors go in and out of your home?"
        elif "window sensor" not in equip:
            equip.append("window sensor")
            result = "How many windows are on the ground floor of your house that are accessible?"
        elif "chime" not in equip:
            equip.append("chime")
            result = _personalize_chime(ctx, name)
        elif "camera" not in equip:
            equip.append("camera")
            result = _personalize_camera(ctx, name)
        elif "panel" not in equip:
            equip.append("panel")
            result = _personalize_panel(ctx, name)
        elif "yard sign" not in equip:
            equip.append("yard sign")
            equip.append("smartphone")
            result = "I'm also going to throw in a free yard sign and window stickers — that way everyone knows you have security in place. Plus you'll have full smartphone access so you can arm and disarm the system, view cameras, and control everything from your phone no matter where you are."
        elif "_build_recap" not in done:
            done.add("_build_recap")
            suffix = f", {name}" if name else ""
            result = f"Is there anything else you'd like to add to your system{suffix}?"

    elif stage == "recap":
        name = coach.customer_name or ""
        suffix = f", {name}" if name else ""
        if "_recap_ask" not in done:
            done.add("_recap_ask")
            result = f"Is there anything else you were hoping I could add{suffix}?"

    elif stage == "closing":
        name = coach.customer_name or ""
        suffix = f", {name}" if name else ""
        # Closing is a monologue — first 5 items flow together, then ask for commitment
        if "no_contract" not in done:
            done.add("no_contract")
            done.add("wireless_install")  # bundle with no_contract — rep delivers together
            result = "It looks like I'm going to be able to get you a lot of extra discounts here. First, here at Cove we have no contracts — it's completely month to month. And we don't charge anything for installation because everything is wireless. We'll send all the equipment straight to you and you can set it up yourself in about 20 minutes."
        elif "trial_60" not in done:
            done.add("trial_60")
            result = "We also have a 60-day risk-free trial — so you can try everything out, and if it's not the right fit, you can return it for a full refund within 60 days."
        elif "monthly_price" not in done:
            done.add("monthly_price")
            done.add("equip_total")  # bundle pricing together
            result = "On the monthly monitoring, for the first six months it'll just be $29.99 per month. After that, it goes to the standard rate of $32.99. And the equipment — with all the discounts and promotions today, I'm gonna get your total down to a great price."
        elif "ask_commitment" not in done:
            done.add("ask_commitment")
            result = f"Does that sound like it will work for you{suffix}?"
        elif "guide_checkout" not in done:
            done.add("guide_checkout")
            result = f"Go ahead and put your payment info in{suffix}. Let me know once you've placed the order and I'll confirm on my side."
        elif "order_confirmed" not in done:
            done.add("order_confirmed")
            result = f"Congratulations and welcome to the Cove family{suffix}! You'll get tracking info as soon as your package ships — usually 3 to 7 business days. If you need a technician, we have a third-party service starting at $129. And if you have home insurance, request an alarm certificate from us for a discount. Is there anything else I can help you with?"

    # Prevent exact same fallback from firing twice in a row
    if result and result == _last_fallback:
        return ""
    _last_fallback = result
    return result


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

        opening = await self.roleplay_customer.opening_line()
        audio_b64 = await _tts(opening, self.roleplay_customer.voice)
        if audio_b64:
            self.tts_active = True
            asyncio.create_task(self._tts_safety_reset(5))
        await self.send({"type": "roleplay_speech", "text": opening, "audio_b64": audio_b64})
        await self._on_transcript("customer", opening, True, True)

    async def stop(self):
        self.tts_active = False
        self.current_stage = "intro"
        self.opener_shown = False
        self.intro_turns = 0
        self._collect_info_done = set()
        self._rep_overrides = set()

        if not self.running:
            return

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
        "why_security": "why_security",
        "had_system_before": "had_system_before",
        "who_protecting": "who_protecting",
        "on_website": "on_website",
        "prior_provider": "had_system_before",  # counts as part of "had system"
        "kids_age": "who_protecting",  # counts as part of "who protecting"
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

    async def send_checklist(self):
        """Broadcast current checklist state to the frontend.
        Respects rep overrides — if the rep unchecked an item, don't re-check it
        until the rep explicitly checks it again."""
        if not self.coach:
            return
        topics = {}
        # Discovery + collect_info topics
        for internal_key, checklist_key in self._TOPIC_TO_CHECKLIST.items():
            if checklist_key in self._rep_overrides:
                continue  # rep said this isn't done — respect that
            if internal_key in self.coach._topics_done or internal_key in self._collect_info_done:
                topics[checklist_key] = True
        # Build system equipment
        for internal_key, checklist_key in self._EQUIP_TO_CHECKLIST.items():
            if checklist_key in self._rep_overrides:
                continue  # rep said this isn't done — respect that
            if internal_key in self.coach._equipment_mentioned:
                topics[checklist_key] = True
        await self.send({"type": "checklist_update", "topics": topics})

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
            else:
                if internal in self.coach._equipment_mentioned:
                    self.coach._equipment_mentioned.remove(internal)
                self.coach._topics_done.discard("_build_recap")
            print(f"[checklist] rep {'checked' if checked else 'unchecked'} equip: {topic} -> {internal}")
        else:
            # Closing items or custom keys — just track in topics_done
            if checked:
                self.coach._topics_done.add(topic)
            else:
                self.coach._topics_done.discard(topic)
            print(f"[checklist] rep {'checked' if checked else 'unchecked'} custom: {topic}")

        # If checking, just silently update the checklist — no guidance change
        if checked:
            await self.send_checklist()
            return

        # If unchecking, show the prompt for the unchecked item directly
            # Find which stage this topic belongs to and get its prompt
            prompt = self._COLLECT_INFO_PROMPTS.get(topic, "")
            stage_for_item = self.current_stage

            # Discovery items
            _DISCOVERY_PROMPTS = {
                "why_security": "What has you looking into security? Did something happen, or did you just decide it was time?",
                "had_system_before": "Have you ever had a security system before?",
                "who_protecting": "Who are we looking to protect — is it just you or is there anyone else living there with you?",
                "on_website": "Are you currently on the Cove website? Go ahead and pull up covesmart.com whenever you're ready.",
            }
            # Build system items
            _BUILD_PROMPTS = {
                "door_sensors": "How many doors go in and out of your home?",
                "window_sensors": "How many windows are on the ground floor of your house that are accessible?",
                "extra_equip": "We also have a motion detector, glass break detector, and carbon monoxide detector. Do you think you'd need any of those?",
                "indoor_camera": "I'm also going to give you a free indoor camera — it's live HD with recording, night vision, two-way audio, and a built-in motion sensor. Does that make sense?",
                "outdoor_camera": "We also have a doorbell camera and a solar-powered outdoor camera. The outdoor camera is 50% off right now. Would you like to add either of those?",
                "panel_hub": "I'm also going to get you the hub and a 7-inch touchscreen panel — it runs on cellular, so even if your power or Wi-Fi goes down, you're still protected 24/7.",
                "yard_sign": "I'm also going to throw in a free yard sign and window stickers — plus you'll have full smartphone access to control everything from your phone.",
            }
            # Closing items — sequential flow
            _CLOSING_PROMPTS = {
                "no_contract": "Here at Cove we have no contracts — it's completely month to month, and we have some of the best customer service in the industry.",
                "wireless_install": "We don't charge anything for installation because everything is wireless. We'll send all the equipment straight to you and you can set it up yourself in about 20 minutes. If you need help, our tech support team will walk you through it over the phone.",
                "trial_60": "We also have a 60-day risk-free trial — so you can try everything out, and if it's not the right fit, you can return it for a full refund within 60 days.",
                "monthly_price": "On the monthly monitoring, for the first six months it'll just be $29.99 per month. After that, it goes to the standard rate of $32.99.",
                "equip_total": "And the equipment — with all the discounts and promotions today, I'm gonna get your total down to a great price.",
                "ask_commitment": "Does that sound like it will work for you?",
                "guide_checkout": "Go ahead and put your payment info in. Let me know once you've placed the order and I'll confirm on my side.",
                "order_confirmed": "Congratulations and welcome to the Cove family! You'll get tracking info as soon as your package ships — that's usually 3 to 7 business days. Once it arrives, you'll find step-by-step setup instructions inside. If you need a technician, we have a third-party service starting at $129. And one more thing — if you have home insurance, you can request an alarm certificate from us and submit it to your insurance company for a discount.",
            }

            if topic in _DISCOVERY_PROMPTS:
                prompt = _DISCOVERY_PROMPTS[topic]
                stage_for_item = "discovery"
            elif topic in self._COLLECT_INFO_PROMPTS:
                prompt = self._COLLECT_INFO_PROMPTS[topic]
                stage_for_item = "collect_info"
            elif topic in _BUILD_PROMPTS:
                prompt = _BUILD_PROMPTS[topic]
                stage_for_item = "build_system"
            elif topic in _CLOSING_PROMPTS:
                prompt = _CLOSING_PROMPTS[topic]
                stage_for_item = "closing"

            if prompt:
                if self.coach and self.coach.customer_name:
                    prompt = prompt.replace("[NAME]", self.coach.customer_name)
                # Only update the Then box — no Say First, so we don't flood
                # the opener bubble when the rep is rapidly toggling checkboxes
                await self.send({"type": "call_guidance", "call_stage": stage_for_item,
                                 "next_step": prompt})
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
                fallback = _fallback_next_step("build_system", self.coach)
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
                    fallback = _fallback_next_step("discovery", self.coach)
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

            new_stage = suggestion.get("call_stage") or "intro"
            new_idx = _STAGE_ORDER.index(new_stage) if new_stage in _STAGE_ORDER else 0
            cur_idx = _STAGE_ORDER.index(self.current_stage) if self.current_stage in _STAGE_ORDER else 0
            if new_idx < cur_idx:
                new_stage = self.current_stage
                suggestion["call_stage"] = new_stage
            else:
                self.current_stage = new_stage

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
                cleaned_next = _fallback_next_step(new_stage, self.coach)
                if cleaned_next:
                    suggestion["next_step"] = cleaned_next

            if self.coach and self.coach.customer_name and cleaned_next:
                cleaned_next = cleaned_next.replace("[NAME]", self.coach.customer_name)
                suggestion["next_step"] = cleaned_next

            # P1 FIX: Trim overly long suggestions so the rep can read them
            # P2 FIX: Remove "$____" placeholders that Claude sometimes outputs
            if cleaned_next:
                cleaned_next = _trim_long_suggestion(cleaned_next)
                cleaned_next = cleaned_next.replace("$____", "your discounted price")
                suggestion["next_step"] = cleaned_next

            if cleaned_next and self.coach:
                self.coach.track_equipment_from_text(cleaned_next)

            await self.send({"type": "call_guidance", "call_stage": new_stage, "next_step": cleaned_next})
            await self.send_checklist()

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
            return
        combined = " ".join(self.rep_buffer)
        self.rep_buffer = []
        print(f"[roleplay] rep said: {combined[:80]}")
        try:
            ai_text = await self.roleplay_customer.respond(combined)
            print(f"[roleplay] AI response: {ai_text[:80]}")
            audio_b64 = await _tts(ai_text, self.roleplay_customer.voice)
            if audio_b64:
                self.tts_active = True
                asyncio.create_task(self._tts_safety_reset(5))
            await self.send({"type": "roleplay_speech", "text": ai_text, "audio_b64": audio_b64})
            await self._on_transcript("customer", ai_text, True, True)
        except Exception:
            import traceback
            print(f"[roleplay] error:\n{traceback.format_exc()}")
            self.tts_active = False
            if self.pending_rep_buffer:
                self.rep_buffer.extend(self.pending_rep_buffer)
                self.pending_rep_buffer.clear()

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
            print("[roleplay] tts_active safety reset fired")
            self.tts_active = False
            if self.pending_rep_buffer:
                self.rep_buffer.extend(self.pending_rep_buffer)
                self.pending_rep_buffer.clear()
                asyncio.create_task(self._fire_roleplay_response())

    # ── Transcript callback ──

    async def _on_transcript(self, speaker: str, text: str, is_final: bool, speech_final: bool):
        # In roleplay, if TTS is playing but rep is speaking, release the lock.
        # Always show transcript in UI; only skip coaching/roleplay processing.
        if self.roleplay_mode and self.tts_active and speaker == "rep":
            await self.send({"type": "transcript", "speaker": speaker, "text": text, "is_final": is_final})
            if is_final and speech_final:
                self.tts_active = False
                # Buffer this text so it's processed after TTS ends
                self.pending_rep_buffer.append(text)
            return

        await self.send({"type": "transcript", "speaker": speaker, "text": text, "is_final": is_final})

        # ── Fast-track intro ──
        if speaker == "customer" and is_final and self.coach is not None and self.current_stage == "intro" and self.intro_turns < 2:
            self.intro_turns += 1
            self.coach.add_turn(speaker, text)
            self.customer_buffer.append(text)
            t = text.lower()
            _wants_system = any(w in t for w in [
                "looking to get", "looking for", "interested in", "i want", "i need",
                "i'm looking", "im looking", "get a system", "get a security",
                "set up", "get this", "security system", "protect",
            ])
            if self.intro_turns == 1:
                if _wants_system:
                    self.current_stage = "discovery"
                    self.intro_turns = 2
                    self.coach._topics_done.add("existing_customer")
                    self.coach._topics_done.add("had_system_before")
                    await self.send({"type": "call_guidance", "call_stage": "discovery",
                        "next_step": "Perfect, well I'll be the one to walk you through the process and help you get set up. Have you ever had a security system before?"})
                    return
                self.coach._topics_done.add("existing_customer")
                await self.send({"type": "call_guidance", "call_stage": "intro",
                    "next_step": "Are you already a Cove customer, or are you looking to get a security system?"})
                return
            if self.intro_turns == 2:
                self.current_stage = "discovery"
                self.coach._topics_done.add("existing_customer")
                self.coach._topics_done.add("had_system_before")
                await self.send({"type": "call_guidance", "call_stage": "discovery",
                    "next_step": "Perfect, well I'll be the one to walk you through the process and help you get set up. Have you ever had a security system before?"})
                return

        # ── Fast-track collect_info ──
        # During collect_info, responses come rapid-fire (name, phone, email, address).
        # Claude coaching gets cancelled before completing, so use instant hardcoded suggestions.
        # IMPORTANT: Validate what the customer actually said before advancing.
        if speaker == "customer" and is_final and self.coach is not None and self.current_stage == "collect_info":
            self.coach.add_turn(speaker, text)
            t = text.lower()

            # Detect what TYPE of info the customer just gave
            _has_name_words = any(w in t for w in ["my name is", "first name", "last name"]) or (len(t.split()) <= 4 and t.replace(" ", "").isalpha())
            _has_phone_digits = sum(c.isdigit() for c in t) >= 7
            _has_email = "@" in t or any(w in t for w in ["gmail", "yahoo", "hotmail", "aol", "outlook", "dot com"])
            _has_address = any(w in t for w in ["street", "drive", "avenue", "road", "lane", "boulevard",
                                                 "way", "circle", "court", "north", "south", "east", "west"]) or sum(c.isdigit() for c in t) >= 4

            # Only advance if the customer gave the expected info type
            opener = _quick_opener(text, "collect_info")
            self.coach.set_opener(opener)
            next_step = None

            if "full_name" not in self._collect_info_done:
                if _has_name_words or (not _has_phone_digits and not _has_email and not _has_address):
                    self._collect_info_done.add("full_name")
                    next_step = "And what's your best phone number?"
            elif "phone_number" not in self._collect_info_done:
                if _has_phone_digits:
                    self._collect_info_done.add("phone_number")
                    next_step = "And your email so I can send all this information over to you by the end of the call?"
                # If they're still spelling their name or giving partial info, don't advance
            elif "email" not in self._collect_info_done:
                if _has_email:
                    self._collect_info_done.add("email")
                    next_step = "And before we get ahead of ourselves, I just want to verify we have coverage. What's the address you're looking to get the security set up at?"
            elif "address" not in self._collect_info_done:
                if _has_address:
                    self._collect_info_done.add("address")
                    self.current_stage = "build_system"
                    opener = _pick(["Awesome, we have fantastic coverage in your area.",
                                    "Great news — we have great coverage out there.",
                                    "Perfect, we can definitely service that area."])
                    self.coach.set_opener(opener)
                    next_step = "Let's go ahead and build your system. How many doors go in and out of your home?"
                    self.coach._topics_done.add("full_name")
                    self.coach._topics_done.add("phone_number")
                    self.coach._topics_done.add("email")
                    self.coach._topics_done.add("address")

            if next_step:
                await self.send({"type": "call_guidance", "call_stage": self.current_stage, "opener": opener, "next_step": next_step})
                await self.send_checklist()
                return

        # ── Opener ──
        # Only show opener on speech_final (complete utterance) to prevent
        # the Say First from changing mid-speech as interim results come in.
        _skip_opener = self.current_stage in ("intro",)
        if speaker == "customer" and not self.opener_shown and self.coach is not None and not _skip_opener:
            if is_final and speech_final:
                opener = _quick_opener(text, self.current_stage)
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
            if any(w in t for w in ["no contract", "month to month", "cancel anytime"]):
                _closing_detected.append("no_contract")
            if any(w in t for w in ["wireless", "ships to you", "twenty minutes", "20 minutes", "set it up yourself", "no installation"]):
                _closing_detected.append("wireless_install")
            if any(w in t for w in ["sixty day", "60 day", "risk free", "risk-free", "full refund"]):
                _closing_detected.append("trial_60")
            if any(w in t for w in ["29.99", "twenty nine", "32.99", "thirty two", "per month", "monthly monitoring"]):
                _closing_detected.append("monthly_price")
            if any(w in t for w in ["total", "discounts", "promotions", "equipment cost", "hundred"]):
                _closing_detected.append("equip_total")
            if any(w in t for w in ["work for you", "sound good", "does that work", "gonna work"]):
                _closing_detected.append("ask_commitment")
            if any(w in t for w in ["scroll down", "fill in your email", "verbal password", "emergency contact", "card info", "card number", "checkout"]):
                _closing_detected.append("guide_checkout")
            if any(w in t for w in ["congratulations", "welcome to the cove", "welcome to cove", "tracking info", "package ships"]):
                _closing_detected.append("order_confirmed")
            if _closing_detected:
                for item in _closing_detected:
                    if item not in self._rep_overrides:
                        self.coach._topics_done.add(item)
                await self.send_checklist()
                # Auto-advance: show the next closing item immediately
                # so the rep can continue the monologue without waiting
                # for a customer response
                if speech_final:
                    next_closing = _fallback_next_step("closing", self.coach)
                    if next_closing:
                        if self.coach.customer_name:
                            next_closing = next_closing.replace("[NAME]", self.coach.customer_name)
                        await self.send({"type": "call_guidance", "call_stage": "closing",
                                         "next_step": next_closing})

        if not speech_final:
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
                        was = session.tts_active
                        session.tts_active = msg.get("active", False)
                        if was and not session.tts_active and session.pending_rep_buffer:
                            session.rep_buffer.extend(session.pending_rep_buffer)
                            session.pending_rep_buffer.clear()
                            asyncio.create_task(session._fire_roleplay_response())
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


# Serve frontend static files (must be after all route definitions)
app.mount("/", StaticFiles(directory=_FRONTEND_DIR), name="static")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8765))
    uvicorn.run(app, host="0.0.0.0", port=port)
