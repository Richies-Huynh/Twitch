"""
Webcam + chat overlay. Run from repo root:

    python -m overlay.main              # MVP: simulated chat (no keys)
    python -m overlay.main --ai         # fake chat lines from OpenAI (needs OPENAI_API_KEY)
    python -m overlay.main --live       # real Twitch IRC (needs TWITCH_* in .env)

Optional: copy .env.example to .env for camera tuning or API keys.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Deque, Optional, Tuple

import cv2

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger(__name__)

SERVER = os.getenv("TWITCH_IRC_HOST", "irc.chat.twitch.tv")
USE_TLS = os.getenv("TWITCH_IRC_USE_TLS", "1").lower() not in ("0", "false", "no")
_default_port = "6697" if USE_TLS else "6667"
PORT = int(os.getenv("TWITCH_IRC_PORT", _default_port))

NICK = os.getenv("TWITCH_NICK", "").strip()
TOKEN = os.getenv("TWITCH_TOKEN", "").strip()
CHANNEL_RAW = os.getenv("TWITCH_CHANNEL", "").strip()

CAMERA_INDEX = int(os.getenv("CAMERA_INDEX", "0"))
CAPTURE_WIDTH = os.getenv("CAPTURE_WIDTH")
CAPTURE_HEIGHT = os.getenv("CAPTURE_HEIGHT")
CAPTURE_FPS = os.getenv("CAPTURE_FPS")

OVERLAY_LINES = int(os.getenv("OVERLAY_LINES", "8"))
CHAT_MAXLEN = int(os.getenv("CHAT_MAXLEN", "200"))
LINE_MAX_CHARS = int(os.getenv("LINE_MAX_CHARS", "52"))

# AI chat (optional)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
AI_POLL_SECONDS = float(os.getenv("AI_CHAT_INTERVAL_SEC", "4"))

shutdown_event = threading.Event()
chat_messages: Deque[str] = deque(maxlen=CHAT_MAXLEN)
chat_lock = threading.Lock()

# Status line for overlay (sim / AI / IRC)
feed_status = {"headline": "", "detail": ""}

# IRC-only extras
irc_state = {
    "connected": False,
    "last_error": "",
    "last_notice": "",
}

SIM_USERS = (
    "lurker_mia",
    "mod_jace",
    "viewer2049",
    "glitch_fox",
    "casual_carter",
    "speedrun_sam",
    "chat_bot_dan",
    "emote_only",
    "night_owl_nia",
    "first_time_finn",
)

SIM_MESSAGES = (
    "PogChamp",
    "first",
    "how long you streaming today?",
    "that was sick",
    "F",
    "clip that",
    "GG",
    "lol",
    "wait what",
    "audio sounds good",
    "can you play that again?",
    "W stream",
    "LUL",
    "7",
    "nice",
    "sub hype",
    "back from dinner",
    "this game is wild",
    "any tips for beginners?",
    "KEKW",
)


def append_chat_line(display: str) -> None:
    with chat_lock:
        chat_messages.append(display)


def sim_chat_loop() -> None:
    feed_status["headline"] = "Chat: simulated (MVP)"
    feed_status["detail"] = ""
    while not shutdown_event.is_set():
        user = random.choice(SIM_USERS)
        msg = random.choice(SIM_MESSAGES)
        sometimes = random.random()
        if sometimes < 0.15:
            msg = f"{msg} {random.choice(SIM_MESSAGES)}"
        append_chat_line(f"{user}: {msg}")
        time.sleep(random.uniform(0.4, 1.6))


def _parse_ai_json_blob(text: str) -> Optional[Tuple[str, str]]:
    text = text.strip()
    try:
        data = json.loads(text)
        u = str(data.get("username", data.get("user", ""))).strip()
        m = str(data.get("message", data.get("text", ""))).strip()
        if u and m:
            return u, m
    except (json.JSONDecodeError, TypeError):
        pass
    m = re.search(
        r'\{\s*"username"\s*:\s*"([^"]+)"\s*,\s*"message"\s*:\s*"((?:[^"\\]|\\.)*)"\s*\}',
        text,
        re.DOTALL,
    )
    if m:
        raw_u, raw_m = m.group(1), m.group(2)
        try:
            return raw_u, json.loads(f'"{raw_m}"')
        except json.JSONDecodeError:
            return raw_u, raw_m.replace("\\n", " ")
    return None


def fetch_ai_chat_line() -> Optional[str]:
    """Returns 'user: message' or None on failure."""
    if not OPENAI_API_KEY:
        return None
    url = f"{OPENAI_BASE_URL}/chat/completions"
    system = (
        "You generate one fictional Twitch live-chat line for a gaming stream UI preview. "
        'Reply with ONLY valid JSON: {"username":"<short lowercase handle>","message":"<one line, max 100 chars>"}. '
        "No markdown, no code fences, no newlines inside strings."
    )
    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": "Generate the next chat line. Vary tone: hype, question, emote-style text, or chill.",
            },
        ],
        "max_tokens": 120,
        "temperature": 1.0,
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    outer = json.loads(raw)
    content = outer["choices"][0]["message"]["content"].strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content).strip()
    parsed = _parse_ai_json_blob(content)
    if parsed:
        u, m = parsed
        return f"{u}: {m.strip()}"
    if ":" in content:
        return content.split("\n", 1)[0].strip()[:200]
    return None


def ai_chat_loop() -> None:
    feed_status["headline"] = "Chat: AI"
    backoff = 2.0
    while not shutdown_event.is_set():
        try:
            line = fetch_ai_chat_line()
            if line:
                append_chat_line(line)
                feed_status["detail"] = ""
                backoff = 2.0
            else:
                feed_status["detail"] = "no API key"
        except urllib.error.HTTPError as exc:
            err = exc.read().decode("utf-8", errors="replace")[:120]
            feed_status["detail"] = f"HTTP {exc.code}: {err}"
            logger.warning("OpenAI HTTP error: %s", exc)
        except Exception as exc:
            feed_status["detail"] = str(exc)[:100]
            logger.warning("OpenAI request failed: %s", exc)
        delay = AI_POLL_SECONDS + random.uniform(0, 1.5)
        for _ in range(int(delay * 10)):
            if shutdown_event.is_set():
                break
            time.sleep(0.1)
        if feed_status["detail"]:
            time.sleep(min(backoff, 30.0))
            backoff = min(backoff * 1.5, 30.0)


def _normalize_channel(name: str) -> str:
    n = name.strip()
    if not n.startswith("#"):
        n = "#" + n
    return n.lower()


def strip_irc_tags(line: str) -> str:
    line = line.strip("\r\n")
    if line.startswith("@"):
        sep = line.find(" :")
        if sep == -1:
            return line
        return line[sep + 2 :]
    return line


def parse_privmsg(line: str) -> Optional[Tuple[str, str]]:
    stripped = strip_irc_tags(line)
    if " PRIVMSG " not in stripped:
        return None
    if not stripped.startswith(":"):
        return None
    try:
        first_space = stripped.index(" ")
        source = stripped[1:first_space]
        nick = source.split("!", 1)[0]
        rest = stripped[first_space + 1 :]
        if not rest.startswith("PRIVMSG "):
            return None
        after_cmd = rest[8:].lstrip()
        space_idx = after_cmd.index(" ")
        trailing = after_cmd[space_idx + 1 :]
        if not trailing.startswith(":"):
            return None
        message = trailing[1:]
        return nick, message
    except (ValueError, IndexError):
        return None


def reply_pong(sock: socket.socket, line: str) -> bool:
    s = line.strip("\r\n")
    if s.startswith("PING"):
        rest = s[4:].strip()
        token = rest if rest else ":tmi.twitch.tv"
        if not token.startswith(":"):
            token = ":" + token.split()[0]
        sock.sendall(f"PONG {token}\r\n".encode("utf-8"))
        return True
    if " PING " in s:
        _, _, tail = s.partition(" PING ")
        tail = tail.strip()
        if not tail:
            tail = ":tmi.twitch.tv"
        elif not tail.startswith(":"):
            tail = ":" + tail.split()[0]
        sock.sendall(f"PONG {tail}\r\n".encode("utf-8"))
        return True
    return False


NOTICE_RE = re.compile(r"NOTICE\s+\*?\s+:(.+)", re.IGNORECASE)


def extract_notice_message(line: str) -> Optional[str]:
    stripped = strip_irc_tags(line)
    m = NOTICE_RE.search(stripped)
    if m:
        return m.group(1).strip()
    return None


def connect_irc() -> socket.socket:
    if USE_TLS:
        ctx = ssl.create_default_context()
        raw = socket.create_connection((SERVER, PORT), timeout=15)
        return ctx.wrap_socket(raw, server_hostname=SERVER)
    return socket.create_connection((SERVER, PORT), timeout=15)


def irc_loop() -> None:
    feed_status["headline"] = "Chat: Twitch IRC"
    backoff = 1.0
    max_backoff = 120.0
    channel = _normalize_channel(CHANNEL_RAW)

    while not shutdown_event.is_set():
        sock: Optional[socket.socket] = None
        try:
            sock = connect_irc()
            sock.settimeout(None)
            sock.sendall(f"PASS {TOKEN}\r\n".encode("utf-8"))
            sock.sendall(f"NICK {NICK.lower()}\r\n".encode("utf-8"))
            sock.sendall(
                b"CAP REQ :twitch.tv/commands twitch.tv/tags twitch.tv/membership\r\n"
            )
            sock.sendall(f"JOIN {channel}\r\n".encode("utf-8"))

            buffer = ""
            irc_state["connected"] = True
            irc_state["last_error"] = ""
            feed_status["detail"] = ""
            backoff = 1.0

            while not shutdown_event.is_set():
                chunk = sock.recv(4096)
                if not chunk:
                    raise ConnectionError("IRC socket closed by remote end")
                buffer += chunk.decode("utf-8", errors="replace")
                while "\r\n" in buffer:
                    line, buffer = buffer.split("\r\n", 1)
                    if not line:
                        continue
                    if reply_pong(sock, line):
                        continue
                    if " JOIN " in line and channel in line.lower():
                        irc_state["last_notice"] = ""
                    notice = extract_notice_message(line)
                    if notice:
                        irc_state["last_notice"] = notice[:200]
                        logger.info("IRC NOTICE: %s", notice)
                    parsed = parse_privmsg(line)
                    if parsed:
                        nick, text = parsed
                        append_chat_line(f"{nick}: {text.strip()}")
                    else:
                        logger.debug("unhandled irc: %s", line[:500])
        except Exception as exc:
            irc_state["connected"] = False
            irc_state["last_error"] = str(exc)[:200]
            feed_status["detail"] = irc_state["last_error"]
            logger.warning("IRC error: %s", exc)
        finally:
            irc_state["connected"] = False
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass

        if shutdown_event.is_set():
            break
        time.sleep(backoff)
        backoff = min(backoff * 2, max_backoff)


def safe_line(text: str, max_chars: int) -> str:
    one_line = text.replace("\r", " ").replace("\n", " ")
    ascii_text = one_line.encode("ascii", "replace").decode("ascii")
    if len(ascii_text) <= max_chars:
        return ascii_text
    return ascii_text[: max_chars - 1] + "…"


def configure_capture(cap: cv2.VideoCapture) -> None:
    if CAPTURE_WIDTH:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(CAPTURE_WIDTH))
    if CAPTURE_HEIGHT:
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(CAPTURE_HEIGHT))
    if CAPTURE_FPS:
        cap.set(cv2.CAP_PROP_FPS, float(CAPTURE_FPS))


def draw_overlay(
    frame,
    lines: list[str],
    status: str,
) -> None:
    h, w = frame.shape[:2]
    panel_w = min(520, max(280, w // 2))
    panel_h = min(40 + OVERLAY_LINES * 28, int(h * 0.45))
    overlay = frame.copy()
    cv2.rectangle(overlay, (0, 0), (panel_w, panel_h), (0, 0, 0), -1)
    alpha = 0.42
    cv2.addWeighted(overlay, alpha, frame, 1 - alpha, 0, dst=frame)

    y0 = 26
    cv2.putText(
        frame,
        safe_line(status, 64),
        (10, y0),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.55,
        (180, 220, 255),
        1,
        cv2.LINE_AA,
    )
    y = y0 + 26
    for msg in lines:
        cv2.putText(
            frame,
            safe_line(msg, LINE_MAX_CHARS),
            (10, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (0, 255, 140),
            1,
            cv2.LINE_AA,
        )
        y += 26


def validate_live_env() -> Optional[str]:
    if not NICK:
        return "TWITCH_NICK is not set."
    if not TOKEN.startswith("oauth:"):
        return "TWITCH_TOKEN must start with oauth:"
    if not CHANNEL_RAW:
        return "TWITCH_CHANNEL is not set (e.g. #yourchannel)."
    return None


def build_status(chat_mode: str) -> str:
    if chat_mode == "live":
        if irc_state["connected"]:
            head = "IRC: connected"
        else:
            head = f"IRC: reconnecting ({irc_state['last_error'][:60]})"
        if irc_state.get("last_notice") and "Login" in irc_state["last_notice"]:
            head = f"IRC: {irc_state['last_notice'][:100]}"
        return head
    detail = feed_status.get("detail") or ""
    head = feed_status.get("headline") or "Chat"
    if detail:
        return f"{head} | {detail[:55]}"
    return head


def main() -> int:
    parser = argparse.ArgumentParser(description="Webcam + chat overlay (sim / AI / Twitch).")
    parser.add_argument(
        "--ai",
        action="store_true",
        help="Use OpenAI for fake chat lines (set OPENAI_API_KEY).",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Use real Twitch IRC (set TWITCH_NICK, TWITCH_TOKEN, TWITCH_CHANNEL).",
    )
    args = parser.parse_args()

    if args.ai and args.live:
        print("Use only one of --ai or --live.", file=sys.stderr)
        return 1

    if args.live:
        chat_mode = "live"
    elif args.ai:
        chat_mode = "ai"
    else:
        chat_mode = "sim"

    if os.getenv("OVERLAY_DEBUG", "").lower() in ("1", "true", "yes"):
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    if chat_mode == "live":
        err = validate_live_env()
        if err:
            print(err, file=sys.stderr)
            print("Use --live with TWITCH_* set in the environment or .env file.", file=sys.stderr)
            return 1
        chat_thread = threading.Thread(target=irc_loop, name="irc", daemon=True)
    elif chat_mode == "ai":
        if not OPENAI_API_KEY:
            print(
                "OPENAI_API_KEY is not set. Export it or add to .env, or run without --ai for simulated chat.",
                file=sys.stderr,
            )
            return 1
        chat_thread = threading.Thread(target=ai_chat_loop, name="ai_chat", daemon=True)
    else:
        chat_thread = threading.Thread(target=sim_chat_loop, name="sim_chat", daemon=True)

    chat_thread.start()

    cap = cv2.VideoCapture(CAMERA_INDEX)
    if not cap.isOpened():
        print(
            f"Could not open camera index {CAMERA_INDEX}. Try CAMERA_INDEX=1 in .env.",
            file=sys.stderr,
        )
        shutdown_event.set()
        chat_thread.join(timeout=3.0)
        return 2

    configure_capture(cap)

    fail_streak = 0
    window = "Twitch Chat Overlay"

    try:
        while not shutdown_event.is_set():
            ret, frame = cap.read()
            if not ret or frame is None:
                fail_streak += 1
                if fail_streak > 45:
                    print("Camera read failed repeatedly; exiting.", file=sys.stderr)
                    return 3
                time.sleep(0.05)
                continue
            fail_streak = 0

            with chat_lock:
                recent = list(chat_messages)[-OVERLAY_LINES:]

            status = build_status(chat_mode)
            draw_overlay(frame, recent, status)
            cv2.imshow(window, frame)
            key = cv2.waitKey(1) & 0xFF
            if key == 27:
                break
    finally:
        shutdown_event.set()
        cap.release()
        cv2.destroyAllWindows()
        chat_thread.join(timeout=5.0)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
