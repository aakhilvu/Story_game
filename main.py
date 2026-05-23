import sys
# Force UTF-8 on Windows stdout/stderr so LLM-generated Unicode never crashes print().
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except AttributeError:
    pass

from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, Request
from fastapi.responses import StreamingResponse, FileResponse
import io
import json
import os
import asyncio
from fastapi.middleware.cors import CORSMiddleware
from PyPDF2 import PdfReader
from fastapi.staticfiles import StaticFiles
import uuid
from game import (
    start_game, take_action, stream_panels,
    extract_all_characters, extract_environment_profile,
    build_story_index, delete_story_index,
    chat_completion, GroqRateLimitError,
)
from image_manager import process_story_portraits_from_characters, delete_session_portraits, PORTRAITS_DIR
from session_store import save_session, load_session, list_saves, delete_save

app = FastAPI()

PORTRAITS_DIR.mkdir(exist_ok=True)
app.mount("/portraits", StaticFiles(directory=str(PORTRAITS_DIR)), name="portraits")

if os.path.isdir("images"):
    app.mount("/images", StaticFiles(directory="images"), name="images")


# ── Serve the frontend ────────────────────────────────────────────────────────
@app.get("/")
async def serve_index():
    return FileResponse("main.html")

@app.get("/script.js")
async def serve_script():
    return FileResponse("script.js", media_type="application/javascript")

@app.get("/style.css")
async def serve_style():
    return FileResponse("style.css", media_type="text/css")
# ─────────────────────────────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

sessions = {}


def summarize_story(text: str) -> str:
    """
    Called ONCE at upload time. Produces a compact ~200-word summary used as
    the system-prompt story context for every game turn.
    """
    sample = text[:8000]
    try:
        summary = chat_completion(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content":
                f"Summarize this story in 200 words. Cover: main characters, "
                f"their goals, key locations, central conflict, and tone. "
                f"Be specific -names, places, stakes. No fluff:\n\n{sample}"
            }],
            max_tokens=280,
            temperature=0.1,
        )
        print(f"  [main] Story summarized -{len(summary)} chars (was {len(sample)} chars)")
        return summary
    except GroqRateLimitError as e:
        print(f"  [main] Rate limit during summarization -using raw excerpt: {e}")
        return text[:1500]


def _run_portrait_pipeline(session_id: str, characters: list):
    """
    Background task: generates portraits for all characters.
    Checks if session still exists before each portrait to avoid wasting
    quota on abandoned sessions (FIX: goBack() now deletes the session).
    """
    try:
        result = process_story_portraits_from_characters(session_id, characters, sessions)
        print(f"[main] Portraits done -{len(result)} generated for session {session_id[:8]}")
        if session_id in sessions:
            sessions[session_id]["portraits"] = result
    except Exception as e:
        print(f"[main] Portrait pipeline error: {e}")


@app.post("/upload-story")
async def upload_story(
    background_tasks: BackgroundTasks,
    custom_text: str = Form(None),
    file: UploadFile = File(None)
):
    try:
        session_id = str(uuid.uuid4())

        if custom_text:
            story_text = custom_text
        elif file:
            contents = await file.read()
            pdf = PdfReader(io.BytesIO(contents))
            story_text = "".join(page.extract_text() or "" for page in pdf.pages)
        else:
            return {"error": "No story provided"}

        build_story_index(session_id, story_text)
        characters = extract_all_characters(story_text)

        loop = asyncio.get_event_loop()
        story_summary, env_profile = await asyncio.gather(
            loop.run_in_executor(None, summarize_story, story_text),
            loop.run_in_executor(None, extract_environment_profile, story_text),
        )

        sessions[session_id] = {
            # ── existing keys ──
            "story":          story_summary,
            "environment":    env_profile,
            "character":      {},
            "history":        [],
            "world_state":    {},

            # ── new keys ──
            "raw_story":      story_text,   # full original text (needed for save/load)
            "journal":        [],           # [{"turn": int, "text": str}, ...]
            "inventory":      [],           # list of item name strings
            "alignment":      0,            # int clamped to [-100, 100]; 0 = neutral
            "difficulty":     "normal",     # "easy" | "normal" | "hard"
            "relationships":  {},           # {"CharName": {"score": int, "label": str, "last_reason": str}}
            "endings_seen":   [],           # list of ending dicts (persists across playthroughs)
            "turn_count":     0,            # increments on every take_action call
            "all_characters": [],           # populated below
        }
        sessions[session_id]["all_characters"] = characters

        background_tasks.add_task(_run_portrait_pipeline, session_id, characters)

        return {
            "message":    "Story stored",
            "session_id": session_id,
            "characters": characters
        }
    except Exception as e:
        return {"error": str(e)}


