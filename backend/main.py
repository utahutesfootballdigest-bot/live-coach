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

@app.get("/")
async def serve_index():
    return FileResponse(os.path.join(_FRONTEND_DIR, "index.html"))

# ── singleton session state ──────────────────────────────────────────────────
mic_queue: asyncio.Queue | None = None
loopback_queue: asyncio.Queue | None = None
transcriber: Transcriber | None = None
coach: CoachingEngine | None = None
roleplay_customer: RoleplayCustomer | None = None
active_clients: set[WebSocket] = set()
session_running = False
roleplay_mode = False
tts_active = False          # True while AI customer audio is playing in the browser
customer_buffer: list[str] = []
rep_buffer: list[str] = []       # accumulates rep speech in roleplay before AI responds
pending_rep_buffer: list[str] = []  # rep finals received while tts_active — processed when TTS ends
_coach_trigger_task: asyncio.Task | None = None
_roleplay_trigger_task: asyncio.Task | None = None
pending_evaluation: dict | None = None
session_scores: list[int] = []
_stage_order = ["intro", "discovery", "collect_info", "build_system", "recap", "closing"]
_current_stage = "intro"
_opener_shown = False  # True once a quick opener has been displayed for the current customer utterance
_intro_turns = 0  # count of customer turns during intro stage (for fast-track)


def _preprocess_tts(text: str) -> str:
    """Fix known Deepgram Aura quirks before sending."""
    import re
    # Em-dashes and en-dashes cause unnatural long pauses
    text = text.replace("—", ", ").replace("–", ", ")
    # Ellipses cause pauses
    text = text.replace("...", ",").replace("…", ",")
    # Standalone 'a' is read as the letter — add comma to force word pronunciation
    text = re.sub(r'\ba\b', 'a,', text)
    # Collapse any double spaces or double commas left behind
    text = re.sub(r',\s*,', ',', text)
    text = re.sub(r' {2,}', ' ', text)
    return text.strip()


