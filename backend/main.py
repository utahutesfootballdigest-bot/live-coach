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

_AFFIRMATIVE_STARTERS = {
    "perfect", "awesome", "great", "love it", "got it", "okay", "ok",
    "sure thing", "sounds good", "absolutely", "no worries", "no problem",
    "nice to meet you", "that's great", "that's awesome", "that's perfect",
    "wonderful", "excellent", "fantastic", "congrats", "congratulations",
    "i totally hear you", "i hear you", "i understand", "totally understandable",
    "that makes sense", "that's totally fine", "that's helpful", "good to know",
    "congrats on the new place", "no worries at all", "that's so exciting",
    "i totally understand that feeling", "that makes complete sense",
}

def _dedup_affirmative(opener: str, next_step: str) -> str:
    if not opener or not next_step:
        return next_step
    opener_lower = opener.lower().strip().rstrip("!.,")
    if not any(opener_lower.startswith(a) for a in _AFFIRMATIVE_STARTERS):
        return next_step
    cleaned = _AFFIRMATIVE_PATTERN.sub("", next_step, count=1).strip()
    if cleaned and cleaned[0].islower():
        cleaned = cleaned[0].upper() + cleaned[1:]
    return cleaned if cleaned else next_step


# Track recently used openers globally (fine to share across sessions)
_recent_openers: list[str] = []

def _pick(options: list[str]) -> str:
    available = [o for o in options if o not in _recent_openers]
    if not available:
        available = options
    choice = random.choice(available)
    _recent_openers.append(choice)
    if len(_recent_openers) > 6:
        _recent_openers.pop(0)
    return choice


