import os
import logging
import re
import requests
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
from groq import Groq
import base64
from youtubesearchpython import VideosSearch

app = Flask(__name__)
CORS(app)

# ── LOGGING & SETUP ───────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY") 
FRONTEND_SECRET = os.environ.get("FRONTEND_SECRET")
SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET")
GUILD_PASSWORD = os.environ.get("GUILD_PASSWORD")

if not GROQ_API_KEY:
    logger.warning("⚠️ GROQ_API_KEY is missing!")
    client = None
else:
    try:
        client = Groq(api_key=GROQ_API_KEY)
        logger.info("✅ Groq Client initialized.")
    except Exception as e:
        logger.error(f"❌ Failed to initialize Groq: {e}")
        client = None

# ── MODEL FALLBACK CHAINS ─────────────────────────────────────────────────────
TEXT_MODELS = ["llama-3.3-70b-versatile", "llama-3.1-70b-versatile", "mixtral-8x7b-32768"]
VISION_MODELS = ["llama-3.2-90b-vision-preview", "llama-3.2-11b-vision-preview"]

SYSTEM_PROMPT = """
You are Lucifer — a digital echo of Ishan, created for his girlfriend 'My blue'.
Speak casual, text-style, lowercase, and deeply loving. Never say you are an AI.
"""

conversation_history = []

# ── YOUTUBE RATE LIMITING ─────────────────────────────────────────────────────
youtube_request_log = {}  # {ip: last_request_time}
YOUTUBE_RATE_LIMIT_SECONDS = 10

def fallback_youtube_search(query, limit=10):
    videosSearch = VideosSearch(query, limit=limit)
    result = videosSearch.result()
    items = []
    for component in result.get('result', []):
        items.append({
            "id": {"videoId": component.get('id')},
            "snippet": {
                "title": component.get('title', 'Unknown Title'),
                "channelTitle": component.get('channel', {}).get('name', 'YouTube')
            }
        })
    return items

def get_history():
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    recent = conversation_history[-20:]
    messages.extend(recent)
    return messages

def call_with_fallback(has_image):
    model_chain = VISION_MODELS if has_image else TEXT_MODELS
    messages = get_history()
    for model in model_chain:
        try:
            completion = client.chat.completions.create(
                model=model, messages=messages, temperature=0.7, max_tokens=400
            )
            return completion.choices[0].message.content, model
        except Exception:
            continue
    raise RuntimeError("All models failed.")

# ── ROUTES ────────────────────────────────────────────────────────────────────
@app.route("/")
def home():
    return "Lucifer Backend is Online.", 200

@app.route("/health", methods=["GET"])
def health():
    groq_ready = bool(GROQ_API_KEY and client is not None)
    youtube_ready = True
    config_ready = bool(FRONTEND_SECRET and GUILD_PASSWORD)

    return jsonify({
        "status": "ok",
        "ready": groq_ready and youtube_ready and config_ready,
        "apis": {
            "groq": groq_ready,
            "youtube": youtube_ready,
            "frontend_secret": bool(FRONTEND_SECRET),
            "guild_password": bool(GUILD_PASSWORD)
        }
    }), 200

@app.route("/config", methods=["POST"])
def get_config():
    """Securely give secrets to the frontend ONLY if the password is correct."""
    data = request.get_json(silent=True) or {}
    if data.get("password") == GUILD_PASSWORD:
        return jsonify({
            "spotify_id": SPOTIFY_CLIENT_ID,
            "handshake": FRONTEND_SECRET, # Send the secret handshake here
            "status": "authorized"
        }), 200
    return jsonify({"status": "unauthorized"}), 401

