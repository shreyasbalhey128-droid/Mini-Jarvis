# Mini-Jarvis 🦾

> Your offline, voice-controlled AI assistant for Windows. No cloud. No subscriptions. Just Jarvis.

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/Platform-Windows%2010%2F11-lightgrey?logo=windows&logoColor=white)](https://www.microsoft.com/windows)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)
[![Offline](https://img.shields.io/badge/Cloud-None%20%E2%9C%93-brightgreen)](#-privacy-first)

---

## 📸 Screenshots

![Mini-Jarvis UI](screenshot.png)
*The arc reactor interface with N.O.V.A. active — system stats, transcript, and voice controls.*

---

## ✨ Features

| Category | Details |
| --- | --- |
| **🧠 AI Brain** | Runs fully offline on **Ollama** (llama3.2:3b) — zero cloud dependency |
| **🔐 Voice Lock** | Responds **only to your voice** (Resemblyzer speaker verification) |
| **👥 6 AI Personas** | Each with unique voice, color, personality, and specialty |
| **👁️ Vision** | Screen reading + camera input + drag-and-drop image analysis |
| **💬 Messaging** | Reads/sends WhatsApp & Instagram via browser automation |
| **📅 Calendar** | Google Calendar integration (read-only iCal, offline cache) |
| **🎮 System Control** | Open/close apps, web search, volume control — all by voice |
| **⚡ Smart Power** | Sleep mode cuts CPU/RAM, interruptible with "stop" |
| **🧠 Memory** | Asks permission before saving — *"Should I remember that?"* |
| **🎨 UI** | Animated arc reactor, floating orb mode, real-time system monitor |

---

## 🎭 The Assistants

| Name | Color | Role |
| --- | --- | --- |
| **Echo** | 🔵 Blue | Daily driver — general tasks, conversation |
| **Nova** | 🟢 Green | Code help — programming, debugging, tech questions |
| **Aegis** | ⚪ Silver | System monitor & security — alerts, battery, performance |
| **Atlas** | 🟡 Gold | Heavy data & storage — file management, backups |
| **Vortex** | 🟣 Purple | Web gathering — research, news, information retrieval |
| **Silo** | 🔴 Red | *Kept in containment. Do not call him.* |

---

## 🚀 Quick Start

### Prerequisites

- Windows 10/11
- Python 3.10+
- [Ollama](https://ollama.com/) installed
- Microphone

### Installation

```bash
# 1. Pull the AI model
ollama pull llama3.2:3b

# 2. Clone the repo
git clone https://github.com/shreyasbalhey128-droid/Mini-Jarvis.git
cd Mini-Jarvis

# 3. Set up virtual environment
python -m venv venv
venv\Scripts\activate

# 4. Install dependencies
pip install -r requirements.txt
playwright install

# 5. Download models (not included in repo)
# - Vosk speech model: https://alphacephei.com/vosk/models
#   → vosk-model-small-en-us-0.15 (unzip to project folder)
# - Piper voices: place in `voices/` folder

# 6. Add your Google Calendar iCal link
#    (create a text file — see calendar_agent.py for filename)

# 7. Run!
python voice.py
```

**First launch:** Say *"register my voice"* to set up voice lock.

---

## 🗂️ Project Structure

```javascript
Mini-Jarvis/
├── voice.py              # Main entry point
├── assistants/           # AI persona definitions
├── core/                 # LLM, STT, TTS pipelines
│   ├── ollama_client.py  # Local LLM interface
│   ├── vosk_stt.py       # Offline speech-to-text
│   └── piper_tts.py      # Offline text-to-speech
├── agents/               # Functional modules
│   ├── calendar_agent.py # Google Calendar (iCal)
│   ├── vision_agent.py   # Screen + camera vision
│   ├── messaging_agent.py# WhatsApp/Instagram
│   └── system_agent.py   # App control, volume, etc.
├── memory/               # Long-term memory (ChromaDB)
├── ui/                   # Arc reactor interface
│   ├── reactor.py        # Main animation
│   └── orb_mode.py       # Floating orb
├── security/             # Voice lock, face data
└── data/                 # Runtime data (gitignored)
```

---

## 🔒 Privacy-First Design

| What | How |
| --- | --- |
| **Voice processing** | 100% local (Vosk + Piper) |
| **AI inference** | Local via Ollama — no API calls |
| **Voice prints** | Stored locally, never uploaded |
| **Memories** | Local ChromaDB, consent-based |
| **Browser sessions** | Isolated profile, stays on your machine |
| **Cloud services** | Zero. Nada. Zilch. |

---

## 🎮 Voice Commands

| Say... | Action |
| --- | --- |
| *"Open Chrome"* | Launch applications |
| *"Search for [query]"* | Web search |
| *"Volume up / down / mute"* | Audio control |
| *"What's on my screen?"* | Vision analysis |
| *"Read my messages"* | Check WhatsApp/Instagram |
| *"Stop"* | Interrupt current task |
| *"Go to sleep"* | Low-power mode |
| *"Register my voice"* | Set up voice lock |

---

## 🛠️ Tech Stack

| Component | Technology |
| --- | --- |
| **LLM** | Ollama (llama3.2:3b) |
| **Speech-to-Text** | Vosk (offline) |
| **Text-to-Speech** | Piper (offline) |
| **Speaker Verification** | Resemblyzer |
| **Vision** | Local vision models |
| **Memory** | ChromaDB (vector store) |
| **Browser Automation** | Playwright |
| **UI** | Python (arc reactor animation) |
| **Calendar** | iCal parsing (read-only) |

---

## ⚠️ Disclaimer

> This is a **personal project**. WhatsApp/Instagram features use browser automation on your own accounts — use at your own risk as platforms may restrict automated access. Your voice profile, memories, and login sessions stay on your machine and are **not** part of this repo.

---

## 🗺️ Roadmap

- [ ] Raspberry Pi port (home server mode)
- [ ] Advanced screen reading (OCR + context)
- [ ] Home Assistant integration
- [ ] Multi-language support
- [ ] Mobile companion app

---

## 🤝 Contributing

This is a personal learning project, but suggestions are welcome! Feel free to:

- Open an issue for bugs or ideas
- Fork and experiment
- Share your own Jarvis builds

---

## 📜 License

MIT License — see [LICENSE](LICENSE)

---

## 👤 Author

**Shreyas**

- GitHub: [@shreyasbalhey128-droid](https://github.com/shreyasbalhey128-droid)

---

> *"Sometimes you gotta run before you can walk."* — Tony Stark