def _quick_opener(text: str, current_stage: str) -> str:
    t = text.lower().strip()

    if any(w in t for w in ["break in", "broken into", "robbery", "robbed", "burglar", "stolen", "theft", "broke in"]):
        return _pick(["Oh no, I'm so sorry to hear that. I'll make sure we get you fully protected.",
                       "That's terrible — I'm really sorry you're dealing with that. Let's make sure that doesn't happen again."])
    if any(w in t for w in ["scared", "nervous", "worried", "anxious", "afraid", "terrified", "freaked"]):
        return _pick(["I totally understand that feeling, and that's exactly why we're here to help.",
                       "That makes complete sense — your safety is our top priority and I'll take great care of you."])
    if any(w in t for w in ["baby", "newborn", "toddler", "infant", "pregnant", "expecting"]):
        return _pick(["Congratulations! Keeping the little one safe is exactly what we do.",
                       "That's so exciting — we'll make sure that baby is well protected."])
    if any(w in t for w in [" kids", "children", "my son", "my daughter", "teenager"]):
        return _pick(["Keeping the family safe is what it's all about — I'll make sure we get everyone protected.",
                       "That's great — we've got some awesome features that'll be perfect for your family."])
    if any(w in t for w in ["moved", "new house", "just bought", "new home", "new place", "just purchased"]):
        return _pick(["Congrats on the new place! A new home is the perfect time to get set up.",
                       "That's exciting — let's make sure your new place is fully protected from day one."])
    if any(w in t for w in ["neighbor", "down the street", "next door", "in the area", "in my neighborhood"]):
        return _pick(["That's really unsettling when it's that close to home. I'll make sure we get you covered.",
                       "I don't blame you — that would make anyone want to take action. Let's get you protected."])
    if any(w in t for w in ["live alone", "by myself", "on my own"]):
        return _pick(["Having that extra layer of protection makes a huge difference when you're on your own.",
                       "That's smart — peace of mind when you're on your own is so important. I'll take care of you."])
    if any(w in t for w in ["travel", "work nights", "gone a lot", "away from home", "long hours", "deployed", "while i'm working", "while i work", "when i'm at work", "protect my family"]):
        return _pick(["That makes a lot of sense — being able to keep an eye on things from anywhere is key.",
                       "I hear that all the time. We'll make sure you have peace of mind no matter where you are."])
    if any(w in t for w in ["expensive", "too much", "cost", "afford", "pricey", "price", "budget"]):
        return _pick(["I totally hear you on that — let me see what I can do to make this work for you.",
                       "I understand where you're coming from. Let me break it down for you."])
    if any(w in t for w in ["talk to my", "ask my wife", "ask my husband", "spouse", "partner", "think about it", "call back", "not sure"]):
        return _pick(["Totally understandable — I'd want to check with my partner too.",
                       "No worries, I completely get that. Let me share a few things that might help."])

    if current_stage in ("intro", "discovery"):
        if any(w in t for w in ["never had", "no i haven", "first time", "don't have one"]):
            return _pick(["No worries at all — like I said, I'll walk you through everything and make it super easy.",
                           "That's totally fine — I'll take great care of you and walk you through the whole process."])
        if any(w in t for w in ["i had", "i was with", "i used to have", "we had", "i've had"]):
            return _pick(["Good to know — that past experience will definitely help us get you set up right.",
                           "That's helpful to know. We'll make sure we match or beat what you had before."])

    if current_stage == "build_system":
        if any(w in t for w in ["sounds good", "that works", "that makes sense", "yes", "yeah", "yep", "okay", "ok", "of course", "absolutely"]):
            return _pick(["Awesome, easy to work with — I love it. Let me keep going here.",
                           "Love it. Alright, let me get you set up with the next piece.",
                           "Perfect, glad that makes sense. Let me keep building this out for you."])

    if current_stage in ("recap", "closing"):
        if any(w in t for w in ["sounds good", "that works", "let's do it", "yes", "yeah", "i'm ready"]):
            return _pick(["Awesome! Let me see what I can do for you on the pricing.",
                           "Love it — let me get this wrapped up for you."])
        if any(w in t for w in ["no thank", "i'm good", "that's it", "nothing else", "that's all", "that should be good"]):
            return _pick(["Sounds good — I think we've got you fully covered here.",
                           "No problem at all — personally I think we've got everything you need."])

    if any(w in t for w in ["yes", "yeah", "yep", "that's right", "correct", "sure", "sounds good", "absolutely"]):
        return _pick(["Ok great, I'll definitely help you out with that.",
                       "Perfect, let me take care of that for you.",
                       "Awesome, I'll get that handled for you right away."])

    return _pick(["Ok great, I'll definitely help you out.",
                   "Absolutely, let me take care of you.",
                   "Perfect, I'll get you taken care of."])


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

    async def send(self, msg: dict):
        try:
            await self.ws.send_text(json.dumps(msg))
        except Exception:
            pass

    # ── Session lifecycle ──

    async def start_live(self):
        if self.running:
            return
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
                opener_used = self.coach._last_opener if self.coach else ""
                cleaned_next = _dedup_affirmative(opener_used, raw_next)
                suggestion["next_step"] = cleaned_next
            else:
                cleaned_next = raw_next

            if self.coach and self.coach.customer_name and cleaned_next:
                cleaned_next = cleaned_next.replace("[NAME]", self.coach.customer_name)
                suggestion["next_step"] = cleaned_next

            if cleaned_next and self.coach:
                self.coach.track_equipment_from_text(cleaned_next)

            await self.send({"type": "call_guidance", "call_stage": new_stage, "next_step": cleaned_next})

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
        # In roleplay, if TTS is playing but rep is speaking, release the lock
        if self.roleplay_mode and self.tts_active and speaker == "rep":
            if is_final and speech_final:
                self.tts_active = False
            else:
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

        # ── Opener ──
        _skip_opener = self.current_stage in ("intro", "collect_info")
        if speaker == "customer" and not self.opener_shown and self.coach is not None and not _skip_opener:
            if not is_final and len(text.split()) < 8:
                pass
            else:
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
        if not speech_final:
            return

        self.coach.add_turn(speaker, text)

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
