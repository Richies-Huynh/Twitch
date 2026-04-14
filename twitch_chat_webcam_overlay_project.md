# Twitch Chat Webcam Overlay

## Overview

This project displays your Twitch chat as an overlay on top of your webcam feed in real time. It uses Python, OpenCV, and Twitch IRC to capture chat messages and render them directly onto video frames.

---

## Features

- Live webcam feed
- Real-time Twitch chat integration
- On-screen chat overlay
- Lightweight and local
- Easily extendable (styling, AI, filters)

---

## Architecture

```
Twitch IRC → Python Socket → Chat Buffer
                               ↓
                        OpenCV Renderer
                               ↓
                         Webcam Display
```

---

## Requirements

- Python 3.8+
- OpenCV
- Internet connection

Install dependencies:

```bash
pip install opencv-python
```

---

## Twitch Setup

1. Get your OAuth token:
   https://twitchapps.com/tmi/

2. Note your:
   - Username
   - Channel name

---

## Implementation

### Full Script

```python
import cv2
import socket
import threading

# ====== TWITCH CONFIG ======
SERVER = 'irc.chat.twitch.tv'
PORT = 6667
NICK = 'your_bot_username'
TOKEN = 'oauth:your_token'
CHANNEL = '#your_channel'

chat_messages = []

# ====== TWITCH CHAT LISTENER ======
def twitch_chat():
    sock = socket.socket()
    sock.connect((SERVER, PORT))
    sock.send(f"PASS {TOKEN}\n".encode('utf-8'))
    sock.send(f"NICK {NICK}\n".encode('utf-8'))
    sock.send(f"JOIN {CHANNEL}\n".encode('utf-8'))

    while True:
        resp = sock.recv(2048).decode('utf-8')

        if resp.startswith('PING'):
            sock.send("PONG\n".encode('utf-8'))
        else:
            try:
                user = resp.split('!')[0][1:]
                message = resp.split('PRIVMSG')[1].split(':', 1)[1]

                chat_messages.append(f"{user}: {message.strip()}")

                if len(chat_messages) > 10:
                    chat_messages.pop(0)
            except:
                pass

# Run chat listener in background
threading.Thread(target=twitch_chat, daemon=True).start()

# ====== WEBCAM ======
cap = cv2.VideoCapture(0)

while True:
    ret, frame = cap.read()
    if not ret:
        break

    # Draw semi-transparent background box
    overlay = frame.copy()
    cv2.rectangle(overlay, (0, 0), (400, 300), (0, 0, 0), -1)
    alpha = 0.4
    frame = cv2.addWeighted(overlay, alpha, frame, 1 - alpha, 0)

    # Draw chat messages
    y0 = 30
    for i, msg in enumerate(chat_messages[-8:]):
        y = y0 + i * 30
        cv2.putText(frame, msg, (10, y),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.6, (0, 255, 0), 2)

    cv2.imshow("Twitch Chat Overlay", frame)

    if cv2.waitKey(1) & 0xFF == 27:
        break

cap.release()
cv2.destroyAllWindows()
```

---

## How to Run

1. Replace credentials in the script:

```
NICK = 'your_bot_username'
TOKEN = 'oauth:your_token'
CHANNEL = '#your_channel'
```

2. Run:

```bash
python your_script.py
```

3. Press `ESC` to exit.

---

## Optional: OBS Integration

To use this in streaming software:

- Add a **Window Capture** source
- Select the OpenCV window

---

## Future Improvements

### UI Enhancements
- Rounded chat box
- Custom fonts
- User color mapping
- Emoji rendering

### Performance
- Switch to GPU rendering
- Limit redraw regions

### Features
- Chat filtering
- Highlight mentions
- Chat summarization (AI)
- Alerts (subs, donations)

---

## Advanced Ideas

- Connect to a React frontend via WebSockets
- Use AI to summarize chat in real time
- Add voice playback of chat messages

---

## Troubleshooting

### No chat messages
- Check OAuth token
- Ensure channel name starts with `#`

### Webcam not working
- Try changing camera index:

```python
cap = cv2.VideoCapture(1)
```

### Lag issues
- Reduce resolution
- Limit chat messages rendered

---

## License

MIT License

---

## Notes

This project is a foundation for building more advanced real-time overlay systems, including AR-style interfaces and intelligent context-aware streaming tools.