@app.post("/select-character")
async def select_character(
    session_id: str = Form(...),
    character: str = Form(...),
    difficulty: str = Form("normal")
):
    try:
        if session_id not in sessions:
            return {"error": "Session not found."}
        sessions[session_id]["character"]          = json.loads(character)
        sessions[session_id]["history"]            = []
        sessions[session_id]["world_state"]        = {}
        sessions[session_id]["journal"]            = []
        sessions[session_id]["inventory"]          = []
        sessions[session_id]["alignment"]          = 0
        sessions[session_id]["turn_count"]         = 0
        sessions[session_id]["relationships"]      = {}
        sessions[session_id]["pending_narrative"]  = ""
        sessions[session_id]["last_choices"]       = []
        sessions[session_id]["difficulty"]         = difficulty if difficulty in ("easy", "normal", "hard") else "normal"
        # NOTE: endings_seen is intentionally NOT reset — it persists across playthroughs
        return {"message": "Character selected"}
    except Exception as e:
        return {"error": str(e)}


@app.post("/start-game")
async def start_game_endpoint(session_id: str = Form(...)):
    try:
        return start_game(session_id, sessions)
    except Exception as e:
        return {"error": str(e)}


@app.post("/take-action")
async def take_action_endpoint(session_id: str = Form(...), player_input: str = Form(...)):
    try:
        return take_action(session_id, player_input, sessions)
    except Exception as e:
        return {"error": str(e)}


@app.get("/stream-panels/{session_id}")
async def stream_panels_endpoint(request: Request, session_id: str):
    """
    SSE endpoint -generates one scene image per turn.
    FIX: Passes request object to check for client disconnect before expensive ops.
    """
    if session_id not in sessions:
        async def err():
            yield f"data: {json.dumps({'type': 'error', 'message': 'Session not found'})}\n\n"
        return StreamingResponse(err(), media_type="text/event-stream")

    character = sessions[session_id].get("character", {})
    narrative = sessions[session_id].get("pending_narrative", "")

    async def generate():
        try:
            # FIX: Check if client disconnected before starting expensive image generation
            if await request.is_disconnected():
                print(f"  [stream] Client disconnected before image gen for session {session_id[:8]}")
                return

            async for event in stream_panels(narrative, character, session_id, sessions):
                if await request.is_disconnected():
                    print(f"  [stream] Client disconnected mid-stream -aborting")
                    return
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )



@app.post("/save-game")
async def save_game_endpoint(session_id: str = Form(...)):
    try:
        if session_id not in sessions:
            existing = load_session(session_id)
            if existing:
                sessions[session_id] = existing
                sessions[session_id]["world_state"] = {}
            else:
                return {"error": "Session not found"}
        ok = save_session(session_id, sessions[session_id])
        return {"message": "Saved" if ok else "Save failed", "success": ok}
    except Exception as e:
        return {"error": str(e)}


@app.get("/list-saves")
async def list_saves_endpoint():
    try:
        return {"saves": list_saves()}
    except Exception as e:
        return {"error": str(e)}


@app.post("/load-game")
async def load_game_endpoint(session_id: str = Form(...)):
    """
    Load a saved session. Rebuilds the TF-IDF story index from raw_story.
    Returns the full session state so the frontend can restore itself.
    """
    try:
        data = load_session(session_id)
        if not data:
            return {"error": "Save not found"}

        raw_story = data.get("raw_story", "")
        if not raw_story:
            return {"error": "Save file is missing raw story text"}

        # Rebuild TF-IDF index
        build_story_index(session_id, raw_story)

        # Restore session into memory
        sessions[session_id] = data
        sessions[session_id]["world_state"] = {}   # reset non-serializable key

        return {
            "message":       "Loaded",
            "session_id":    session_id,
            "character":     data.get("character", {}),
            "characters":    data.get("all_characters", []),
            "turn_count":    data.get("turn_count", 0),
            "narrative":     data.get("pending_narrative", ""),
            "choices":       data.get("last_choices", []),
            "inventory":     data.get("inventory", []),
            "alignment":     data.get("alignment", 0),
            "difficulty":    data.get("difficulty", "normal"),
            "journal":       data.get("journal", []),
            "relationships": data.get("relationships", {}),
            "endings_seen":  data.get("endings_seen", []),
        }
    except Exception as e:
        return {"error": str(e)}


@app.delete("/delete-save/{session_id}")
async def delete_save_endpoint(session_id: str):
    try:
        ok = delete_save(session_id)
        return {"message": "Deleted" if ok else "Not found", "success": ok}
    except Exception as e:
        return {"error": str(e)}


@app.delete("/end-game/{session_id}")
async def end_game(session_id: str):
    try:
        delete_story_index(session_id)
        delete_session_portraits(session_id)
        if session_id in sessions:
            del sessions[session_id]
        return {"message": "Game ended"}
    except Exception as e:
        return {"error": str(e)}