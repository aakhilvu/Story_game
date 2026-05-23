"""
image_manager.py -Unified image generation + character portrait system

Scene Image Pipeline:
  - generate_scene_image()   -text-to-image via Pollinations flux (SCENE priority)

Portrait Pipeline:
  1. build_portrait_prompt()                    -assembles prompt from character fields
  2. generate_portrait_image()                  -Pollinations flux (PORTRAIT priority)
  3. save_portrait()                            -saves PNG to portraits/{session_id}/{name}.png
  4. process_story_portraits_from_characters()  -background task runner

KEY: All Pollinations calls go through pollinations_gate() to ensure only
one request is in-flight at a time (Pollinations allows 1 concurrent req/IP).
Scene images have PRIORITY over portrait generation.
"""

from dotenv import load_dotenv
import builtins
import os
import re
import json
import random
import time
import zlib
import base64
import hashlib
import threading
import requests
from pathlib import Path
from urllib.parse import quote

def _p(*args, **kwargs):
    """Safe print - never raises UnicodeEncodeError on Windows consoles."""
    try:
        builtins.print(*args, **kwargs)
    except UnicodeEncodeError:
        safe = ' '.join(str(a).encode('ascii', 'replace').decode('ascii') for a in args)
        try:
            builtins.print(safe, **{k: v for k, v in kwargs.items() if k != 'file'})
        except Exception:
            pass

# ── Inline Pollinations gate (serialises all requests, 1 at a time) ────────────
SCENE   = 0
PORTRAIT = 1
_poll_sem          = threading.Semaphore(1)
_scene_waiting_evt = threading.Event()
_api_cooldown_until = 0.0


class _YieldToScene(Exception):
    pass


class _PollGate:
    def __init__(self, priority: int):
        self._p = priority

    def __enter__(self):
        global _api_cooldown_until
        # Wait for cooldown BEFORE acquiring the semaphore -never hold the gate
        # while sleeping, otherwise one 429 freezes all other threads.
        while time.time() < _api_cooldown_until:
            time.sleep(0.3)

        if self._p == PORTRAIT:
            for _ in range(40):
                if _scene_waiting_evt.is_set():
                    time.sleep(0.3)
                else:
                    break
        else:
            _scene_waiting_evt.set()

        _poll_sem.acquire()

        if self._p == SCENE:
            _scene_waiting_evt.clear()
        elif self._p == PORTRAIT and _scene_waiting_evt.is_set():
            _poll_sem.release()
            raise _YieldToScene()
        return self

    def __exit__(self, *_):
        # No unconditional sleep -authenticated keys allow ~1 req/s and the
        # HTTP call itself takes well over 1 s on the happy path.
        _poll_sem.release()


def pollinations_gate(priority: int = SCENE) -> _PollGate:
    return _PollGate(priority)

load_dotenv()

# ── Load all Pollinations keys ─────────────────────────────────────────────────
_keys: list[str] = []
for _i in range(1, 11):
    _k = os.getenv(f"POLLINATIONS_KEY_{_i}", "").strip()
    if _k:
        _keys.append(_k)
_legacy = os.getenv("POLLINATIONS_API_KEY", "").strip()
if _legacy and _legacy not in _keys:
    _keys.insert(0, _legacy)

if _keys:
    _p(f"[image_manager] Pollinations -{len(_keys)} key(s) loaded")
else:
    _p("[image_manager] WARNING: No Pollinations keys. Add POLLINATIONS_KEY_1=xxx to .env")
    _p("  Get keys at: https://auth.pollinations.ai")

# ── Key rotation state ─────────────────────────────────────────────────────────
_current_idx    = 0
_exhausted:  dict[int, float] = {}   # idx → time when the key can be retried

# Portrait-specific key state (shares same key pool but separate rotation counter)
_portrait_idx       = 0
_portrait_exhausted: dict[int, float] = {}  # idx → retry-after timestamp

# How long a key is cooled down after a 401/402 before we try it again
_KEY_COOLDOWN_SECS = 90

