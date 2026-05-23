"""
game.py — Unified game engine
Merges: groq_client, pollinations_queue, rag, character, queue_test (dropped), game
"""

import builtins
import os, re, json, time, random, threading, hashlib
import numpy as np
from dotenv import load_dotenv

def _p(*args, **kwargs):
    try:
        builtins.print(*args, **kwargs)
    except UnicodeEncodeError:
        safe = ' '.join(str(a).encode('ascii', 'replace').decode('ascii') for a in args)
        try:
            builtins.print(safe, **{k: v for k, v in kwargs.items() if k != 'file'})
        except Exception:
            pass
from groq import Groq, RateLimitError, APIStatusError
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from image_manager import generate_scene_image
import anyio

load_dotenv()

# ══════════════════════════════════════════════════════════════════════════════
# GROQ CLIENT
# ══════════════════════════════════════════════════════════════════════════════

_groq_keys: list[str] = []
for _i in range(1, 11):
    _k = os.getenv(f"GROQ_API_KEY_{_i}", "").strip()
    if _k:
        _groq_keys.append(_k)
_groq_legacy = os.getenv("GROQ_API_KEY", "").strip()
if _groq_legacy and _groq_legacy not in _groq_keys:
    _groq_keys.insert(0, _groq_legacy)

if _groq_keys:
    _p(f"[game] {len(_groq_keys)} Groq key(s) loaded")
else:
    _p("[game] WARNING: No Groq keys found. Add GROQ_API_KEY_1=xxx to .env")



RESET_WINDOW_SECONDS = 65
_groq_lock       = threading.Lock()
_groq_current    = 0
_exhausted_at: dict[int, float] = {}


class GroqRateLimitError(Exception):
    pass


def _groq_is_available(idx: int) -> bool:
    t = _exhausted_at.get(idx)
    if t is None:
        return True
    if time.time() - t >= RESET_WINDOW_SECONDS:
        del _exhausted_at[idx]
        return True
    return False


def _groq_next() -> tuple[str, int] | tuple[None, None]:
    global _groq_current
    for _ in range(len(_groq_keys)):
        idx = _groq_current % len(_groq_keys)
        if _groq_is_available(idx):
            return _groq_keys[idx], idx
        _groq_current += 1
    return None, None


def _groq_exhaust(idx: int):
    global _groq_current
    _exhausted_at[idx] = time.time()
    _groq_current = (idx + 1) % len(_groq_keys)


def chat_completion(
    model: str,
    messages: list[dict],
    max_tokens: int = 1000,
    temperature: float = 0.7,
    retries: int = 0,
    response_format: dict = None,
) -> str:
    if not _groq_keys:
        raise GroqRateLimitError("No Groq API keys configured.")
    if retries >= len(_groq_keys):
        raise GroqRateLimitError(f"All {len(_groq_keys)} Groq key(s) exhausted.")

    with _groq_lock:
        key, idx = _groq_next()
    if key is None:
        raise GroqRateLimitError(f"All {len(_groq_keys)} Groq key(s) rate-limited.")

    client = Groq(api_key=key)
    try:
        kwargs = dict(model=model, messages=messages, max_tokens=max_tokens, temperature=temperature)
        if response_format:
            kwargs["response_format"] = response_format
        resp = client.chat.completions.create(**kwargs)
        return resp.choices[0].message.content
    except (RateLimitError, APIStatusError) as e:
        if isinstance(e, APIStatusError) and e.status_code != 429:
            raise
        with _groq_lock:
            _groq_exhaust(idx)
        time.sleep(0.5)
        return chat_completion(model, messages, max_tokens, temperature, retries + 1, response_format)





# ══════════════════════════════════════════════════════════════════════════════
# RAG — TF-IDF story index
# ══════════════════════════════════════════════════════════════════════════════

story_indexes: dict = {}


def build_story_index(session_id: str, full_text: str):
    chunk_size, overlap = 600, 100
    chunks, i = [], 0
    while i < len(full_text):
        chunk = full_text[i:i + chunk_size]
        if chunk.strip():
            chunks.append(chunk)
        i += chunk_size - overlap
    vectorizer = TfidfVectorizer(stop_words='english')
    matrix     = vectorizer.fit_transform(chunks)
    story_indexes[session_id] = {"chunks": chunks, "vectorizer": vectorizer, "matrix": matrix}
    _p(f"  [rag] {len(chunks)} chunks built for session {session_id[:8]}")
    return len(chunks)





