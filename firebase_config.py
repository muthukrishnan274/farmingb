"""
Firebase (Firestore) integration for the Agriculture AI Assistant.

This module gives the chatbot durable, per-session conversation history
backed by Cloud Firestore, so a user's chat survives a page refresh or a
new browser tab (as long as they keep the same session id).

Setup
-----
Provide Firebase Admin credentials via ONE of:

  1. FIREBASE_CREDENTIALS_JSON  - the full service-account JSON as a string
     (handy for platforms like Render/Heroku where you paste secrets in).
  2. FIREBASE_CREDENTIALS_PATH  - a filesystem path to a service-account
     JSON key file.
  3. Application Default Credentials (e.g. when running on Cloud Run /
     Cloud Functions / GCE, where no explicit key file is needed).

No credentials configured -> Firestore features are disabled and the app
falls back to client-only (in-browser) history, so local development
without Firebase still works.
"""

import json
import os
import threading

import firebase_admin
from firebase_admin import credentials, firestore

COLLECTION = "chat_sessions"

# Keep a little more than the model-facing history window so trimming
# doesn't fight with MAX_HISTORY_MESSAGES on every turn.
MAX_STORED_MESSAGES = 60

_lock = threading.Lock()
_db = None
_enabled = None  # tri-state: None = not yet checked, True/False after first check


def _load_credentials():
    cred_json = os.environ.get("FIREBASE_CREDENTIALS_JSON")
    if cred_json:
        return credentials.Certificate(json.loads(cred_json))

    cred_path = os.environ.get("FIREBASE_CREDENTIALS_PATH")
    if cred_path:
        return credentials.Certificate(cred_path)

    # Works on GCP infra (Cloud Run/Functions/GCE) with no key file.
    return credentials.ApplicationDefault()


def is_enabled():
    """Whether Firestore persistence is configured and available."""
    global _enabled
    if _enabled is None:
        get_db()
    return bool(_enabled)


def get_db():
    """Lazily initialize Firebase Admin + Firestore. Returns None on failure."""
    global _db, _enabled

    if _db is not None:
        return _db

    with _lock:
        if _db is not None:
            return _db

        try:
            if not firebase_admin._apps:
                firebase_admin.initialize_app(_load_credentials())
            _db = firestore.client()
            _enabled = True
        except Exception:
            # No credentials configured, or Firestore unreachable.
            # Degrade gracefully instead of crashing the whole app.
            _db = None
            _enabled = False

    return _db


def load_history(session_id, limit=None):
    """Return stored [{role, content}, ...] messages for a session, oldest first."""
    if not session_id:
        return []

    db = get_db()
    if db is None:
        return []

    try:
        snapshot = db.collection(COLLECTION).document(session_id).get()
    except Exception:
        return []

    if not snapshot.exists:
        return []

    data = snapshot.to_dict() or {}
    messages = data.get("messages", [])

    if limit:
        messages = messages[-limit:]

    # Only hand back the shape the model layer expects.
    return [
        {"role": item.get("role"), "content": item.get("content")}
        for item in messages
        if item.get("role") in {"user", "assistant"} and item.get("content")
    ]


def append_messages(session_id, new_messages):
    """Append [{role, content}, ...] to a session's stored history and trim."""
    if not session_id or not new_messages:
        return

    db = get_db()
    if db is None:
        return

    doc_ref = db.collection(COLLECTION).document(session_id)

    try:
        doc_ref.set(
            {
                "messages": firestore.ArrayUnion(list(new_messages)),
                "updated_at": firestore.SERVER_TIMESTAMP,
            },
            merge=True,
        )
        _trim_history(doc_ref)
    except Exception:
        # Persistence is a nice-to-have; don't break the chat response over it.
        pass


def _trim_history(doc_ref, max_messages=MAX_STORED_MESSAGES):
    try:
        snapshot = doc_ref.get()
        if not snapshot.exists:
            return

        data = snapshot.to_dict() or {}
        messages = data.get("messages", [])

        if len(messages) > max_messages:
            doc_ref.update({"messages": messages[-max_messages:]})
    except Exception:
        pass


def delete_session(session_id):
    """Clear a session's stored history (used by the 'New chat' action)."""
    if not session_id:
        return

    db = get_db()
    if db is None:
        return

    try:
        db.collection(COLLECTION).document(session_id).delete()
    except Exception:
        pass
