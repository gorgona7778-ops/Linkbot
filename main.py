"""ДВИЖ link bot. Python 3.12+, Telegram Bot API, one polling replica."""
import json
import logging
import os
from pathlib import Path
import signal
import threading
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parent
STOP = threading.Event()
LOG = logging.getLogger("dvizh")


class APIError(Exception):
    def __init__(self, code, retry_after=5):
        self.code = code
        self.retry_after = retry_after
        super().__init__(f"Telegram API error {code}")


class Bot:
    def __init__(self, token):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        self.photo = (ROOT / "assets/banner.jpg").read_bytes()
        self.photo_id = None

    def api(self, method, data, photo=None):
        if photo is None:
            body = json.dumps(data).encode()
            content_type = "application/json"
        else:
            boundary = uuid.uuid4().hex
            chunks = []
            for key, value in data.items():
                value = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
                chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n').encode())
            chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="banner.jpg"\r\nContent-Type: image/jpeg\r\n\r\n').encode() + photo + b"\r\n")
            chunks.append(f"--{boundary}--\r\n".encode())
            body = b"".join(chunks)
            content_type = f"multipart/form-data; boundary={boundary}"
        request = urllib.request.Request(self.base + method, data=body, headers={"Content-Type": content_type})
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            try:
                result = json.loads(error.read())
            except (ValueError, OSError):
                raise APIError(error.code) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise APIError(0) from None
        if not result.get("ok"):
            raise APIError(result.get("error_code", 0), result.get("parameters", {}).get("retry_after", 5))
        return result["result"]

    def send_menu(self, chat_id):
        data = {
            "chat_id": chat_id,
            "caption": self.config["caption"],
            "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": [
                [{"text": item["text"], "url": item["url"], "style": "danger"}]
                for item in self.config["links"]
            ]},
        }
        if self.photo_id:
            data["photo"] = self.photo_id
        result = self.api("sendPhoto", data, None if self.photo_id else self.photo)
        self.photo_id = result["photo"][-1]["file_id"]

    def handle(self, update):
        message = update.get("message", {})
        chat = message.get("chat", {})
        if chat.get("type") != "private":
            return
        text = message.get("text", "")
        command = text.split(maxsplit=1)[0].split("@")[0].lower() if text else ""
        if command in ("/start", "/links", "/help"):
            self.send_menu(chat["id"])

    def run(self):
        self.api("getMe", {})
        self.api("deleteWebhook", {"drop_pending_updates": False})
        self.api("setMyCommands", {"commands": [
            {"command": "start", "description": "Открыть ДВИЖ"},
            {"command": "links", "description": "Каналы и чаты"},
        ]})
        LOG.info("Bot started. Polling active.")
        offset = 0
        while not STOP.is_set():
            try:
                updates = self.api("getUpdates", {"offset": offset, "timeout": 25, "allowed_updates": ["message"]})
                for update in updates:
                    if STOP.is_set():
                        break
                    try:
                        self.handle(update)
                    except APIError as error:
                        if error.code not in (400, 403):
                            raise
                        LOG.warning("Cannot deliver menu: API %s", error.code)
                    offset = update["update_id"] + 1
            except APIError as error:
                if error.code in (401, 404, 409):
                    LOG.error("API %s: check BOT_TOKEN and run only one replica.", error.code)
                    raise SystemExit(1) from None
                LOG.warning("Temporary API error %s; retrying.", error.code)
                STOP.wait(max(1, error.retry_after))


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Set BOT_TOKEN in Railway Variables.")
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: STOP.set())
    try:
        Bot(token).run()
    except APIError as error:
        raise SystemExit(f"Startup API error {error.code}; check token and connection.") from None


if __name__ == "__main__":
    main()