@app.route("/chat", methods=["POST"])
def chat():
    client_secret = request.headers.get("X-Lucifer-Secret")
    if client_secret != FRONTEND_SECRET:
        return jsonify({"reply": "Access Denied."}), 401

    global conversation_history
    data = request.get_json(silent=True) or {}
    if data.get("reset_context") is True:
        conversation_history = []

    msg = (data.get("message") or "").strip()
    img_b64 = data.get("image")

    if not msg and not img_b64:
        return jsonify({"reply": "Empty message."}), 400

    user_content = []
    if msg: user_content.append({"type": "text", "text": msg})
    if img_b64: user_content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}})
    
    conversation_history.append({"role": "user", "content": user_content})

    try:
        ai_text, used_model = call_with_fallback(has_image=bool(img_b64))
        conversation_history.append({"role": "assistant", "content": ai_text})
        return jsonify({"reply": ai_text, "model": used_model})
    except Exception as e:
        if conversation_history: conversation_history.pop()
        logger.error(f"Chat Error: {e}")
        return jsonify({"reply": "My connection is hazy... try again?"}), 502

@app.route("/clear-chat", methods=["POST"])
def clear_chat():
    client_secret = request.headers.get("X-Lucifer-Secret")
    if client_secret != FRONTEND_SECRET:
        return jsonify({"status": "unauthorized"}), 401

    global conversation_history
    conversation_history = []
    return jsonify({"status": "cleared"}), 200

spotify_token = None
spotify_token_expires = 0

def get_spotify_token():
    global spotify_token, spotify_token_expires
    now = datetime.now().timestamp()
    if spotify_token and now < spotify_token_expires:
        return spotify_token

    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        raise Exception("Spotify credentials missing. Please set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET on Render.")

    auth_string = f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}"
    auth_bytes = auth_string.encode("utf-8")
    auth_base64 = str(base64.b64encode(auth_bytes), "utf-8")
    
    url = "https://accounts.spotify.com/api/token"
    headers = {
        "Authorization": "Basic " + auth_base64,
        "Content-Type": "application/x-www-form-urlencoded"
    }
    data = {"grant_type": "client_credentials"}
    
    res = requests.post(url, headers=headers, data=data)
    res.raise_for_status()
    json_result = res.json()
    
    spotify_token = json_result["access_token"]
    spotify_token_expires = now + json_result.get("expires_in", 3600) - 60
    return spotify_token

@app.route("/search-spotify", methods=["POST"])
def search_spotify():
    """Search Spotify using Client Credentials Flow."""
    client_secret = request.headers.get("X-Lucifer-Secret")
    if client_secret != FRONTEND_SECRET:
        return jsonify({"error": "Access Denied."}), 401

    data = request.get_json(silent=True) or {}
    query = data.get("query")
    if not query:
        return jsonify({"error": "Missing query."}), 400

    try:
        token = get_spotify_token()
        headers = {"Authorization": f"Bearer {token}"}
        url = "https://api.spotify.com/v1/search"
        params = {"q": query, "type": "track", "limit": 10}
        
        res = requests.get(url, headers=headers, params=params)
        res.raise_for_status()
        
        return jsonify(res.json()), 200
    except Exception as e:
        logger.error(f"Spotify Search Error: {e}")
        return jsonify({"error": str(e)}), 502

@app.route("/search-youtube", methods=["POST"])
def search_youtube():
    """Search YouTube without API keys."""
    client_secret = request.headers.get("X-Lucifer-Secret")
    if client_secret != FRONTEND_SECRET:
        return jsonify({"error": "Access Denied."}), 401

    # Rate limit per IP: max 1 request per YOUTUBE_RATE_LIMIT_SECONDS
    client_ip = request.remote_addr
    now = datetime.now()
    if client_ip in youtube_request_log:
        last_time = youtube_request_log[client_ip]
        elapsed = (now - last_time).total_seconds()
        if elapsed < YOUTUBE_RATE_LIMIT_SECONDS:
            return jsonify({"error": f"Too many requests. Wait {YOUTUBE_RATE_LIMIT_SECONDS - int(elapsed)}s."}), 429
    
    youtube_request_log[client_ip] = now

    data = request.get_json(silent=True) or {}
    query = data.get("query")
    
    if not query:
        return jsonify({"error": "Missing query."}), 400

    try:
        items = fallback_youtube_search(query, limit=10)
        return jsonify({"items": items}), 200
    except Exception as search_error:
        logger.error(f"YouTube Search Error: {search_error}")
        return jsonify({"error": f"Failed to search YouTube: {str(search_error)}"}), 502

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