def get_character_first_scene(session_id: str, char_name: str, context_chunks: int = 2) -> str:
    if session_id not in story_indexes:
        return ""
    chunks     = story_indexes[session_id]["chunks"]
    name_lower = char_name.lower().strip()
    parts      = name_lower.split()
    searches   = [name_lower]
    if parts and len(parts[0]) > 3:
        searches.append(parts[0])
    if len(parts) > 1 and len(parts[-1]) > 3:
        searches.append(parts[-1])
    for term in searches:
        for i, chunk in enumerate(chunks):
            cl = chunk.lower()
            if f" {term}" in f" {cl}" or f"\n{term}" in f"\n{cl}":
                return "\n\n---\n\n".join(chunks[i:i + context_chunks])
    result = get_relevant_chunks(session_id, char_name, n_results=2)
    return result if result else "\n\n---\n\n".join(chunks[:2])


def get_relevant_chunks(session_id: str, query: str, n_results: int = 2) -> str:
    if session_id not in story_indexes:
        return ""
    idx  = story_indexes[session_id]
    qvec = idx["vectorizer"].transform([query])
    sims = cosine_similarity(qvec, idx["matrix"]).flatten()
    tops = np.argsort(sims)[::-1][:n_results]
    hits = [idx["chunks"][i] for i in tops if sims[i] > 0]
    return "\n\n---\n\n".join(hits)


def delete_story_index(session_id: str):
    story_indexes.pop(session_id, None)


# ══════════════════════════════════════════════════════════════════════════════
# CHARACTER — extraction & prompt builders
# ══════════════════════════════════════════════════════════════════════════════

def _parse_json(text: str) -> list:
    text = text.replace("```json", "").replace("```", "").strip()
    for attempt in [
        lambda t: json.loads(t),
        lambda t: json.loads(t[t.find('['):t.rfind(']') + 1]),
        lambda t: json.loads(re.sub(r',\s*([}\]])', r'\1', t)[t.find('['):t.rfind(']') + 1]),
    ]:
        try:
            r = attempt(text)
            if isinstance(r, list):
                return r
        except Exception:
            pass
    return []


def _assemble_appearance(char: dict) -> str:
    parts = [char.get(f, "").strip() for f in ("hair", "face", "body", "clothing")
             if char.get(f, "").strip() and char.get(f, "").lower() != "unspecified"]
    return ", ".join(parts) if parts else char.get("description", "")


def build_character_image_prompt(char: dict) -> str:
    name  = char.get("name", "the character")
    parts = [char.get(f, "").strip() for f in ("hair", "face", "body", "clothing")
             if char.get(f, "").strip() and char.get(f, "").lower() != "unspecified"]
    if not parts:
        parts = [char.get("appearance") or char.get("description", "appearance unspecified")]
    return f"{name}: {', '.join(parts)}"





def build_environment_image_prompt(env: dict) -> str:
    parts = [env.get(k, "").strip() for k in
             ("world_type", "architecture", "lighting", "time_period", "color_palette", "atmosphere")
             if env.get(k, "").strip() and env.get(k, "").lower() != "unspecified"]
    for key, default in [
        ("art_style",      "detailed anime illustration, Studio Ghibli aesthetic"),
        ("lighting_style", "soft cinematic lighting"),
        ("detail_style",   "richly detailed background, painterly environment, lush scenery, vivid colors"),
    ]:
        v = env.get(key, default).strip()
        if v:
            parts.append(v)
    return ", ".join(parts) if parts else (
        "interior setting, soft cinematic lighting, detailed anime illustration, Studio Ghibli aesthetic, "
        "richly detailed background, painterly environment, vivid colors"
    )


def build_character_prompt(character: dict) -> str:
    skills     = ", ".join(character.get("skills", []))
    weaknesses = ", ".join(character.get("weaknesses", []))
    name       = character.get("name", "The Hero")
    return f"""
--- THE PLAYER'S CHARACTER ---
Name: {name}
Side: {character.get("side", "neutral")}
Skills: {skills}
Weaknesses: {weaknesses}
Description: {character.get("description", "")}
Appearance: {character.get("appearance", "")}
--- END CHARACTER ---

IMPORTANT CHARACTER RULES:
- Always refer to the player as "{name}"
- Use the character's skills naturally in relevant situations
- The character's weaknesses should create interesting challenges
- Frame all choices from this character's perspective
- If the character is a villain, reflect villain motivations in choices
- The player IS this character
"""


