import sys
print("Python:", sys.version)

try:
    print("Testing pyaudiowpatch...")
    import pyaudiowpatch as pyaudio
    p = pyaudio.PyAudio()
    print(f"PyAudioWPatch OK — {p.get_device_count()} devices:")
    for i in range(p.get_device_count()):
        d = p.get_device_info_by_index(i)
        print(f"  {i}: in={d['maxInputChannels']} out={d['maxOutputChannels']} | {d['name']}")
    p.terminate()
except Exception as e:
    print(f"pyaudiowpatch failed: {e}")

try:
    print("\nTesting sounddevice...")
    import sounddevice as sd
    print("sounddevice imported OK")
    print(sd.query_devices())
except Exception as e:
    print(f"sounddevice failed: {e}")
