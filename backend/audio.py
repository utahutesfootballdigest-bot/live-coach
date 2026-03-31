import asyncio
import pyaudiowpatch as pyaudio
import numpy as np


SAMPLE_RATE = 16000
BLOCK_SIZE = 3200  # 200ms at 16kHz
FORMAT = pyaudio.paInt16

# RMS noise gate for the rep mic — chunks quieter than this are blanked out.
# Prevents distant voices / room noise from being transcribed.
# Scale: int16 max = 32767. Typical values:
#   room noise / distant voice: ~100–600
#   normal speech into mic:     ~1000–5000+
# Raise this number to be stricter; lower it if your mic is quiet.
MIC_NOISE_GATE = 400

_pa = pyaudio.PyAudio()


def list_devices():
    """Return mic and loopback device lists."""
    mic_devices = []
    output_devices = []
    for i in range(_pa.get_device_count()):
        d = _pa.get_device_info_by_index(i)
        if d["maxInputChannels"] == 0:
            continue
        entry = {
            "id": i,
            "name": d["name"],
            "channels": int(d["maxInputChannels"]),
            "sample_rate": int(d["defaultSampleRate"]),
        }
        if d.get("isLoopbackDevice", False) or "[Loopback]" in d["name"]:
            output_devices.append(entry)
        else:
            mic_devices.append(entry)
    return mic_devices, output_devices


def _gate_mic(data: bytes) -> bytes:
    """Return silence if the chunk's RMS is below the noise gate threshold."""
    arr = np.frombuffer(data, dtype=np.int16).astype(np.float32)
    rms = np.sqrt(np.mean(arr ** 2)) if len(arr) else 0
    return data if rms >= MIC_NOISE_GATE else bytes(len(data))


def _to_mono_16k(data: bytes, src_rate: int, src_channels: int) -> bytes:
    """Convert raw int16 PCM to mono 16kHz."""
    arr = np.frombuffer(data, dtype=np.int16)
    if src_channels > 1:
        arr = arr.reshape(-1, src_channels).mean(axis=1).astype(np.int16)
    if src_rate != SAMPLE_RATE:
        ratio = src_rate / SAMPLE_RATE
        indices = np.round(np.arange(0, len(arr), ratio)).astype(int)
        indices = indices[indices < len(arr)]
        arr = arr[indices]
    return arr.tobytes()


