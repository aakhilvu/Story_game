# 📖 Story Game

An AI-powered interactive storytelling game where users can upload stories or PDFs, choose characters, and experience dynamic gameplay with AI-generated narratives and scene visuals.

---

## ✨ Features

- 📄 Upload custom stories or PDF files
- 🎭 Automatic character extraction from stories
- 👤 Choose and play as different characters
- 🤖 AI-generated dynamic story progression
- 🖼️ AI-generated scene images
- 🧠 Character memory and story context using RAG
- 💾 Save and load game sessions
- 📚 Story journal and progression tracking
- ⚔️ Difficulty selection system
- ❤️ Relationship and alignment system
- 🎨 Interactive and responsive UI

---

## 🛠️ Tech Stack

### Frontend
- HTML
- CSS
- JavaScript

### Backend
- Python
- FastAPI

### AI / APIs
- Groq API
- Pollinations AI

### Libraries
- Scikit-learn
- NumPy
- PyPDF2
- python-dotenv
- AnyIO

---

## 📂 Project Structure

```text
Story_game/
│
├── game.py                 # Main game logic
├── image_manager.py        # Image generation and management
├── main.py                 # FastAPI backend
├── session_store.py        # Session save/load handling
│
├── main.html               # Frontend UI
├── script.js               # Frontend functionality
├── style.css               # Styling
│
├── requirements.txt
├── README.md
└── .gitignore
```

---

## ⚙️ Installation

Clone the repository:

```bash
git clone https://github.com/aakhilvu/Story_game.git
```

Move into the project folder:

```bash
cd Story_game
```

Create a virtual environment:

```bash
python -m venv venv
```

Activate virtual environment:

Windows:

```bash
venv\Scripts\activate
```

Linux/Mac:

```bash
source venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

---

## 🔑 Environment Variables

Create a `.env` file in the root directory and add your API keys:

```env
GROQ_API_KEY_1=your_key_here

POLLINATIONS_KEY_1=your_key_here

```

---

## ▶️ Run the Project

Start the FastAPI server:

```bash
python -X utf8 -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Open your browser and visit:

```text
http://127.0.0.1:8000
```

---

## 🎮 How To Use

1. Launch the application
2. Upload a PDF or write a custom story
3. Let AI extract characters
4. Choose your character
5. Start the adventure
6. Make choices and influence the story

---


## 👨‍💻 Author

akhil

GitHub:
https://github.com/aakhilvu

---

## 📜 License

This project is for educational and personal learning purposes.
