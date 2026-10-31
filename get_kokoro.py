"""
Downloads the Kokoro voice model into Jarvis's 'voices' folder (one-time, about 350 MB).

  python get_kokoro.py          full quality (recommended)
  python get_kokoro.py --small  smaller and faster (about 120 MB), slightly lower quality

Before the first run, install the package:   pip install kokoro-onnx
"""
import os
import sys
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
VOICES_DIR = os.path.join(BASE, "voices")
URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"


def download(name):
    dest = os.path.join(VOICES_DIR, name)
    if os.path.isfile(dest) and os.path.getsize(dest) > 1_000_000:
        print(f"[ok] {name} already there")
        return
    print(f"[+] downloading {name} ...")
    tmp = dest + ".part"

    def progress(blocks, size, total):
        if total > 0:
            done = min(blocks * size, total)
            print(f"\r    {done / 1e6:6.1f} / {total / 1e6:.1f} MB", end="", flush=True)

    urllib.request.urlretrieve(URL + name, tmp, progress)
    os.replace(tmp, dest)
    print(f"\n[ok] saved {name}")


def main():
    os.makedirs(VOICES_DIR, exist_ok=True)
    model = "kokoro-v1.0.int8.onnx" if "--small" in sys.argv else "kokoro-v1.0.onnx"
    download("voices-v1.0.bin")
    download(model)
    print("\nDone. Start Jarvis. The log will say 'Voice: Echo (Kokoro)'.")


if __name__ == "__main__":
    main()