# ── Dimensions ─────────────────────────────────────────────────────────────────
IMAGE_WIDTH   = 896
IMAGE_HEIGHT  = 512   # 16:9-ish ratio, slightly taller reduces subject-cloning on flux
PORTRAIT_W    = 512
PORTRAIT_H    = 680

# ── Pollinations endpoint config ───────────────────────────────────────────────
# Primary: new canonical host (2025+). Fallback: legacy host.
POLLINATIONS_HOST     = "https://gen.pollinations.ai/image"
POLLINATIONS_FALLBACK = "https://image.pollinations.ai/prompt"
SCENE_MODELS    = ["seedream", "flux", "zimage"]   # seedream = best artistic/illustrative quality
PORTRAIT_MODELS = ["seedream", "flux", "zimage"]
REFERRER        = os.getenv("POLLINATIONS_REFERRER", "story-game-local")
HTTP_TIMEOUT    = 90   # just under the 100 s Cloudflare edge ceiling

# ── Image cache ────────────────────────────────────────────────────────────────
CACHE_DIR    = Path("images/cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)
MAX_CACHE_MB = 500


def _cache_key(prompt: str, model: str, seed: int, w: int, h: int) -> str:
    raw = f"{model}|{seed}|{w}x{h}|{prompt}"
    return hashlib.sha1(raw.encode()).hexdigest()


def _cache_get(key: str) -> bytes | None:
    p = CACHE_DIR / f"{key}.png"
    return p.read_bytes() if p.exists() else None


def _cache_put(key: str, data: bytes):
    (CACHE_DIR / f"{key}.png").write_bytes(data)
    _trim_cache()


def _trim_cache():
    files = sorted(CACHE_DIR.glob("*.png"), key=lambda f: f.stat().st_mtime)
    total = sum(f.stat().st_size for f in files)
    limit = MAX_CACHE_MB * 1024 * 1024
    while total > limit and files:
        f = files.pop(0)
        total -= f.stat().st_size
        try:
            f.unlink()
        except OSError:
            pass


# Trim old cache entries once at import time
_trim_cache()

# ── Portrait storage ───────────────────────────────────────────────────────────
PORTRAITS_DIR = Path("portraits")
PORTRAITS_DIR.mkdir(exist_ok=True)


class ImageError(Exception):
    pass


# ── Key helpers -scene ────────────────────────────────────────────────────────

def _next_available_key() -> tuple[str, int] | tuple[None, None]:
    global _current_idx
    now = time.time()
    for _ in range(len(_keys)):
        idx = _current_idx % len(_keys)
        _current_idx += 1
        retry_after = _exhausted.get(idx)
        if retry_after is None or now >= retry_after:
            if retry_after is not None:
                del _exhausted[idx]  # cooldown expired, restore key
            return _keys[idx], idx
    return None, None


def _mark_exhausted(idx: int):
    global _current_idx
    _exhausted[idx] = time.time() + _KEY_COOLDOWN_SECS
    _current_idx += 1
    available = sum(1 for i in range(len(_keys)) if _exhausted.get(i, 0) <= time.time())
    _p(f"  [Pollinations] Key {idx+1} cooled down for {_KEY_COOLDOWN_SECS}s -{available} key(s) still available")


# ── Key helpers -portrait ─────────────────────────────────────────────────────

def _next_portrait_key() -> tuple[str, int] | tuple[None, None]:
    global _portrait_idx
    now = time.time()
    for _ in range(len(_keys)):
        idx = _portrait_idx % len(_keys)
        _portrait_idx += 1
        retry_after = _portrait_exhausted.get(idx)
        if retry_after is None or now >= retry_after:
            if retry_after is not None:
                del _portrait_exhausted[idx]
            return _keys[idx], idx
    return None, None


def _exhaust_portrait_key(idx: int):
    global _portrait_idx
    _portrait_exhausted[idx] = time.time() + _KEY_COOLDOWN_SECS
    _portrait_idx += 1


# ── URL builders ───────────────────────────────────────────────────────────────

def _build_scene_url(prompt: str, model: str, seed: int, key: str) -> tuple[str, dict]:
    clean   = re.sub(r'[\r\n\t]+', ' ', prompt[:700]).strip()
    encoded = quote(clean, safe='')
    url = (
        f"{POLLINATIONS_HOST}/{encoded}"
        f"?width={IMAGE_WIDTH}&height={IMAGE_HEIGHT}"
        f"&model={model}&seed={seed}&nologo=true&enhance=false&private=true&referrer={REFERRER}"
    )
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return url, headers


def _build_scene_url_fallback(prompt: str, model: str, seed: int, key: str) -> tuple[str, dict]:
    clean   = re.sub(r'[\r\n\t]+', ' ', prompt[:700]).strip()
    encoded = quote(clean, safe='')
    url = (
        f"{POLLINATIONS_FALLBACK}/{encoded}"
        f"?width={IMAGE_WIDTH}&height={IMAGE_HEIGHT}"
        f"&model={model}&seed={seed}&nologo=true&enhance=false&private=true&referrer={REFERRER}"
    )
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return url, headers


def _build_portrait_url(prompt: str, model: str, seed: int, key: str) -> tuple[str, dict]:
    clean   = re.sub(r'[\r\n\t]+', ' ', prompt[:500]).strip()
    encoded = quote(clean, safe='')
    url = (
        f"{POLLINATIONS_HOST}/{encoded}"
        f"?width={PORTRAIT_W}&height={PORTRAIT_H}"
        f"&model={model}&seed={seed}&nologo=true&enhance=false&private=true&referrer={REFERRER}"
    )
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return url, headers


# ── Utility ────────────────────────────────────────────────────────────────────

def panel_bytes_to_b64(img_bytes: bytes) -> str:
    return f"data:image/png;base64,{base64.b64encode(img_bytes).decode()}"


# ══════════════════════════════════════════════════════════════════════════════
# SCENE IMAGE GENERATION
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_one(prompt: str, seed: int, max_retries: int = 5) -> bytes:
    """
    Fetches a scene image, rotating models across attempts (flux → zimage → …).
    Cooldown sleeps happen OUTSIDE the gate so the semaphore is never held during
    waits. Falls back to the legacy Pollinations host on the final attempt.
    """
    global _api_cooldown_until
    if not _keys:
        raise ImageError(
            "No Pollinations API keys configured. "
            "Add POLLINATIONS_KEY_1=xxx to .env -get keys at https://auth.pollinations.ai"
        )

    _start = time.time()

    for attempt in range(1, max_retries + 1):
        model = SCENE_MODELS[(attempt - 1) % len(SCENE_MODELS)]

        # Check cache before hitting the network
        ck = _cache_key(prompt, model, seed, IMAGE_WIDTH, IMAGE_HEIGHT)
        cached = _cache_get(ck)
        if cached:
            _p(f"  [Pollinations] OK Cache hit (model={model}, seed={seed})")
            return cached

        key, idx = _next_available_key()
        if key is None:
            raise ImageError(
                f"All {len(_keys)} Pollinations key(s) are permanently invalid. "
                "Check your keys in .env."
            )

        # Use fallback host on last attempt if previous attempts failed
        if attempt == max_retries:
            url, headers = _build_scene_url_fallback(prompt, model, seed, key)
        else:
            url, headers = _build_scene_url(prompt, model, seed, key)

        attempt_start = time.time()
        _p(f"  [Pollinations] Key {idx+1}/{len(_keys)} model={model} attempt {attempt}/{max_retries}...")

        try:
            with pollinations_gate(SCENE):
                r = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT)
        except requests.exceptions.Timeout:
            elapsed = time.time() - attempt_start
            cooldown = max(4, min(6 * attempt, 14)) + random.uniform(0, 2)
            _p(f"  [Pollinations] WARN TIMEOUT after {elapsed:.1f}s on attempt {attempt} -cooldown {cooldown:.0f}s...")
            _api_cooldown_until = max(_api_cooldown_until, time.time() + cooldown)
            continue
        except (requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError) as e:
            # ConnectionReset / ChunkedEncoding errors are transient — retry with cooldown
            elapsed = time.time() - attempt_start
            cooldown = max(3, min(5 * attempt, 12)) + random.uniform(0, 2)
            _p(f"  [Pollinations] WARN CONNECTION RESET after {elapsed:.1f}s on attempt {attempt} -cooldown {cooldown:.0f}s, retrying...")
            _api_cooldown_until = max(_api_cooldown_until, time.time() + cooldown)
            continue

        ct      = r.headers.get("content-type", "")
        elapsed = time.time() - attempt_start

        if r.status_code == 200 and "image" in ct and len(r.content) > 1000:
            total   = time.time() - _start
            size_kb = len(r.content) // 1024
            if total > 15:
                _p(f"  [Pollinations] WARN SLOW: {total:.1f}s ({size_kb}KB) model={model} Key {idx+1}")
            else:
                _p(f"  [Pollinations] OK OK in {total:.1f}s -{size_kb}KB model={model} Key {idx+1}")
            _cache_put(ck, r.content)
            return r.content

        try:
            body = r.json()
            msg  = body.get("message") or body.get("error", {}).get("message", r.text[:200])
        except Exception:
            msg = r.text[:200]

        _p(f"  [Pollinations] FAIL Key {idx+1} model={model} -HTTP {r.status_code} after {elapsed:.1f}s: {str(msg)[:100]}")

        if r.status_code in (401, 402):
            _p(f"  [Pollinations] FAIL Key {idx+1} permanently invalid -skipping")
            _mark_exhausted(idx)
            continue

        if r.status_code != 429 and "queue full" not in str(msg).lower() and r.status_code < 500:
            raise ImageError(f"Pollinations failed (HTTP {r.status_code}): {str(msg)[:120]}")

        wait = min(5 * attempt, 14) + random.uniform(0, 2)
        _p(f"  [Pollinations] Server/Queue busy -cooldown {wait:.0f}s before retry...")
        _api_cooldown_until = max(_api_cooldown_until, time.time() + wait)

    total = time.time() - _start
    _p(f"  [Pollinations] FAIL FAILED after {max_retries} attempts ({total:.1f}s total) -giving up")
    raise ImageError(
        f"Pollinations failed after {max_retries} attempts ({total:.1f}s). "
        "The service may be overloaded. Try again in a moment."
    )


