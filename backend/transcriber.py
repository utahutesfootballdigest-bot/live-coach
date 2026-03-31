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
    "&endpointing=600"
    "&utterance_end_ms=1200"
)

# Callback signature: (speaker: str, text: str, is_final: bool, speech_final: bool)
TranscriptCallback = Callable[[str, str, bool, bool], Awaitable[None]]


async def _stream(api_key: str, label: str, audio_queue: asyncio.Queue, cb: TranscriptCallback):
    headers = {"Authorization": f"Token {api_key}"}
    last_final: str = ""   # dedup consecutive identical finals

    while True:
        try:
            print(f"[transcriber:{label}] connecting to Deepgram...")
            try:
                ws_conn = await websockets.connect(DEEPGRAM_URL, extra_headers=headers)
            except TypeError:
                # Older websockets versions use additional_headers
                ws_conn = await websockets.connect(DEEPGRAM_URL, additional_headers=headers)
            async with ws_conn as ws:
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

                async def _recv():
                    nonlocal last_final
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue
                        if msg.get("type") != "Results":
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

                        await cb(label, text, is_final, speech_final)

                await asyncio.gather(_send(), _recv())
                return  # clean shutdown via None sentinel — don't reconnect

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
