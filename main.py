
"""ДВИЖ navigation bot with opt-in broadcasts and PostgreSQL analytics."""
import datetime as dt
import json
import logging
import os
import signal
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent
STOP = threading.Event()
LOG = logging.getLogger('dvizh')
ADMIN_ID = int(os.getenv('ADMIN_ID', '7951665024'))

class APIError(Exception):
    def __init__(self, code, retry_after=5, description=''):
        self.code, self.retry_after, self.description = code, retry_after, description
        super().__init__(f'Telegram API {code}: {description}')

class Bot:
    def __init__(self, token):
        self.base = f'https://api.telegram.org/bot{token}/'
        self.config = json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))
        self.photo = (ROOT / 'assets/banner.jpg').read_bytes()
        self.photo_id = None
        url = os.getenv('DATABASE_URL')
        if not url:
            raise SystemExit('DATABASE_URL is required. Add Railway PostgreSQL and connect its DATABASE_URL variable.')
        self.db = psycopg.connect(url, autocommit=True)
        self.db.execute('''CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY, username TEXT, first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(), subscribed BOOLEAN NOT NULL DEFAULT FALSE,
            blocked BOOLEAN NOT NULL DEFAULT FALSE)''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS broadcasts (
            id BIGSERIAL PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            content TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            sent INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0)''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS deliveries (
            broadcast_id BIGINT NOT NULL REFERENCES broadcasts(id), user_id BIGINT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', PRIMARY KEY (broadcast_id,user_id))''')
        self.drafts = {}

    def api(self, method, data, photo=None):
        if photo is None:
            body = json.dumps(data).encode()
            content_type = 'application/json'
        else:
            boundary = uuid.uuid4().hex
            chunks = []
            for key, value in data.items():
                value = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
                chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n').encode())
            chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="banner.jpg"\r\nContent-Type: image/jpeg\r\n\r\n').encode() + photo + b'\r\n')
            chunks.append(f'--{boundary}--\r\n'.encode())
            body = b''.join(chunks)
            content_type = f'multipart/form-data; boundary={boundary}'
        request = urllib.request.Request(self.base + method, data=body, headers={'Content-Type': content_type})
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
        if not result.get('ok'):
            raise APIError(result.get('error_code', 0), result.get('parameters', {}).get('retry_after', 5), result.get('description', ''))
        return result['result']

    def send(self, chat_id, text, keyboard=None):
        data = {'chat_id': chat_id, 'text': text}
        if keyboard:
            data['reply_markup'] = {'inline_keyboard': keyboard}
        return self.api('sendMessage', data)

    def menu(self, chat_id):
        data = {'chat_id': chat_id, 'caption': self.config['caption'], 'parse_mode': 'HTML',
                'reply_markup': {'inline_keyboard': [[{'text': item['text'], 'url': item['url']}] for item in self.config['links']]}}
        if self.photo_id:
            data['photo'] = self.photo_id
        result = self.api('sendPhoto', data, None if self.photo_id else self.photo)
        self.photo_id = result['photo'][-1]['file_id']

    def record(self, user):
        self.db.execute('''INSERT INTO users (user_id,username) VALUES (%s,%s)
            ON CONFLICT (user_id) DO UPDATE SET username=EXCLUDED.username,
            last_seen=NOW(), blocked=FALSE''', (user['id'], user.get('username')))

    def stats(self):
        total, today, week, opted, blocked = self.db.execute('''SELECT COUNT(*),
            COUNT(*) FILTER (WHERE first_seen >= NOW()-INTERVAL '1 day'),
            COUNT(*) FILTER (WHERE first_seen >= NOW()-INTERVAL '7 days'),
            COUNT(*) FILTER (WHERE subscribed AND NOT blocked),
            COUNT(*) FILTER (WHERE blocked) FROM users''').fetchone()
        return f'📊 Статистика\nВсего: {total}\nНовых за 24 ч: {today}\nНовых за 7 дней: {week}\nПодписаны на рассылку: {opted}\nЗаблокировали бота: {blocked}\n\nУчёт начинается после подключения базы.'

    def admin(self, chat_id):
        self.send(chat_id, 'ДВИЖ · АДМИН-ПАНЕЛЬ', [
            [{'text': '📊 Статистика', 'callback_data': 'a:stats'}],
            [{'text': '📨 Создать рассылку', 'callback_data': 'a:draft'}],
            [{'text': '📈 Последние рассылки', 'callback_data': 'a:reports'}],
        ])

    def reports(self):
        rows = self.db.execute('SELECT id, status, sent, failed FROM broadcasts ORDER BY id DESC LIMIT 5').fetchall()
        return '📈 Последние рассылки\n' + ('\n'.join(f'#{i}: {status}; успешно {sent}, ошибок {failed}' for i,status,sent,failed in rows) if rows else 'Пока нет.')

    def broadcast(self, broadcast_id):
        row = self.db.execute('SELECT content,status FROM broadcasts WHERE id=%s', (broadcast_id,)).fetchone()
        if not row or row[1] != 'pending':
            return
        self.db.execute("UPDATE broadcasts SET status='running' WHERE id=%s AND status='pending'", (broadcast_id,))
        users = self.db.execute('SELECT user_id FROM users WHERE subscribed AND NOT blocked').fetchall()
        with self.db.transaction():
            for (user_id,) in users:
                self.db.execute('INSERT INTO deliveries (broadcast_id,user_id) VALUES (%s,%s) ON CONFLICT DO NOTHING', (broadcast_id,user_id))
        for (user_id,) in users:
            if STOP.is_set():
                break
            status = self.db.execute('SELECT status FROM deliveries WHERE broadcast_id=%s AND user_id=%s', (broadcast_id,user_id)).fetchone()[0]
            if status != 'pending':
                continue
            for attempt in range(4):
                try:
                    self.send(user_id, row[0], [[{'text': 'Отписаться', 'callback_data': 'u:off'}]])
                    self.db.execute("UPDATE deliveries SET status='sent' WHERE broadcast_id=%s AND user_id=%s", (broadcast_id,user_id))
                    self.db.execute('UPDATE broadcasts SET sent=sent+1 WHERE id=%s', (broadcast_id,))
                    break
                except APIError as exc:
                    if exc.code == 429 and attempt < 3:
                        STOP.wait(min(max(exc.retry_after, 1), 60))
                        continue
                    if exc.code == 403:
                        self.db.execute('UPDATE users SET blocked=TRUE WHERE user_id=%s', (user_id,))
                    self.db.execute("UPDATE deliveries SET status='failed' WHERE broadcast_id=%s AND user_id=%s", (broadcast_id,user_id))
                    self.db.execute('UPDATE broadcasts SET failed=failed+1 WHERE id=%s', (broadcast_id,))
                    LOG.warning('Broadcast delivery error user=%s code=%s', user_id, exc.code)
                    break
            STOP.wait(0.08)
        self.db.execute('UPDATE broadcasts SET status=%s WHERE id=%s', ('paused' if STOP.is_set() else 'done', broadcast_id))
        if not STOP.is_set():
            self.send(ADMIN_ID, 'Рассылка завершена.\n' + self.reports())

    def callback(self, callback):
        sender = callback.get('from', {})
        data = callback.get('data', '')
        user_id = sender.get('id')
        self.api('answerCallbackQuery', {'callback_query_id': callback['id']})
        if data == 'u:on':
            self.db.execute('UPDATE users SET subscribed=TRUE WHERE user_id=%s', (user_id,))
            self.send(user_id, 'Уведомления включены. Для отказа отправь /stop.')
            return
        if data == 'u:off':
            self.db.execute('UPDATE users SET subscribed=FALSE WHERE user_id=%s', (user_id,))
            self.send(user_id, 'Уведомления отключены. Для подписки отправь /subscribe.')
            return
        if user_id != ADMIN_ID or not data.startswith('a:'):
            return
        if data == 'a:stats':
            self.send(user_id, self.stats())
        elif data == 'a:reports':
            self.send(user_id, self.reports())
        elif data == 'a:draft':
            self.drafts[user_id] = {'state': 'text'}
            self.send(user_id, 'Отправь текст рассылки одним сообщением (до 4000 символов). /cancel — отмена.')
        elif data == 'a:cancel':
            self.drafts.pop(user_id, None)
            self.send(user_id, 'Черновик отменён.')
        elif data == 'a:confirm':
            draft = self.drafts.get(user_id)
            if not draft or draft.get('state') != 'confirm':
                self.send(user_id, 'Черновик отсутствует. Открой /admin.')
                return
            self.drafts.pop(user_id, None)
            broadcast_id = self.db.execute('INSERT INTO broadcasts (content) VALUES (%s) RETURNING id', (draft['text'],)).fetchone()[0]
            self.send(user_id, f'Рассылка #{broadcast_id} запущена. Получатели — только подписавшиеся пользователи.')
            self.broadcast(broadcast_id)

    def handle(self, update):
        if 'callback_query' in update:
            self.callback(update['callback_query'])
            return
        message = update.get('message', {})
        chat = message.get('chat', {})
        if chat.get('type') != 'private' or 'from' not in message:
            return
        sender = message['from']
        self.record(sender)
        chat_id = chat['id']
        text = message.get('text', '')
        command = text.split(maxsplit=1)[0].split('@')[0].lower() if text else ''
        if command == '/admin':
            if sender['id'] == ADMIN_ID:
                self.admin(chat_id)
            return
        if command == '/cancel' and sender['id'] == ADMIN_ID:
            self.drafts.pop(sender['id'], None)
            self.send(chat_id, 'Отменено.')
            return
        if command in ('/stop', '/unsubscribe'):
            self.db.execute('UPDATE users SET subscribed=FALSE WHERE user_id=%s', (sender['id'],))
            self.send(chat_id, 'Уведомления отключены. /subscribe — включить.')
            return
        if command == '/subscribe':
            self.db.execute('UPDATE users SET subscribed=TRUE WHERE user_id=%s', (sender['id'],))
            self.send(chat_id, 'Уведомления включены. /stop — отключить.')
            return
        draft = self.drafts.get(sender['id']) if sender['id'] == ADMIN_ID else None
        if draft and draft.get('state') == 'text' and text and not text.startswith('/'):
            if len(text) > 4000:
                self.send(chat_id, 'Слишком длинный текст. Максимум 4000 символов.')
                return
            self.drafts[sender['id']] = {'state': 'confirm', 'text': text}
            self.send(chat_id, 'Предпросмотр рассылки:\n\n' + text,
                      [[{'text': '✅ Подтвердить', 'callback_data': 'a:confirm'}],
                       [{'text': '❌ Отмена', 'callback_data': 'a:cancel'}]])
            return
        if command in ('/start', '/links', '/help'):
            self.menu(chat_id)
            if command == '/start':
                self.send(chat_id, 'Хочешь получать обновления ДВИЖ? Это необязательно.',
                          [[{'text': '🔔 Подписаться', 'callback_data': 'u:on'}]])

    def run(self):
        self.api('getMe', {})
        self.api('deleteWebhook', {'drop_pending_updates': False})
        self.api('setMyCommands', {'commands': [
            {'command': 'start', 'description': 'Открыть ДВИЖ'},
            {'command': 'links', 'description': 'Каналы и чаты'},
            {'command': 'subscribe', 'description': 'Включить уведомления'},
            {'command': 'stop', 'description': 'Отключить уведомления'}]})
        LOG.info('Bot started. Polling active.')
        offset = 0
        while not STOP.is_set():
            try:
                updates = self.api('getUpdates', {'offset': offset, 'timeout': 25,
                                                  'allowed_updates': ['message', 'callback_query']})
                for update in updates:
                    if STOP.is_set():
                        break
                    try:
                        self.handle(update)
                    except APIError as error:
                        LOG.warning('Update API error %s: %s', error.code, error.description)
                    except psycopg.Error:
                        LOG.exception('Database error; update will be retried')
                        raise
                    offset = update['update_id'] + 1
            except APIError as error:
                if error.code in (401, 404, 409):
                    raise SystemExit(f'API {error.code}: check token and ensure one replica.') from None
                LOG.warning('Temporary API error %s', error.code)
                STOP.wait(max(1, error.retry_after))
            except psycopg.Error:
                STOP.wait(5)
                try:
                    self.db.close()
                    self.db = psycopg.connect(os.environ['DATABASE_URL'], autocommit=True)
                except psycopg.Error:
                    LOG.exception('Database reconnect failed')


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    token = os.environ.get('BOT_TOKEN', '').strip()
    if not token:
        raise SystemExit('Set BOT_TOKEN in Railway Variables.')
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: STOP.set())
    Bot(token).run()

if __name__ == '__main__':
    main()