def _fetch_flux_text(prompt: str, seed: int = 42) -> bytes:
    return _fetch_one(prompt, seed)


def generate_scene_image(
    prompt: str,
    portrait_path: Path | None = None,
    seed: int = 42
) -> str | None:
    """
    Generates one scene image (SCENE priority gate).
    Returns base64 data-URL string, or raises ImageError.
    portrait_path is accepted for API compatibility but unused.
    """
    t0 = time.time()
    _p(f"  [scene] >> Starting image generation (seed={seed})")
    _p(f"  [scene]   Prompt: {prompt[:120].encode('ascii', 'replace').decode('ascii')}...")
    try:
        img_bytes = _fetch_flux_text(prompt, seed)
        elapsed = time.time() - t0
        _p(f"  [scene] OK Image ready in {elapsed:.1f}s")
        return panel_bytes_to_b64(img_bytes)
    except ImageError as e:
        elapsed = time.time() - t0
        _p(f"  [scene] FAIL IMAGE GENERATION FAILED after {elapsed:.1f}s -{e}")
        raise
    except Exception as e:
        elapsed = time.time() - t0
        _p(f"  [scene] FAIL UNEXPECTED ERROR after {elapsed:.1f}s -{e}")
        raise ImageError(f"Unexpected error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# CHARACTER PORTRAIT SYSTEM
# ══════════════════════════════════════════════════════════════════════════════

def build_portrait_prompt(character: dict) -> str:
    """Assembles anime-style portrait prompt from ALL available structured fields."""
    name  = character.get("name", "a person")
    parts = []
    for field in ("appearance", "hair", "face", "body", "clothing"):
        val = character.get(field, "").strip()
        if val and val.lower() not in ("unspecified", "", "none"):
            parts.append(val)

    if not parts:
        parts = [character.get("description", "a person")]

    # Deduplicate while preserving order
    seen, deduped = set(), []
    for p in parts:
        key = p.lower().strip()
        if key not in seen:
            seen.add(key)
            deduped.append(p)

    appearance = ", ".join(deduped)
    return (
        f"detailed anime portrait illustration, painterly cel shading, Studio Ghibli aesthetic, clean line art, vivid colors. "
        f"SOLO single character: {name}. {appearance}. "
        f"Head and shoulders, facing camera, neutral expression, eyes clearly visible. "
        f"plain solid white background, centered composition, "
        f"one person only, single subject, full face visible, "
        f"masterpiece, best quality, extremely detailed, sharp focus. "
        f"avoid: extra limbs, extra hands, extra fingers, fused fingers, multiple heads, "
        f"deformed anatomy, mutated, malformed, disfigured, bad proportions."
    )[:680]


def generate_portrait_image(prompt: str, seed: int = 42) -> bytes | None:
    """
    Generates a portrait image via Pollinations flux (PORTRAIT priority gate).
    Automatically yields to any waiting SCENE request, preventing 429 collisions.
    """
    global _api_cooldown_until
    if not _keys:
        _p("  [Portrait] No Pollinations keys available")
        return None

    clean_prompt = re.sub(r'[\r\n\t]+', ' ', prompt[:400]).strip()
    if not clean_prompt:
        _p("  [Portrait] Empty prompt after sanitization -skipping")
        return None

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        model = PORTRAIT_MODELS[(attempt - 1) % len(PORTRAIT_MODELS)]

        # Check cache before hitting the network
        ck = _cache_key(clean_prompt, model, seed, PORTRAIT_W, PORTRAIT_H)
        cached = _cache_get(ck)
        if cached:
            _p(f"  [Portrait] OK Cache hit (model={model}, seed={seed})")
            return cached

        key, idx = _next_portrait_key()
        if key is None:
            _p("  [Portrait] All Pollinations keys exhausted -giving up")
            return None

        url, headers = _build_portrait_url(clean_prompt, model, seed, key)

        try:
            with pollinations_gate(PORTRAIT):
                _p(f"  [Portrait] Key {idx+1} model={model} attempt {attempt}...")
                try:
                    r = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT)
                except requests.exceptions.Timeout:
                    cooldown = 10 + random.uniform(0, 3)
                    _api_cooldown_until = max(_api_cooldown_until, time.time() + cooldown)
                    _p(f"  [Portrait] Timeout on attempt {attempt} -cooldown {cooldown:.0f}s")
                    continue
                except Exception as e:
                    _p(f"  [Portrait] Fatal request error: {e}")
                    return None

                ct = r.headers.get("content-type", "")
                if r.status_code == 200 and "image" in ct and len(r.content) > 1000:
                    _p(f"  [Portrait] Key {idx+1} model={model} OK -{len(r.content)//1024}KB")
                    _cache_put(ck, r.content)
                    return r.content

                try:
                    msg = r.json().get("error", {}).get("message", "")
                except Exception:
                    msg = r.text[:100]

                is_bad_key = r.status_code in (401, 402) or any(
                    w in str(msg).lower() for w in ("limit", "quota", "exceeded", "balance")
                )
                _p(f"  [Portrait] HTTP {r.status_code} model={model} -{str(msg)[:80]}")

                if is_bad_key:
                    _exhaust_portrait_key(idx)
                elif not (r.status_code == 429 or "queue full" in str(msg).lower() or r.status_code >= 500):
                    _p("  [Portrait] Non-retryable error -skipping")
                    return None

        except _YieldToScene:
            _p(f"  [Portrait] Yielded to scene request -retrying attempt {attempt}")
            time.sleep(1.5)
            continue

        wait = min(5 * attempt, 14) + random.uniform(0, 2)
        _p(f"  [Portrait] Global cooldown {wait:.0f}s before retry...")
        _api_cooldown_until = max(_api_cooldown_until, time.time() + wait)

    _p("  [Portrait] Max retries exhausted -skipping this portrait")
    return None