async def _tts(text: str, voice: str = "aura-asteria-en") -> str | None:
    """Call Deepgram Aura TTS, return base64-encoded MP3 or None on error."""
    processed = _preprocess_tts(text)
    def _call():
        r = requests.post(
            f"https://api.deepgram.com/v1/speak?model={voice}",
            headers={
                "Authorization": f"Token {DEEPGRAM_API_KEY}",
                "Content-Type": "application/json",
            },
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


async def broadcast(msg: dict):
    data = json.dumps(msg)
    dead = set()
    for ws in active_clients:
        try:
            await ws.send_text(data)
        except Exception:
            dead.add(ws)
    active_clients.difference_update(dead)


# Track recently used openers to avoid repeating
_recent_openers: list[str] = []

def _pick(options: list[str]) -> str:
    """Pick an opener we haven't used recently."""
    available = [o for o in options if o not in _recent_openers]
    if not available:
        available = options
    choice = random.choice(available)
    _recent_openers.append(choice)
    if len(_recent_openers) > 6:
        _recent_openers.pop(0)
    return choice


def _quick_opener(text: str) -> str:
    """Context-aware instant opener — must be a full bridging sentence, not a single word."""
    t = text.lower().strip()

    # ── Emotional / situational (high priority) ──
    if any(w in t for w in ["break in", "broken into", "robbery", "robbed", "burglar", "stolen", "theft", "broke in"]):
        return _pick([
            "Oh no, I'm so sorry to hear that. I'll make sure we get you fully protected.",
            "That's terrible — I'm really sorry you're dealing with that. Let's make sure that doesn't happen again.",
        ])
    if any(w in t for w in ["scared", "nervous", "worried", "anxious", "afraid", "terrified", "freaked"]):
        return _pick([
            "I totally understand that feeling, and that's exactly why we're here to help.",
            "That makes complete sense — your safety is our top priority and I'll take great care of you.",
        ])
    if any(w in t for w in ["baby", "newborn", "toddler", "infant", "pregnant", "expecting"]):
        return _pick([
            "Congratulations! Keeping the little one safe is exactly what we do.",
            "That's so exciting — we'll make sure that baby is well protected.",
        ])
    if any(w in t for w in [" kids", "children", "my son", "my daughter", "teenager"]):
        return _pick([
            "Keeping the family safe is what it's all about — I'll make sure we get everyone protected.",
            "That's great — we've got some awesome features that'll be perfect for your family.",
        ])
    if any(w in t for w in ["moved", "new house", "just bought", "new home", "new place", "just purchased"]):
        return _pick([
            "Congrats on the new place! A new home is the perfect time to get set up.",
            "That's exciting — let's make sure your new place is fully protected from day one.",
        ])
    if any(w in t for w in ["neighbor", "down the street", "next door", "in the area", "in my neighborhood"]):
        return _pick([
            "That's really unsettling when it's that close to home. I'll make sure we get you covered.",
            "I don't blame you — that would make anyone want to take action. Let's get you protected.",
        ])
    if any(w in t for w in ["live alone", "by myself", "on my own"]):
        return _pick([
            "Having that extra layer of protection makes a huge difference when you're on your own.",
            "That's smart — peace of mind when you're on your own is so important. I'll take care of you.",
        ])
    if any(w in t for w in ["travel", "work nights", "gone a lot", "away from home", "long hours", "deployed", "while i'm working", "while i work", "when i'm at work", "protect my family"]):
        return _pick([
            "That makes a lot of sense — being able to keep an eye on things from anywhere is key.",
            "I hear that all the time. We'll make sure you have peace of mind no matter where you are.",
        ])

    # ── Objection / concern signals ──
    if any(w in t for w in ["expensive", "too much", "cost", "afford", "pricey", "price", "budget"]):
        return _pick([
            "I totally hear you on that — let me see what I can do to make this work for you.",
            "I understand where you're coming from. Let me break it down for you.",
        ])
    if any(w in t for w in ["talk to my", "ask my wife", "ask my husband", "spouse", "partner", "think about it", "call back", "not sure"]):
        return _pick([
            "Totally understandable — I'd want to check with my partner too.",
            "No worries, I completely get that. Let me share a few things that might help.",
        ])

    # ── Discovery responses ──
    if _current_stage in ("intro", "discovery"):
        if any(w in t for w in ["never had", "no i haven", "first time", "don't have one"]):
            return _pick([
                "No worries at all — like I said, I'll walk you through everything and make it super easy.",
                "That's totally fine — I'll take great care of you and walk you through the whole process.",
            ])
        if any(w in t for w in ["i had", "i was with", "i used to have", "we had", "i've had"]):
            return _pick([
                "Good to know — that past experience will definitely help us get you set up right.",
                "That's helpful to know. We'll make sure we match or beat what you had before.",
            ])

    # ── Build system responses ──
    if _current_stage == "build_system":
        if any(w in t for w in ["sounds good", "that works", "that makes sense", "yes", "yeah", "yep", "okay", "ok", "of course", "absolutely"]):
            return _pick([
                "Awesome, easy to work with — I love it. Let me keep going here.",
                "Love it. Alright, let me get you set up with the next piece.",
                "Perfect, glad that makes sense. Let me keep building this out for you.",
            ])

    # ── Recap / closing ──
    if _current_stage in ("recap", "closing"):
        if any(w in t for w in ["sounds good", "that works", "let's do it", "yes", "yeah", "i'm ready"]):
            return _pick([
                "Awesome! Let me see what I can do for you on the pricing.",
                "Love it — let me get this wrapped up for you.",
            ])
        if any(w in t for w in ["no thank", "i'm good", "that's it", "nothing else", "that's all", "that should be good"]):
            return _pick([
                "Sounds good — I think we've got you fully covered here.",
                "No problem at all — personally I think we've got everything you need.",
            ])

    # ── General affirmative ──
    if any(w in t for w in ["yes", "yeah", "yep", "that's right", "correct", "sure", "sounds good", "absolutely"]):
        return _pick([
            "Ok great, I'll definitely help you out with that.",
            "Perfect, let me take care of that for you.",
            "Awesome, I'll get that handled for you right away.",
        ])

    # ── Default — always a full sentence ──
    return _pick([
        "Ok great, I'll definitely help you out.",
        "Absolutely, let me take care of you.",
        "Perfect, I'll get you taken care of.",
    ])


import re as _re

# Affirmative phrases that can appear at the start of next_step
_AFFIRMATIVE_PATTERN = _re.compile(
    r"^(perfect[!.,]*\s*|awesome[!.,]*\s*|great[!.,]*\s*|love it[!.,]*\s*|"
    r"got it[!.,]*\s*|ok(?:ay)?[!.,]*\s*|sure thing[!.,]*\s*|sounds good[!.,]*\s*|"
    r"absolutely[!.,]*\s*|no worries[!.,]*\s*|no problem[!.,]*\s*|"
    r"that's great[!.,]*\s*|that's awesome[!.,]*\s*|that's perfect[!.,]*\s*|"
    r"nice[!.,]*\s*|wonderful[!.,]*\s*|excellent[!.,]*\s*|fantastic[!.,]*\s*|"
    r"i totally hear you[!.,]*\s*|i hear you[!.,]*\s*|i understand[!.,]*\s*|"
    r"totally understandable[!.,]*\s*|that makes sense[!.,]*\s*|"
    r"that's totally fine[!.,]*\s*|that's helpful[!.,]*\s*|good to know[!.,]*\s*)",
    _re.IGNORECASE,
)

def _dedup_affirmative(opener: str, next_step: str) -> str:
    """If the opener is already an affirmative, strip any leading affirmative from next_step."""
    if not opener or not next_step:
        return next_step
    # Check if opener itself is affirmative-ish
    opener_lower = opener.lower().strip().rstrip("!.,")
    affirmatives = {
        "perfect", "awesome", "great", "love it", "got it", "okay", "ok",
        "sure thing", "sounds good", "absolutely", "no worries", "no problem",
        "nice to meet you", "that's great", "that's awesome", "that's perfect",
        "wonderful", "excellent", "fantastic", "congrats", "congratulations",
        "i totally hear you", "i hear you", "i understand", "totally understandable",
        "that makes sense", "that's totally fine", "that's helpful", "good to know",
        "congrats on the new place", "no worries at all", "that's so exciting",
        "i totally understand that feeling", "that makes complete sense",
    }
    opener_is_affirmative = any(opener_lower.startswith(a) for a in affirmatives)
    if not opener_is_affirmative:
        return next_step
    # Strip leading affirmative from next_step
    cleaned = _AFFIRMATIVE_PATTERN.sub("", next_step, count=1).strip()
    if cleaned and cleaned[0].islower():
        cleaned = cleaned[0].upper() + cleaned[1:]
    return cleaned if cleaned else next_step


async def _fire_coaching():
    global customer_buffer, pending_evaluation, _current_stage, _opener_shown
    try:
        if not customer_buffer or coach is None:
            print(f"[coach] skipping fire: buffer={len(customer_buffer)} coach={'set' if coach else 'None'}")
            return
        customer_buffer = []
        await broadcast({"type": "status", "state": "processing"})
        suggestion = await coach.get_suggestion()
        print(f"[coach] got suggestion: {suggestion}")
        await broadcast({"type": "status", "state": "recording"})

        # Never let the stage regress — once past intro we never go back
        new_stage = suggestion.get("call_stage") or "intro"
        new_idx = _stage_order.index(new_stage) if new_stage in _stage_order else 0
        cur_idx = _stage_order.index(_current_stage) if _current_stage in _stage_order else 0
        if new_idx < cur_idx:
            print(f"[coach] ignoring stage regression {new_stage} < {_current_stage}")
            new_stage = _current_stage
            suggestion["call_stage"] = new_stage
        else:
            _current_stage = new_stage

        _opener_shown = False  # reset for next customer utterance

        raw_next = suggestion.get("next_step", "")

        # Strip double affirmative: if opener was affirmative, remove leading affirmative from next_step
        if raw_next:
            opener_used = coach._last_opener if coach else ""
            cleaned_next = _dedup_affirmative(opener_used, raw_next)
            if cleaned_next != raw_next:
                print(f"[coach] stripped affirmative: {raw_next[:40]!r} -> {cleaned_next[:40]!r}")
            suggestion["next_step"] = cleaned_next
        else:
            cleaned_next = raw_next

        # Replace [NAME] with customer's actual name
        if coach.customer_name and cleaned_next:
            cleaned_next = cleaned_next.replace("[NAME]", coach.customer_name)
            suggestion["next_step"] = cleaned_next

        # Track equipment mentioned in Claude's suggestion so we don't repeat next time
        if cleaned_next:
            coach.track_equipment_from_text(cleaned_next)

        print(f"[coach] stage={new_stage} triggered={suggestion.get('triggered')} next={cleaned_next[:60]}")
        await broadcast({
            "type": "call_guidance",
            "call_stage": new_stage,
            "next_step": cleaned_next,
        })
        if suggestion.get("triggered"):
            await broadcast({"type": "coaching", **suggestion})
            coach.mark_addressed(suggestion.get("objection_type", ""))
            pending_evaluation = {
                "objection_type": suggestion.get("objection_type", ""),
                "objection_summary": suggestion.get("objection_summary", ""),
            }
    except Exception as e:
        import traceback
        print(f"[coach] _fire_coaching error: {e}\n{traceback.format_exc()}")
        await broadcast({"type": "status", "state": "recording"})


async def _delayed_coaching():
    try:
        await _fire_coaching()
    except asyncio.CancelledError:
        # Cancelled because new customer speech came in — that's fine,
        # the new task will fire with fresher context
        await broadcast({"type": "status", "state": "recording"})
    except Exception:
        await broadcast({"type": "status", "state": "recording"})


async def _fire_roleplay_response():
    global rep_buffer, tts_active
    if not rep_buffer or not roleplay_customer:
        print(f"[roleplay] _fire_roleplay_response skipped: rep_buffer={len(rep_buffer)} roleplay_customer={'set' if roleplay_customer else 'None'}")
        return
    combined = " ".join(rep_buffer)
    rep_buffer = []
    print(f"[roleplay] rep said: {combined[:80]}")
    try:
        ai_text = await roleplay_customer.respond(combined)
        print(f"[roleplay] AI response: {ai_text[:80]}")
        audio_b64 = await _tts(ai_text, roleplay_customer.voice)
        if audio_b64:
            tts_active = True
            print(f"[roleplay] tts_active=True")
            asyncio.create_task(_tts_safety_reset(8))
        else:
            print("[roleplay] TTS failed, tts_active stays False")
        await broadcast({"type": "roleplay_speech", "text": ai_text, "audio_b64": audio_b64})
        await on_transcript("customer", ai_text, True, True)
    except Exception:
        import traceback
        print(f"[roleplay] _fire_roleplay_response error:\n{traceback.format_exc()}")
        # Make sure tts_active is reset so the rep can keep talking
        tts_active = False


async def _tts_safety_reset(seconds: int):
    await asyncio.sleep(seconds)
    global tts_active, rep_buffer, pending_rep_buffer
    if tts_active:
        print("[roleplay] tts_active safety reset fired")
        tts_active = False
        if pending_rep_buffer:
            print(f"[roleplay] safety reset flushing {len(pending_rep_buffer)} held rep turn(s)")
            rep_buffer.extend(pending_rep_buffer)
            pending_rep_buffer.clear()
            asyncio.create_task(_fire_roleplay_response())


async def _delayed_roleplay_response():
    await asyncio.sleep(0.6)
    await _fire_roleplay_response()


async def on_transcript(speaker: str, text: str, is_final: bool, speech_final: bool):
    global coach, customer_buffer, rep_buffer, pending_rep_buffer, _coach_trigger_task, _roleplay_trigger_task, pending_evaluation, session_scores, roleplay_customer

    # In roleplay, suppress rep mic while AI audio plays to avoid speaker bleed-through
    if roleplay_mode and tts_active and speaker == "rep":
        if is_final and speech_final:
            # Hold the final — replay it once TTS ends so the AI can respond
            pending_rep_buffer.append(text)
            print(f"[roleplay] held rep final during tts_active: {text[:40]!r}")
        return

    global _opener_shown, _intro_turns, _current_stage

    await broadcast({"type": "transcript", "speaker": speaker, "text": text, "is_final": is_final})

    # ── FAST-TRACK: First 2 customer turns in intro — fire canned response instantly ──
    if speaker == "customer" and is_final and coach is not None and _current_stage == "intro" and _intro_turns < 2:
        _intro_turns += 1
        coach.add_turn(speaker, text)
        customer_buffer.append(text)
        t = text.lower()
        _wants_system = any(w in t for w in [
            "looking to get", "looking for", "interested in", "i want", "i need",
            "i'm looking", "im looking", "get a system", "get a security",
            "set up", "get this", "security system", "protect",
        ])
        if _intro_turns == 1:
            if _wants_system:
                # Customer already said they want a system — skip the intro question
                next_line = "Perfect, well I'll be the one to walk you through the process and help you get set up. Have you ever had a security system before?"
                _current_stage = "discovery"
                _intro_turns = 2  # skip turn 2
                # Mark topics done: we asked about existing customer AND had system before
                coach._topics_done.add("existing_customer")
                coach._topics_done.add("had_system_before")
                print(f"[coach] fast-track: customer wants system, skipping intro -> discovery")
                await broadcast({"type": "call_guidance", "call_stage": "discovery", "next_step": next_line})
                return
            # Otherwise it's a greeting — fire the intro question
            next_line = "Are you already a Cove customer, or are you looking to get a security system?"
            coach._topics_done.add("existing_customer")
            print(f"[coach] fast-track: greeting -> intro question")
            await broadcast({"type": "call_guidance", "call_stage": "intro", "next_step": next_line})
            return
        if _intro_turns == 2:
            # Second customer turn — they said they want a system (or are existing)
            next_line = "Perfect, well I'll be the one to walk you through the process and help you get set up. Have you ever had a security system before?"
            _current_stage = "discovery"
            # Mark topics done
            coach._topics_done.add("existing_customer")
            coach._topics_done.add("had_system_before")
            print(f"[coach] fast-track: intro -> discovery")
            await broadcast({"type": "call_guidance", "call_stage": "discovery", "next_step": next_line})
            return

    # ── Customer: opener bubble for discovery/build/recap/closing ──
    # Skip opener for intro and collect_info (short formulaic exchanges)
    _skip_opener = _current_stage in ("intro", "collect_info")
    if speaker == "customer" and not _opener_shown and coach is not None and not _skip_opener:
        # Wait for enough words to pick a contextual opener (8+ on interim, any on final)
        if not is_final and len(text.split()) < 8:
            pass
        else:
            opener = _quick_opener(text)
            _opener_shown = True
            coach.set_opener(opener)
            print(f"[coach] opener: {opener[:60]}")
            await broadcast({
                "type": "call_guidance",
                "call_stage": _current_stage,
                "opener": opener,
            })

    if not is_final or coach is None:
        return

    # Skip very short filler-only finals
    stripped = text.strip()
    if len(stripped) < 2 or stripped.lower() in ("um", "uh", "hmm", "mm", "ah", "hm"):
        return

    # ── Customer final: add turn and fire coaching ──
    if speaker == "customer":
        coach.add_turn(speaker, text)
        customer_buffer.append(text)

        # Cancel any in-flight coaching and fire fresh — always use latest context
        if _coach_trigger_task and not _coach_trigger_task.done():
            _coach_trigger_task.cancel()
        _coach_trigger_task = asyncio.create_task(_delayed_coaching())
        return

    # ── Everything below is rep-only and requires speech_final ──
    if not speech_final:
        return

    coach.add_turn(speaker, text)

    # Evaluate rep response if we were waiting for one
    if pending_evaluation:
        ev = pending_evaluation
        pending_evaluation = None
        result = await coach.evaluate_response(
            ev["objection_type"], ev["objection_summary"], text
        )
        if result and result.get("score") is not None:
            session_scores.append(result["score"])
            avg = round(sum(session_scores) / len(session_scores))
            await broadcast({
                "type": "score_update",
                "score": result["score"],
                "feedback": result.get("feedback", ""),
                "breakdown": result.get("breakdown", {}),
                "session_avg": avg,
                "total_evaluated": len(session_scores),
            })

    if roleplay_mode and roleplay_customer:
        # Buffer rep speech — only respond after 0.6s of silence so we don't cut them off
        rep_buffer.append(text)
        print(f"[roleplay] buffered rep turn, rep_buffer len={len(rep_buffer)}, tts_active={tts_active}")
        if _roleplay_trigger_task and not _roleplay_trigger_task.done():
            _roleplay_trigger_task.cancel()
        _roleplay_trigger_task = asyncio.create_task(_delayed_roleplay_response())


async def start_session():
    global transcriber, coach, session_running, roleplay_mode, mic_queue, loopback_queue

    if session_running:
        return

    roleplay_mode = False
    mic_queue = asyncio.Queue()
    loopback_queue = asyncio.Queue()
    coach = CoachingEngine(ANTHROPIC_API_KEY)
    transcriber = Transcriber(DEEPGRAM_API_KEY)
    transcriber.start(mic_queue, loopback_queue, on_transcript)
    session_running = True
    await broadcast({"type": "status", "state": "recording"})


async def start_roleplay_session():
    global transcriber, coach, roleplay_customer, session_running, roleplay_mode, tts_active, mic_queue

    if session_running:
        return

    roleplay_mode = True
    mic_queue = asyncio.Queue()
    coach = CoachingEngine(ANTHROPIC_API_KEY)
    roleplay_customer = RoleplayCustomer(ANTHROPIC_API_KEY)
    transcriber = Transcriber(DEEPGRAM_API_KEY)
    transcriber.start_mic_only(mic_queue, on_transcript)
    session_running = True

    await broadcast({"type": "status", "state": "recording"})
    await broadcast({"type": "roleplay_mode", "active": True})

    # AI customer opens the conversation
    opening = await roleplay_customer.opening_line()
    audio_b64 = await _tts(opening, roleplay_customer.voice)
    if audio_b64:
        tts_active = True
        asyncio.create_task(_tts_safety_reset(8))
    await broadcast({"type": "roleplay_speech", "text": opening, "audio_b64": audio_b64})
    await on_transcript("customer", opening, True, True)


async def stop_session():
    global transcriber, coach, roleplay_customer, session_running, roleplay_mode, tts_active
    global customer_buffer, rep_buffer, pending_rep_buffer, mic_queue, loopback_queue
    global _coach_trigger_task, _roleplay_trigger_task, pending_evaluation, session_scores, _current_stage, _opener_shown, _intro_turns
    tts_active = False
    _current_stage = "intro"
    _opener_shown = False
    _intro_turns = 0
    _recent_openers.clear()

    if not session_running:
        return

    if _coach_trigger_task and not _coach_trigger_task.done():
        _coach_trigger_task.cancel()
    if _roleplay_trigger_task and not _roleplay_trigger_task.done():
        _roleplay_trigger_task.cancel()
    _coach_trigger_task = None
    _roleplay_trigger_task = None
    customer_buffer = []
    rep_buffer = []
    pending_rep_buffer = []
    pending_evaluation = None
    session_scores = []

    if mic_queue:
        mic_queue.put_nowait(None)
    if loopback_queue:
        loopback_queue.put_nowait(None)
    mic_queue = None
    loopback_queue = None
    if transcriber:
        await transcriber.stop()
        transcriber = None
    if coach:
        coach.reset()
        coach = None
    if roleplay_customer:
        roleplay_customer.reset()
        roleplay_customer = None

    session_running = False
    roleplay_mode = False
    await broadcast({"type": "status", "state": "idle"})
    await broadcast({"type": "roleplay_mode", "active": False})


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    global tts_active, pending_rep_buffer, rep_buffer
    await ws.accept()
    active_clients.add(ws)

    await ws.send_text(json.dumps({"type": "status", "state": "recording" if session_running else "idle"}))
    if roleplay_mode:
        await ws.send_text(json.dumps({"type": "roleplay_mode", "active": True}))

    _audio_chunk_count = 0
    try:
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                break

            # Binary frame: audio data from browser
            raw_bytes = message.get("bytes")
            if raw_bytes and len(raw_bytes) >= 2:
                _audio_chunk_count += 1
                if _audio_chunk_count == 1:
                    print(f"[ws] first audio chunk received, len={len(raw_bytes)}, mic_queue={'set' if mic_queue else 'None'}")
                elif _audio_chunk_count % 500 == 0:
                    print(f"[ws] audio chunks: {_audio_chunk_count}")
                label = raw_bytes[0]
                pcm = raw_bytes[1:]
                if label == 0x00 and mic_queue is not None:
                    mic_queue.put_nowait(pcm)
                elif label == 0x01 and loopback_queue is not None:
                    loopback_queue.put_nowait(pcm)
                continue

            # Text frame: JSON action
            if "text" in message and message["text"]:
                raw = message["text"]
                msg = json.loads(raw)
                action = msg.get("action")

                try:
                    if action == "start":
                        await start_session()

                    elif action == "start_roleplay":
                        await start_roleplay_session()

                    elif action == "tts_playing":
                        was_active = tts_active
                        tts_active = msg.get("active", False)
                        print(f"[roleplay] tts_active={tts_active} (browser signal)")
                        if was_active and not tts_active and pending_rep_buffer:
                            print(f"[roleplay] flushing {len(pending_rep_buffer)} held rep turn(s)")
                            rep_buffer.extend(pending_rep_buffer)
                            pending_rep_buffer.clear()
                            asyncio.create_task(_fire_roleplay_response())

                    elif action == "stop":
                        await stop_session()

                except Exception as e:
                    import traceback
                    print(f"[ws] message error (action={action!r}):\n{traceback.format_exc()}")

    except WebSocketDisconnect:
        active_clients.discard(ws)
    except Exception as e:
        import traceback
        active_clients.discard(ws)
        print(f"[ws] fatal error:\n{traceback.format_exc()}")


# Serve frontend static files (must be after all route definitions)
app.mount("/", StaticFiles(directory=_FRONTEND_DIR), name="static")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8765))
    uvicorn.run(app, host="0.0.0.0", port=port)
