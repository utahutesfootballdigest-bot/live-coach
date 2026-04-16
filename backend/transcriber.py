import asyncio
import json
import websockets
from typing import Callable, Awaitable


DEEPGRAM_URL = (
    "wss://api.deepgram.com/v1/listen"
    "?model=nova-3"
    "&language=en-US"
    "&encoding=linear16"
    "&sample_rate=16000"
    "&channels=1"
    "&interim_results=true"
    "&endpointing=500"
    "&utterance_end_ms=1200"
    "&vad_events=true"
)

# Callback signature: (speaker: str, text: str, is_final: bool, speech_final: bool)
TranscriptCallback = Callable[[str, str, bool, bool], Awaitable[None]]

# Which kwarg name websockets accepts for headers — varies by version + Python.
# Detected once on first connection, then cached for the process lifetime.
_ws_header_kwarg = None


async def _connect_deepgram(url: str, headers: dict):
    """Connect to Deepgram, auto-detecting the correct headers kwarg.
    Returns an async context manager for the websocket connection."""
    global _ws_header_kwarg

    if _ws_header_kwarg:
        return websockets.connect(url, **{_ws_header_kwarg: headers})

    # First call — try both and cache whichever works
    try:
        ws = await websockets.connect(url, extra_headers=headers)
        _ws_header_kwarg = "extra_headers"
        print(f"[transcriber] using extra_headers")
        return ws
    except TypeError:
        pass

    try:
        ws = await websockets.connect(url, additional_headers=headers)
        _ws_header_kwarg = "additional_headers"
        print(f"[transcriber] using additional_headers")
        return ws
    except TypeError:
        raise RuntimeError("websockets.connect() accepts neither extra_headers nor additional_headers")


async def _stream(api_key: str, label: str, audio_queue: asyncio.Queue, cb: TranscriptCallback):
    headers = {"Authorization": f"Token {api_key}"}
    last_final: str = ""   # dedup consecutive identical finals

    while True:
        try:
            print(f"[transcriber:{label}] connecting to Deepgram...")

            ws_or_cm = await _connect_deepgram(DEEPGRAM_URL, headers)

            # _connect_deepgram returns either a raw websocket (first call, via await)
            # or a context manager (cached calls). Handle both.
            if hasattr(ws_or_cm, 'recv'):
                # Already a connected websocket
                ws = ws_or_cm
            else:
                # Context manager — need to enter it
                ws = await ws_or_cm.__aenter__()

            try:
                print(f"[transcriber:{label}] connected")

                async def _send():
                    while True:
                        chunk = await audio_queue.get()
                        if chunk is None:
                            try:
                                await ws.send(json.dumps({"type": "CloseStream"}))
                            except Exception:
                                pass
                            return
                        await ws.send(chunk)

                # Buffer is_final transcripts without speech_final so we can
                # flush them on UtteranceEnd (covers cases where Deepgram detects
                # the speaker is done but never marks speech_final=True)
                pending_final: list[str] = []

                async def _recv():
                    nonlocal last_final
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue

                        msg_type = msg.get("type")

                        # Handle UtteranceEnd — speaker is done, flush any buffered finals
                        if msg_type == "UtteranceEnd":
                            if pending_final:
                                combined = " ".join(pending_final).strip()
                                pending_final.clear()
                                if combined and combined != last_final:
                                    last_final = combined
                                    print(f"[transcriber:{label}] UtteranceEnd flush: {combined[:60]!r}")
                                    await cb(label, combined, True, True)
                            continue

                        if msg_type != "Results":
                            continue
                        alts = msg.get("channel", {}).get("alternatives", [])
                        if not alts:
                            continue
                        text = alts[0].get("transcript", "").strip()
                        if not text:
                            continue
                        is_final = msg.get("is_final", False)
                        speech_final = msg.get("speech_final", False)

                        # Drop duplicate finals (can occur after reconnect)
                        if is_final and speech_final:
                            if text == last_final:
                                print(f"[transcriber:{label}] dropping duplicate: {text[:50]!r}")
                                continue
                            last_final = text
                            pending_final.clear()  # speech_final supersedes pending
                        elif is_final:
                            # Buffer for UtteranceEnd flush
                            pending_final.append(text)

                        await cb(label, text, is_final, speech_final)

                await asyncio.gather(_send(), _recv())
                return  # clean shutdown via None sentinel — don't reconnect

            finally:
                try:
                    await ws.close()
                except Exception:
                    pass

        except asyncio.CancelledError:
            return
        except Exception as e:
            print(f"[transcriber:{label}] disconnected: {e} — reconnecting in 1s")
            # Drain stale audio so reconnect starts fresh
            drained = 0
            while not audio_queue.empty():
                try:
                    audio_queue.get_nowait()
                    drained += 1
                except Exception:
                    break
            if drained:
                print(f"[transcriber:{label}] drained {drained} stale chunks")
            await asyncio.sleep(1)


class Transcriber:
    def __init__(self, api_key: str):
        self._api_key = api_key
        self._tasks: list[asyncio.Task] = []

    def start(self, mic_queue: asyncio.Queue, loopback_queue: asyncio.Queue, cb: TranscriptCallback):
        self._tasks = [
            asyncio.create_task(_stream(self._api_key, "rep", mic_queue, cb)),
            asyncio.create_task(_stream(self._api_key, "customer", loopback_queue, cb)),
        ]

    def start_mic_only(self, mic_queue: asyncio.Queue, cb: TranscriptCallback):
        """Only transcribe the rep's mic — used for roleplay mode."""
        self._tasks = [
            asyncio.create_task(_stream(self._api_key, "rep", mic_queue, cb)),
        ]

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