# ── Portrait save / load helpers ───────────────────────────────────────────────

def _safe_name(char_name: str) -> str:
    return re.sub(r'[^\w\-]', '_', char_name.strip())


def _portrait_path(session_id: str, char_name: str) -> Path:
    d = PORTRAITS_DIR / session_id
    d.mkdir(exist_ok=True)
    return d / f"{_safe_name(char_name)}.png"


def _meta_path(session_id: str) -> Path:
    return PORTRAITS_DIR / session_id / "meta.json"


def save_portrait(session_id: str, char_name: str, img_bytes: bytes, prompt: str):
    path = _portrait_path(session_id, char_name)
    try:
        from rembg import remove
        from PIL import Image
        import io
        img     = Image.open(io.BytesIO(img_bytes))
        out_img = remove(img)
        buf     = io.BytesIO()
        out_img.save(buf, format="PNG")
        img_bytes = buf.getvalue()
    except Exception as e:
        _p(f"  [Portrait] rembg failed: {e}")

    path.write_bytes(img_bytes)
    meta = load_portrait_meta(session_id)
    meta[char_name] = {"file": path.name, "prompt": prompt}
    _meta_path(session_id).write_text(json.dumps(meta, indent=2))
    _p(f"  [Portrait] Saved: {path}")


def load_portrait_meta(session_id: str) -> dict:
    mp = _meta_path(session_id)
    return json.loads(mp.read_text()) if mp.exists() else {}