def extract_all_characters(story_text: str) -> list:
    length  = len(story_text)
    samples = []
    for i in range(9):
        pos   = int(i * length / 8)
        chunk = story_text[max(0, pos - 1000): pos + 1000]
        if chunk.strip():
            samples.append(chunk)
    sample_text = "\n\n--- NEXT SECTION ---\n\n".join(samples)

    try:
        raw = chat_completion(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": f"""Read these story excerpts and extract every character who matters.

INCLUDE: Main hero, villain, companions, mentors, rivals, family central to the plot.
EXCLUDE: One-off background characters.

Return ONLY a JSON array:
[
    {{
        "name": "Full Name",
        "skills": ["skill1"],
        "weaknesses": ["weakness1"],
        "side": "good or evil or neutral",
        "description": "One sentence role description",
        "appearance": "cohesive comma-separated visual traits",
        "face": "facial features",
        "body": "body/build",
        "hair": "color, length, texture",
        "clothing": "clothing and accessories"
    }}
]

STORY EXCERPTS:
{sample_text}"""}],
            max_tokens=4000
        )
    except GroqRateLimitError as e:
        _p(f"[game] Groq rate limit during character extraction: {e}")
        raw = "[]"

    characters = _parse_json(raw.strip())
    if characters:
        for char in characters:
            if not char.get("appearance"):
                char["appearance"] = _assemble_appearance(char)
            char["image_prompt"] = build_character_image_prompt(char)
        return characters

    return [{"name": "The Hero", "skills": ["courage"], "weaknesses": ["overconfidence"],
             "side": "good", "description": "The main hero of the story",
             "appearance": "young adult, athletic build, determined expression, practical clothing",
             "face": "unspecified", "body": "young adult, athletic build",
             "hair": "unspecified", "clothing": "practical clothing",
             "image_prompt": "The Hero: young adult, athletic build, practical clothing"}]


def extract_environment_profile(story_text: str) -> dict:
    length  = len(story_text)
    samples = [story_text[:2000],
               story_text[length // 3: length // 3 + 2000],
               story_text[2 * length // 3: 2 * length // 3 + 1500]]
    sample_text = "\n\n---\n\n".join(s for s in samples if s.strip())

    try:
        raw_env = chat_completion(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": f"""Extract environment/art-style details for image generation.

Return ONLY a JSON object with keys:
world_type, primary_location, architecture, lighting, atmosphere, time_period,
color_palette, art_style, lighting_style, detail_style

art_style must be ONE of: 'cinematic digital painting', 'painterly oil illustration',
'noir ink illustration', 'clean graphic novel art', 'photorealistic render',
'watercolor illustration', 'gritty concept art', 'anime illustration', 'retro pulp illustration'

STORY EXCERPTS:
{sample_text}"""}],
            max_tokens=900
        )
    except GroqRateLimitError as e:
        _p(f"[game] Groq rate limit during env extraction: {e}")
        raw_env = "{}"

    raw = raw_env.strip().replace("```json", "").replace("```", "").strip()
    try:
        env = json.loads(raw)
    except Exception:
        try:
            s, e2 = raw.find('{'), raw.rfind('}') + 1
            env = json.loads(raw[s:e2]) if s != -1 else {}
        except Exception:
            env = {}

    # Always force anime style regardless of story genre
    env["art_style"] = "detailed anime illustration, Studio Ghibli aesthetic"
    env["image_prompt"] = build_environment_image_prompt(env)
    _p(f"  [Environment] {env.get('world_type','?')} | {env.get('art_style','?')}")
    return env


# ══════════════════════════════════════════════════════════════════════════════
# GAME ENGINE
# ══════════════════════════════════════════════════════════════════════════════

FAST_MODEL = "llama-3.1-8b-instant"

SYSTEM_PROMPT_TEMPLATE = """
You are a game master for a VISUAL interactive story game.

CRITICAL: This is an ORIGINAL, UNKNOWN story. You have ZERO knowledge about it outside the text provided below. Do NOT use any external franchise knowledge, existing books, movies, or pop culture. Everything must come from the story text given to you.

STORY WORLD:
{story}

{character_section}

NARRATIVE FOCUS — THIS IS THE MOST IMPORTANT RULE:
- This game follows the MAIN STORYLINE only. Every scene must be a KEY moment in the central plot.
- BANNED: trivial daily-life scenes, filler moments, unrelated side activities, mundane routines.
- BANNED: scenes that have nothing to do with the core conflict, quest, or character arc.
- REQUIRED: every scene must be directly connected to the main plot — the central conflict, a key event, a critical relationship, or a turning point.
- If the player's choice would lead off the main story, redirect them back naturally within the scene.
- Think: "Is this scene something that matters to the story's main arc?" If no — rewrite it.

CRITICAL RULES:
- The story world, its characters, locations, and main plot NEVER change
- Always refer to the player by their character name
- When the player makes a choice, show the DIRECT consequence of that exact choice
- ALWAYS keep the narrative pushing forward toward key story events
- NEVER reference events, places, or characters that haven't happened yet in the game
- NEVER write choices that require knowing how the story ends
- Choices must only reference things the character can currently see, hear, or do RIGHT NOW
- NO external franchise knowledge — the story text provided above is your ONLY source of truth

RESPONSE FORMAT — follow this EXACTLY every time:

[SCENE: Exactly 2-3 SHORT, vivid sentences. This is a VISUAL game — write like a movie caption, not a novel.
Focus on: WHERE the character is right now + WHAT they are doing + ONE strong sensory detail.
Be specific and punchy. No rambling. No inner monologue. Pure visual action.]

CHOICES:
1. [short action phrase]
2. [short action phrase]
3. [short action phrase]
4. [short action phrase]

STRICT RULES:
- Maximum 3 sentences in the SCENE description.
- Every sentence must add a NEW visual or sensory detail.
- No filler phrases like "you find yourself" or "you can't help but".
- Start with immediate action or vivid location detail.
- CHOICES must be 5-10 words, start with an action verb, and reference SPECIFIC elements
  (characters, objects, locations) visible in the current scene. They must feel like real, meaningful decisions.
  Good examples: "Confront the captain about the可疑 plan", "Follow the old woman into the dark corridor", "Warn the guard about the approaching danger"
  Bad examples: "Look around carefully", "Talk to someone nearby", "Take immediate action", "Do something"
- Every choice must lead somewhere different — no two choices should feel like the same action.
- At least 2 of the 4 choices must clearly advance the MAIN PLOT.
- Choices must only reference what is immediately visible or possible in the current scene.
- Rely ONLY on the provided story text. Do NOT use external knowledge from books, movies, or any known franchises.
"""


def _build_char_anchor(character: dict) -> str:
    parts = [f"Character: {character.get('name', 'the hero')}"]
    for field, label in [("appearance","Overall"),("face","Face"),("hair","Hair"),("body","Body"),("clothing","Clothing")]:
        val = character.get(field, "").strip()
        if val and val.lower() not in ("unspecified", "", "none"):
            parts.append(f"{label}: {val}")
    if not any(character.get(f) for f in ("appearance", "face", "hair")):
        desc = character.get("description", "")
        if desc:
            parts.append(f"Description: {desc}")
    return " | ".join(parts)


def build_image_prompt(scene_text: str, char_anchor: str, env_prompt: str = "",
                       all_characters: list = None, player_name: str = "") -> str:
    # Extract player character name + key visual traits
    char_name = ""
    body_look = ""
    other_look = ""
    for part in char_anchor.split(" | "):
        if part.startswith("Character:"):
            char_name = part.replace("Character:", "").strip()
        elif part.startswith("Body:"):
            body_look = part.split(":", 1)[-1].strip()
        elif any(part.startswith(lbl) for lbl in ("Overall:", "Hair:", "Face:", "Clothing:")):
            other_look += part.split(":", 1)[-1].strip() + ", "
    other_look = other_look.rstrip(", ")
    char_look = ", ".join(p for p in [body_look, other_look] if p)

    # Use first 2 sentences so secondary characters mentioned later aren't dropped
    scene_clean = scene_text.replace("\n", " ").strip()
    sentences = re.split(r'(?<=[.!?])\s+', scene_clean)
    scene_excerpt = " ".join(sentences[:2]).strip()
    if len(scene_excerpt) < 20:
        scene_excerpt = scene_clean[:280]
    elif len(scene_excerpt) > 280:
        scene_excerpt = scene_excerpt[:280]

    # Pull world/location from env prompt
    env_tag = ""
    if env_prompt:
        for token in env_prompt.split(","):
            token = token.strip()
            if token and not any(kw in token.lower() for kw in ("anime", "ghibli", "illustration", "style", "painting", "lighting", "proportions", "expressions", "ambient", "detail")):
                env_tag = token
                break

    if env_tag:
        scene_block = f"{scene_excerpt} {env_tag}."
    else:
        scene_block = scene_excerpt

    # Build character anchors: player first, then any other named characters in the scene
    # Each anchor is (name, compact_look_string) so we can format them clearly
    char_anchors = []  # list of (name, look)
    if char_name:
        char_anchors.append((char_name, char_look))

    if all_characters:
        scene_lower = scene_clean.lower()
        for c in all_characters:
            name = c.get("name", "").strip()
            if not name or name.lower() == (player_name or char_name).lower():
                continue
            if re.search(r'\b' + re.escape(name.lower()) + r'\b', scene_lower):
                anchor = _build_char_anchor(c)
                c_body = ""
                c_other = ""
                for part in anchor.split(" | "):
                    if part.startswith("Body:"):
                        c_body = part.split(":", 1)[-1].strip()
                    elif any(part.startswith(lbl) for lbl in ("Overall:", "Hair:", "Face:", "Clothing:")):
                        c_other += part.split(":", 1)[-1].strip() + ", "
                c_other = c_other.rstrip(", ")
                c_look = ", ".join(p for p in [c_body, c_other] if p)
                char_anchors.append((name, c_look))
                if len(char_anchors) >= 3:
                    break

    char_block = ""
    if len(char_anchors) == 1:
        name, look = char_anchors[0]
        char_block = f"featuring {name} ({look})" if look else f"featuring {name}"
    elif len(char_anchors) >= 2:
        # Explicitly label each character as distinct to prevent the model blending their appearances
        parts_chars = []
        for name, look in char_anchors:
            parts_chars.append(f"{name}: {look}" if look else name)
        char_block = (
            f"{len(char_anchors)} distinct characters, each with a unique appearance — "
            + "; ".join(parts_chars)
        )

    style_block = "detailed anime illustration, Studio Ghibli aesthetic, painterly cel shading, vivid cinematic colors"

    quality_block = (
        "wide establishing shot, full scene visible, all characters from the scene visible, "
        "rich detailed background, dynamic composition, masterpiece, best quality, extremely detailed, sharp focus"
    )

    # Anatomical safety negatives; also reinforce character distinction when multiple chars present
    if len(char_anchors) >= 2:
        negative_block = (
            "avoid: same face on both characters, identical hair on both characters, "
            "cloned appearance, extra limbs, extra hands, extra fingers, fused fingers, "
            "multiple heads, deformed anatomy, mutated, malformed, disfigured, bad proportions"
        )
    else:
        negative_block = (
            "avoid: extra limbs, extra hands, extra fingers, fused fingers, multiple heads, "
            "deformed anatomy, mutated, malformed, disfigured, bad proportions"
        )

    parts = [scene_block, char_block, style_block, quality_block, negative_block]

    prompt = ". ".join(p for p in parts if p)[:900]
    _p(f"  [image_prompt] {prompt[:160].encode('ascii', 'replace').decode('ascii')}...")
    return prompt


def parse_response(text: str):
    choices, narrative = [], text

    # Split on CHOICES: label (case-insensitive)
    idx = text.upper().find('CHOICES:')
    if idx != -1:
        narrative     = text[:idx].strip()
        choices_block = text[idx:]
    else:
        choices_block = text

    # Extract numbered choices from the choices block
    for line in choices_block.split('\n'):
        line = line.strip()
        if line and len(line) > 2 and line[0].isdigit():
            for sep in ['. ', ') ', ': ']:
                if sep in line:
                    choice = line.split(sep, 1)[1].strip()
                    if choice:
                        choices.append(choice)
                    break

    # Clean scene markers from narrative — extract content between [SCENE: ... ]
    match = re.match(r'^\s*\[SCENE:\s*(.*?)\]\s*', narrative, re.DOTALL | re.IGNORECASE)
    if match:
        narrative = match.group(1)
    narrative = narrative.replace("**SCENE:**", "").replace("**", "").strip()

    # Strip numbered-list lines the LLM leaked into the narrative block
    # e.g. "1. Go left", "2) Talk to Ron", "3: Run away"
    narrative = re.sub(r'(?m)^\s*\d+[.):\s]\s*\S.+$', '', narrative)
    narrative = re.sub(r'\n{3,}', '\n\n', narrative).strip()

    return narrative, choices


def _strip_choices(text: str) -> str:
    idx = text.upper().find('CHOICES:')
    return text[:idx].strip() if idx != -1 else text


def build_rag_context(session_id: str, query: str) -> str:
    chunks = get_relevant_chunks(session_id, query, n_results=2)
    return f"--- RELEVANT STORY PASSAGES ---\n{chunks}\n--- END PASSAGES ---\n" if chunks else ""


def get_llm_response(session_id: str, player_input: str, sessions: dict, is_start: bool = False):
    story             = sessions[session_id]["story"]
    character         = sessions[session_id].get("character", {})
    character_section = build_character_prompt(character) if character else ""
    char_name         = character.get("name", "the hero")
    char_side         = character.get("side", "neutral")
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(story=story, character_section=character_section)

    # Inject difficulty into system prompt
    difficulty = sessions[session_id].get("difficulty", "normal")
    diff_text  = {
        "easy":   (
            "\n\nDIFFICULTY: EASY\n"
            "- Be lenient. Bad choices cause minor, easily-recoverable setbacks.\n"
            "- Choices should feel manageable — no choice permanently derails the story.\n"
            "- Positive outcomes are more likely. Consequences are softened.\n"
            "- The narrative favors the player's success."
        ),
        "normal": (
            "\n\nDIFFICULTY: NORMAL\n"
            "- Balance consequences naturally with player choices.\n"
            "- Good choices yield proportional rewards; bad choices yield proportional setbacks.\n"
            "- The narrative is neutral — neither favoring nor punishing the player."
        ),
        "hard":   (
            "\n\nDIFFICULTY: HARD\n"
            "- Be strict. Bad choices have harsh, lasting consequences.\n"
            "- The world is unforgiving. Every choice matters — poor decisions can close off paths permanently.\n"
            "- Choices should feel weighty and consequential. No easy way out.\n"
            "- Negative outcomes are more likely for careless play. Mistakes are costly."
        ),
    }.get(difficulty, "")
    system_prompt += diff_text

    # Build inventory context string
    inventory = sessions[session_id].get("inventory", [])
    inventory_context = ""
    if inventory:
        inventory_context = f"\nYOUR INVENTORY: {json.dumps(inventory)}\nYou are carrying these items. Use them, reference them, and let them affect the scene when appropriate.\n"

    if is_start:
        first_scene_text = get_character_first_scene(session_id, char_name)
        rag_context = (
            f"--- SOURCE PASSAGE INVOLVING {char_name.upper()} ---\n"
            f"{first_scene_text}\n"
            f"--- END ---\n"
            if first_scene_text else ""
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": (
                f"You are starting the game with the player controlling {char_name} ({char_side}).\n"
                f"\nSTORY SUMMARY:\n{story}\n"
                f"\n{rag_context}\n"
                f"CRITICAL INSTRUCTION: Open at a KEY MOMENT in the main plot involving {char_name}. "
                f"Use the STORY SUMMARY above to identify a pivotal scene from the central storyline. "
                f"DO NOT invent a random or mundane scene. The scene must be directly tied to the main conflict, "
                f"a major turning point, or a critical character moment from the story. "
                f"This is an ORIGINAL story — do NOT use any external franchise knowledge. "
                f"Characters, locations, and events from books, movies, or pop culture do NOT exist in this world.\n"
                f"Rewrite this scene from {char_name}'s perspective.\n"
                f"Write EXACTLY 2-3 short vivid sentences for the scene. Pure visual scene-setting, like a movie intro.\n"
                f"Then output EXACTLY 4 numbered choices. Rules: 5-10 words each, start with a verb, "
                f"reference SPECIFIC people/objects/places from the scene, each choice leads somewhere different. "
                f"Every choice must have MEANINGFUL CONSEQUENCES for the main plot — no filler options. NO generic choices."
            )}
        ]
    else:
        rag_context     = build_rag_context(session_id, player_input)
        trimmed_history = sessions[session_id]["history"][-6:]
        messages        = [{"role": "system", "content": system_prompt}]
        messages       += trimmed_history
        messages.append({"role": "user", "content": (
            f"{rag_context}\n"
            f"PREVIOUS SCENE: \"{sessions[session_id].get('pending_narrative', '')}\"\n\n"
            f"THE PLAYER'S ACTION: \"{player_input[:200]}\"\n\n"
            f"{inventory_context}\n"
            f"CRITICAL INSTRUCTIONS — FOLLOW EVERY RULE:\n"
            f"1. The new scene MUST show the DIRECT consequence of EVERY part of the player's action. "
            f"If they say \"do X and Y\", show BOTH X and Y happening.\n"
            f"2. Stay in the EXACT SAME location as the previous scene. Do NOT teleport.\n"
            f"3. Do NOT introduce any new characters, objects, or locations that were not already in the previous scene.\n"
            f"4. Do NOT use ANY external knowledge from books, movies, or franchises. "
            f"This is an original story — act like you have never heard of Harry Potter, Star Wars, or any known IP.\n"
            f"5. If the previous scene is in a forest, the new scene is also in that forest. "
            f"If it was at a house, stay at that house. Keep the setting identical.\n\n"
            f"Write EXACTLY 2-3 SHORT vivid sentences showing what happens right after this action.\n"
            f"Then output EXACTLY 4 numbered choices. Rules: 5-10 words each, start with a verb, "
            f"reference SPECIFIC people/objects/places from this new scene, each choice leads somewhere different. "
            f"Every choice must have MEANINGFUL CONSEQUENCES — no filler, no safe options that lead nowhere. NO generic choices."
        )})

    temp = 0.5 if is_start else 0.3
    try:
        reply = chat_completion(model=FAST_MODEL, messages=messages, max_tokens=420, temperature=temp)
    except GroqRateLimitError as e:
        raise RuntimeError(f"All Groq keys are rate-limited: {e}")

    narrative, choices = parse_response(reply)

    if not choices:
        retry_messages = messages + [
            {"role": "assistant", "content": reply},
            {"role": "user", "content": (
                "You forgot to include the CHOICES block. "
                "Output ONLY this section now — do NOT repeat the scene.\n"
                "Rules: 5-10 words, start with a verb, SPECIFIC to scene elements, each leads somewhere different:\n\n"
                "CHOICES:\n1. [specific action]\n2. [specific action]\n3. [specific action]\n4. [specific action]"
            )}
        ]
        try:
            choices_reply = chat_completion(model=FAST_MODEL, messages=retry_messages, max_tokens=120, temperature=0.5)
            _, choices = parse_response("CHOICES:\n" + choices_reply)
        except Exception:
            choices = ["Look around carefully", "Talk to someone nearby",
                       "Stay silent and observe", "Take immediate action"]

    return narrative, choices, reply


async def stream_panels(narrative: str, character: dict, session_id: str, sessions: dict):
    env_prompt    = sessions[session_id].get("environment", {}).get("image_prompt", "")
    all_characters = sessions[session_id].get("all_characters", [])
    char_anchor   = _build_char_anchor(character)
    char_name     = character.get("name", "")
    image_prompt  = build_image_prompt(narrative, char_anchor, env_prompt,
                                       all_characters=all_characters, player_name=char_name)
    # Mix scene content into seed so composition varies per scene while keeping character traits stable
    char_seed = int(hashlib.sha1(f"{session_id}:{char_name}:{narrative[:120]}".encode()).hexdigest(), 16) % 999999 + 1

    try:
        b64 = await anyio.to_thread.run_sync(
            lambda: generate_scene_image(image_prompt, portrait_path=None, seed=char_seed)
        )
        if b64:
            yield {"type": "grid", "data": b64}
        else:
            yield {"type": "grid_skipped"}
    except Exception as e:
        yield {"type": "image_error", "message": str(e)}

    yield {"type": "done"}


def start_game(session_id: str, sessions: dict):
    if session_id not in sessions:
        return {"error": f"Session '{session_id}' not found."}
    narrative, choices, reply = get_llm_response(session_id, "", sessions, is_start=True)
    character = sessions[session_id].get("character", {})
    char_name = character.get("name", "the hero")
    sessions[session_id]["history"] = [
        {"role": "user",      "content": f"Start the game as {char_name}."},
        {"role": "assistant", "content": _strip_choices(reply)}
    ]
    sessions[session_id]["pending_narrative"] = narrative
    sessions[session_id]["last_choices"]       = choices
    return {"session_id": session_id, "narrative": narrative, "choices": choices}


def extract_turn_metadata(session_id: str, narrative: str, player_input: str, sessions: dict) -> dict:
    """
    Single LLM call that extracts ALL per-turn metadata at once:
    journal entry, inventory delta, alignment delta, relationship deltas, ending detection.
    Run in parallel with get_llm_response() via ThreadPoolExecutor.
    """
    session       = sessions[session_id]
    inventory     = session.get("inventory", [])
    relationships = session.get("relationships", {})
    all_chars     = [c.get("name", "") for c in session.get("all_characters", []) if c.get("name")]
    difficulty    = session.get("difficulty", "normal")

    diff_instruction = {
        "easy":   "Be lenient. Bad choices have minor, easily-recoverable consequences. Lean toward positive outcomes. Favor the player.",
        "normal": "Balance consequences naturally with player choices. Good choices reward, bad choices penalize proportionally.",
        "hard":   "Be strict. Bad choices have serious, lasting consequences. Mistakes are costly. The narrative reflects poor decisions harshly.",
    }.get(difficulty, "Balance consequences naturally.")

    prompt = f"""Scene: {narrative}
Player action: {player_input}
Current inventory: {json.dumps(inventory)}
All story characters: {json.dumps(all_chars)}
Difficulty: {difficulty}. {diff_instruction}

Return ONLY a valid JSON object with these exact keys. No extra text, no markdown:
{{
  "journal": "one past-tense sentence summarizing what happened. Start with a verb. Include names and specific actions.",
  "inventory_add": [],
  "inventory_remove": [],
  "alignment_delta": 0,
  "relationships": {{}},
  "is_ending": false,
  "ending_type": null,
  "ending_summary": null
}}

Rules:
- journal: exactly one sentence, past tense, specific
- inventory_add: if the character TAKES, PICKS UP, RECEIVES, or is GIVEN something this scene (2-5 word names only). Do NOT add items the character already has.
- inventory_remove: if the character GIVES AWAY, LEAVES BEHIND, LOSES, CONSUMES, or DROPS an item this scene. Only remove items currently in the inventory.
- alignment_delta: integer -15 to +15. +15=deeply heroic, 0=neutral, -15=deeply villainous
- relationships: only include characters who appeared in this scene.
  Format: {{"CharName": {{"delta": <-10 to 10>, "reason": "one short phrase"}}}}
- is_ending: true only if the story has clearly concluded (protagonist died, final conflict resolved, explicit ending)
- ending_type: one of "victory", "defeat", "sacrifice", "betrayal", "escape", "ambiguous" or null
- ending_summary: one sentence describing the ending, or null
"""

    try:
        raw = chat_completion(
            model=FAST_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=400,
            temperature=0.3,
        )
        raw = raw.strip().replace("```json", "").replace("```", "").strip()
        result = json.loads(raw)
        return {
            "journal":          str(result.get("journal", "")),
            "inventory_add":    result.get("inventory_add", []) if isinstance(result.get("inventory_add"), list) else [],
            "inventory_remove": result.get("inventory_remove", []) if isinstance(result.get("inventory_remove"), list) else [],
            "alignment_delta":  int(result.get("alignment_delta", 0)),
            "relationships":    result.get("relationships", {}) if isinstance(result.get("relationships"), dict) else {},
            "is_ending":        bool(result.get("is_ending", False)),
            "ending_type":      result.get("ending_type"),
            "ending_summary":   result.get("ending_summary"),
        }
    except Exception as e:
        _p(f"  [metadata] extraction failed: {e}")
        return {
            "journal": "", "inventory_add": [], "inventory_remove": [],
            "alignment_delta": 0, "relationships": {},
            "is_ending": False, "ending_type": None, "ending_summary": None,
        }


def _relationship_label(score: int) -> str:
    if score >= 60:  return "Trusted Ally"
    if score >= 20:  return "Friend"
    if score >= -20: return "Acquaintance"
    if score >= -60: return "Rival"
    return "Enemy"


def apply_turn_metadata(session_id: str, metadata: dict, sessions: dict) -> dict:
    """
    Apply the extracted metadata dict to the session.
    Returns a summary dict to include in the take_action response.
    """
    session = sessions[session_id]

    # Journal
    if metadata["journal"]:
        session["journal"].append({
            "turn": session["turn_count"],
            "text": metadata["journal"],
        })

    # Inventory
    current_inv = session["inventory"]
    for item in metadata["inventory_add"]:
        if item and item not in current_inv:
            current_inv.append(item)
    for item in metadata["inventory_remove"]:
        if item in current_inv:
            current_inv.remove(item)
    session["inventory"] = current_inv

    # Alignment
    old_align = session["alignment"]
    new_align = max(-100, min(100, old_align + metadata["alignment_delta"]))
    session["alignment"] = new_align

    # Relationships
    for char_name, data in metadata["relationships"].items():
        if not isinstance(data, dict):
            continue
        delta  = int(data.get("delta", 0))
        reason = str(data.get("reason", ""))
        if char_name not in session["relationships"]:
            session["relationships"][char_name] = {"score": 0, "label": "Acquaintance", "last_reason": ""}
        old_score = session["relationships"][char_name]["score"]
        new_score = max(-100, min(100, old_score + delta))
        session["relationships"][char_name]["score"]       = new_score
        session["relationships"][char_name]["last_reason"] = reason
        session["relationships"][char_name]["label"]       = _relationship_label(new_score)

    # Endings
    ending_info = None
    if metadata["is_ending"] and metadata["ending_type"]:
        summary_text = metadata["ending_summary"] or ""
        fingerprint  = hashlib.sha1((metadata["ending_type"] + summary_text[:100]).encode()).hexdigest()[:16]
        existing_fps = [e.get("fingerprint") for e in session["endings_seen"]]
        ending_info  = {
            "type":        metadata["ending_type"],
            "summary":     summary_text,
            "alignment":   new_align,
            "turn_count":  session["turn_count"],
            "fingerprint": fingerprint,
            "is_new":      fingerprint not in existing_fps,
        }
        if ending_info["is_new"]:
            session["endings_seen"].append(ending_info)

    return {
        "journal_entry": metadata["journal"],
        "inventory":     session["inventory"],
        "alignment":     new_align,
        "relationships": session["relationships"],
        "ending":        ending_info,
    }


def take_action(session_id: str, player_input: str, sessions: dict):
    if session_id not in sessions:
        return {"error": "Session not found."}
    if not sessions[session_id].get("history"):
        return {"error": "Game not started."}

    player_input = player_input.strip()[:200]

    # Run narrative generation and metadata extraction in parallel
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        narrative_future = executor.submit(get_llm_response, session_id, player_input, sessions)
        metadata_future  = executor.submit(
            extract_turn_metadata, session_id,
            sessions[session_id].get("pending_narrative", ""),
            player_input, sessions
        )
        narrative, choices, reply = narrative_future.result()
        metadata = metadata_future.result()

    # Increment turn count
    sessions[session_id]["turn_count"] += 1

    # Apply history
    sessions[session_id]["history"].append({"role": "user",      "content": player_input})
    sessions[session_id]["history"].append({"role": "assistant", "content": _strip_choices(reply)})
    sessions[session_id]["pending_narrative"] = narrative
    sessions[session_id]["last_choices"]       = choices

    # Apply metadata
    meta_result = apply_turn_metadata(session_id, metadata, sessions)

    return {
        "session_id":    session_id,
        "narrative":     narrative,
        "choices":       choices,
        "journal_entry": meta_result["journal_entry"],
        "inventory":     meta_result["inventory"],
        "alignment":     meta_result["alignment"],
        "relationships": meta_result["relationships"],
        "ending":        meta_result["ending"],
    }