class AudioCapture:
    def __init__(self):
        self._mic_stream = None
        self._loopback_stream = None
        self.mic_queue: asyncio.Queue | None = None
        self.loopback_queue: asyncio.Queue | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.running = False

    def start(self, mic_device_id: int, loopback_device_id: int, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self.mic_queue = asyncio.Queue()
        self.loopback_queue = asyncio.Queue()
        self.running = True

        mic_info = _pa.get_device_info_by_index(mic_device_id)
        mic_rate = int(mic_info["defaultSampleRate"])
        mic_channels = int(mic_info["maxInputChannels"])

        def _mic_cb(in_data, frame_count, time_info, status):
            if self.running:
                converted = _gate_mic(_to_mono_16k(in_data, mic_rate, mic_channels))
                self._loop.call_soon_threadsafe(self.mic_queue.put_nowait, converted)
            return (None, pyaudio.paContinue)

        lb_info = _pa.get_device_info_by_index(loopback_device_id)
        lb_rate = int(lb_info["defaultSampleRate"])
        lb_channels = int(lb_info["maxInputChannels"])

        def _loopback_cb(in_data, frame_count, time_info, status):
            if self.running:
                converted = _to_mono_16k(in_data, lb_rate, lb_channels)
                self._loop.call_soon_threadsafe(self.loopback_queue.put_nowait, converted)
            return (None, pyaudio.paContinue)

        self._mic_stream = _pa.open(
            format=FORMAT,
            channels=mic_channels,
            rate=mic_rate,
            input=True,
            input_device_index=mic_device_id,
            frames_per_buffer=int(BLOCK_SIZE * mic_rate / SAMPLE_RATE),
            stream_callback=_mic_cb,
        )

        self._loopback_stream = _pa.open(
            format=FORMAT,
            channels=lb_channels,
            rate=lb_rate,
            input=True,
            input_device_index=loopback_device_id,
            frames_per_buffer=int(BLOCK_SIZE * lb_rate / SAMPLE_RATE),
            stream_callback=_loopback_cb,
        )

        self._mic_stream.start_stream()
        self._loopback_stream.start_stream()

    def start_mic_only(self, mic_device_id: int, loop: asyncio.AbstractEventLoop):
        """Start only mic capture — used for roleplay mode."""
        self._loop = loop
        self.mic_queue = asyncio.Queue()
        self.loopback_queue = asyncio.Queue()  # stays empty; AI fills customer side
        self.running = True

        mic_info = _pa.get_device_info_by_index(mic_device_id)
        mic_rate = int(mic_info["defaultSampleRate"])
        mic_channels = int(mic_info["maxInputChannels"])

        def _mic_cb(in_data, frame_count, time_info, status):
            if self.running:
                converted = _gate_mic(_to_mono_16k(in_data, mic_rate, mic_channels))
                self._loop.call_soon_threadsafe(self.mic_queue.put_nowait, converted)
            return (None, pyaudio.paContinue)

        self._mic_stream = _pa.open(
            format=FORMAT,
            channels=mic_channels,
            rate=mic_rate,
            input=True,
            input_device_index=mic_device_id,
            frames_per_buffer=int(BLOCK_SIZE * mic_rate / SAMPLE_RATE),
            stream_callback=_mic_cb,
        )
        self._mic_stream.start_stream()

    def swap(self, mic_device_id: int, loopback_device_id: int):
        """Hot-swap audio streams without disrupting the queues."""
        if self._mic_stream:
            self._mic_stream.stop_stream()
            self._mic_stream.close()
            self._mic_stream = None
        if self._loopback_stream:
            self._loopback_stream.stop_stream()
            self._loopback_stream.close()
            self._loopback_stream = None

        mic_info = _pa.get_device_info_by_index(mic_device_id)
        mic_rate = int(mic_info["defaultSampleRate"])
        mic_channels = int(mic_info["maxInputChannels"])

        def _mic_cb(in_data, frame_count, time_info, status):
            if self.running:
                converted = _gate_mic(_to_mono_16k(in_data, mic_rate, mic_channels))
                self._loop.call_soon_threadsafe(self.mic_queue.put_nowait, converted)
            return (None, pyaudio.paContinue)

        lb_info = _pa.get_device_info_by_index(loopback_device_id)
        lb_rate = int(lb_info["defaultSampleRate"])
        lb_channels = int(lb_info["maxInputChannels"])

        def _loopback_cb(in_data, frame_count, time_info, status):
            if self.running:
                converted = _to_mono_16k(in_data, lb_rate, lb_channels)
                self._loop.call_soon_threadsafe(self.loopback_queue.put_nowait, converted)
            return (None, pyaudio.paContinue)

        self._mic_stream = _pa.open(
            format=FORMAT,
            channels=mic_channels,
            rate=mic_rate,
            input=True,
            input_device_index=mic_device_id,
            frames_per_buffer=int(BLOCK_SIZE * mic_rate / SAMPLE_RATE),
            stream_callback=_mic_cb,
        )
        self._loopback_stream = _pa.open(
            format=FORMAT,
            channels=lb_channels,
            rate=lb_rate,
            input=True,
            input_device_index=loopback_device_id,
            frames_per_buffer=int(BLOCK_SIZE * lb_rate / SAMPLE_RATE),
            stream_callback=_loopback_cb,
        )
        self._mic_stream.start_stream()
        self._loopback_stream.start_stream()

    def stop(self):
        self.running = False
        if self._mic_stream:
            self._mic_stream.stop_stream()
            self._mic_stream.close()
            self._mic_stream = None
        if self._loopback_stream:
            self._loopback_stream.stop_stream()
            self._loopback_stream.close()
            self._loopback_stream = None
        if self.mic_queue and self._loop:
            self._loop.call_soon_threadsafe(self.mic_queue.put_nowait, None)
        if self.loopback_queue and self._loop:
            self._loop.call_soon_threadsafe(self.loopback_queue.put_nowait, None)