def portrait_exists(session_id: str, char_name: str) -> bool:
    return _portrait_path(session_id, char_name).exists()


def get_portrait_path(session_id: str, char_name: str) -> Path | None:
    path = _portrait_path(session_id, char_name)
    return path if path.exists() else None


# ── Main portrait pipeline ─────────────────────────────────────────────────────

def process_story_portraits_from_characters(
    session_id: str,
    characters: list,
    sessions: dict = None,
) -> dict:
    """
    Generates portraits for all extracted characters in the background.
    Each portrait uses PORTRAIT-priority gate -pauses automatically when
    a scene image (SCENE priority) needs to go through.
    """
    if not characters:
        _p("  [image_manager] No characters provided -aborting")
        return {}

    _p(f"\n[image_manager] Starting portraits for {len(characters)} characters in session {session_id[:8]}...")

    _scene_event = _scene_waiting_evt

    for char in characters:
        if sessions is not None and session_id not in sessions:
            _p(f"  [image_manager] Session {session_id[:8]} deleted -aborting")
            return load_portrait_meta(session_id)

        char_name = char.get("name", "").strip()
        if not char_name:
            continue

        if portrait_exists(session_id, char_name):
            _p(f"  [Portrait] Already exists: {char_name} -skipping")
            continue

        waited = 0
        while _scene_event.is_set() and waited < 60:
            time.sleep(0.5)
            waited += 0.5
        if waited > 0:
            _p(f"  [image_manager] Waited {waited:.1f}s for scene to clear before {char_name}")
            time.sleep(1.0)

        _p(f"  [Portrait] Generating: {char_name}...")
        prompt    = build_portrait_prompt(char)
        seed      = int(hashlib.sha1(f"{session_id}:{char_name}".encode('utf-8')).hexdigest(), 16) % 999999 + 1
        img_bytes = generate_portrait_image(prompt, seed=seed)

        if img_bytes:
            save_portrait(session_id, char_name, img_bytes, prompt)
        else:
            _p(f"  [Portrait] FAILED: {char_name}")

        # Short pause between characters -authenticated throttle is ~1 req/s
        # and the gate already serialises requests, so minimal sleep is enough.
        time.sleep(0.5)

    result = load_portrait_meta(session_id)
    _p(f"[image_manager] Done -{len(result)} portraits saved for session {session_id[:8]}")
    return result


def delete_session_portraits(session_id: str):
    session_dir = PORTRAITS_DIR / session_id
    if session_dir.exists():
        for f in session_dir.iterdir():
            f.unlink()
        session_dir.rmdir()
