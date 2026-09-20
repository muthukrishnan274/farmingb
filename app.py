import os
import re
from flask import Flask, jsonify, render_template, request
from google import genai
from google.genai import types

import firebase_config
from chatbot_config import (
    CHATBOT_TITLE,
    SYSTEM_PROMPT,
    GEMINI_MODEL,
    MAX_HISTORY_MESSAGES,
    MAX_MESSAGE_LENGTH,
)

app = Flask(__name__)

# Session ids are client-generated UUIDs; keep validation strict.
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

client = None


def get_client():
    global client
    if client is None:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not configured.")
        client = genai.Client(api_key=api_key)
    return client


def validate_text(value, field_name, max_length):
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string.")
    value = value.strip()
    if not value:
        raise ValueError(f"{field_name} cannot be empty.")
    if len(value) > max_length:
        raise ValueError(f"{field_name} is too long.")
    return value


def validate_history(history):
    if history is None:
        return []

    if not isinstance(history, list):
        raise ValueError("history must be a list.")

    if len(history) > MAX_HISTORY_MESSAGES:
        raise ValueError("history contains too many messages.")

    validated = []
    for item in history:
        if not isinstance(item, dict):
            raise ValueError("Each history item must be an object.")

        role = item.get("role")
        content = item.get("content")

        if role not in {"user", "assistant"}:
            raise ValueError("Invalid history role.")

        content = validate_text(content, "history content", MAX_MESSAGE_LENGTH)
        validated.append({"role": role, "content": content})

    return validated


def validate_session_id(value):
    if value is None:
        return None
    if not isinstance(value, str) or not SESSION_ID_RE.match(value):
        raise ValueError("Invalid session_id.")
    return value


def build_contents(history, current_message):
    contents = []

    for item in history:
        contents.append(
            types.Content(
                role="user" if item["role"] == "user" else "model",
                parts=[types.Part.from_text(text=item["content"])],
            )
        )

    contents.append(
        types.Content(
            role="user",
            parts=[types.Part.from_text(text=current_message)],
        )
    )
    return contents


@app.get("/")
def index():
    return render_template(
        "index.html",
        chatbot_title=CHATBOT_TITLE,
        firebase_enabled=firebase_config.is_enabled(),
    )


@app.post("/api/chat")
def chat():
    try:
        if not request.is_json:
            return jsonify({"error": "Request body must be JSON."}), 400

        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Invalid JSON request body."}), 400

        try:
            message = validate_text(
                data.get("message"), "message", MAX_MESSAGE_LENGTH
            )
            session_id = validate_session_id(data.get("session_id"))

            # If a session id is present and Firestore has stored history for
            # it, that's the source of truth. Otherwise fall back to whatever
            # history the client sent (e.g. Firestore not configured).
            history = validate_history(data.get("history", []))
            if session_id:
                stored_history = firebase_config.load_history(
                    session_id, limit=MAX_HISTORY_MESSAGES
                )
                if stored_history:
                    history = stored_history
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        response = get_client().models.generate_content(
            model=GEMINI_MODEL,
            contents=build_contents(history, message),
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=0.3,
            ),
        )

        reply = (response.text or "").strip()
        if not reply:
            return jsonify({"error": "The AI did not return a response."}), 502

        if session_id:
            firebase_config.append_messages(
                session_id,
                [
                    {"role": "user", "content": message},
                    {"role": "assistant", "content": reply},
                ],
            )

        return jsonify({"reply": reply}), 200

    except RuntimeError:
        return jsonify({"error": "The chatbot service is not configured correctly."}), 500
    except Exception:
        app.logger.exception("Unexpected chatbot error")
        return jsonify({"error": "Sorry, something went wrong. Please try again."}), 500


@app.get("/api/session/<session_id>/history")
def get_session_history(session_id):
    try:
        session_id = validate_session_id(session_id)
        history = firebase_config.load_history(session_id, limit=MAX_HISTORY_MESSAGES)
        return jsonify({"history": history}), 200
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        app.logger.exception("Unexpected history fetch error")
        return jsonify({"error": "Sorry, something went wrong. Please try again."}), 500


@app.post("/api/session/reset")
def reset_session():
    try:
        data = request.get_json(silent=True) or {}
        session_id = validate_session_id(data.get("session_id"))
        if session_id:
            firebase_config.delete_session(session_id)
        return jsonify({"ok": True}), 200
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        app.logger.exception("Unexpected session reset error")
        return jsonify({"error": "Sorry, something went wrong. Please try again."}), 500


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
