"""Клиент Telegram Bot API: проверка бота и отправка поста в чат.

Никаких сессий/2FA — только токен бота и chat_id. Картинку Telegram скачивает
сам по URL (resize не нужен); если фото не отдаётся — шлём текстом.
"""

import html
import time

import httpx

from app.util import clean_image_url

API = "https://api.telegram.org/bot{token}/{method}"
TG_CAPTION_LIMIT = 1024   # лимит подписи к фото
TG_TEXT_LIMIT = 4096      # лимит текстового сообщения


class TGError(Exception):
    """Ошибка Telegram Bot API."""


class TGClient:
    def __init__(self, account):
        self.token = (account.bot_token or "").strip()
        self.chat_id = (account.chat_id or "").strip()
        if not self.token or not self.chat_id:
            raise TGError("Не заданы токен бота или chat_id")
        self._chat_cache: dict | None = None

    def _call(self, method: str, payload: dict | None = None) -> dict:
        url = API.format(token=self.token, method=method)
        try:
            r = httpx.post(url, json=payload or {}, timeout=30)
            data = r.json()
        except Exception as exc:
            raise TGError(f"Сеть/ответ Telegram: {exc}")
        if not data.get("ok"):
            raise TGError(data.get("description") or "Telegram API error")
        return data.get("result", {})

    def verify(self) -> dict:
        """Проверить токен (getMe) и доступ к чату (getChat)."""
        me = self._call("getMe")
        chat = self._call("getChat", {"chat_id": self.chat_id})
        return {
            "bot": me.get("username", ""),
            "chat": chat.get("title") or chat.get("username") or str(self.chat_id),
        }

    @staticmethod
    def build_comment(template: str, link: str) -> str:
        """Текст первого комментария (parse_mode=HTML): шаблон с кликабельной ссылкой.

        Плейсхолдер {link} в шаблоне заменяется на ссылку (анкор — хост сайта).
        Если плейсхолдера нет — ссылка добавляется в конец. Шаблон по умолчанию:
        «Спасибо проекту {link}».
        """
        link = (link or "").strip()
        tmpl = template or "Спасибо проекту {link}"
        if not link:
            return html.escape(tmpl.replace("{link}", "").strip())
        href = html.escape(link, quote=True)
        label = html.escape(link.split("//", 1)[-1].rstrip("/"))
        anchor = f'<a href="{href}">{label}</a>'
        if "{link}" in tmpl:
            return anchor.join(html.escape(p) for p in tmpl.split("{link}"))
        return html.escape(tmpl).rstrip() + " " + anchor

    def _chat_info(self) -> dict:
        """Тип и связанная группа обсуждений целевого чата (кешируется).

        Возвращает результат getChat: важны type ("channel"/"supergroup"/"group"/
        "private") и linked_chat_id (id группы обсуждений у канала, если задана).
        При ошибке возвращает пустой dict.
        """
        if self._chat_cache is None:
            try:
                self._chat_cache = self._call("getChat", {"chat_id": self.chat_id})
            except TGError:
                self._chat_cache = {}
        return self._chat_cache or {}

    def _find_discussion_message(self, channel_msg_id: int, linked_id=None,
                                 attempts: int = 5, timeout: int = 4):
        """Найти авто-пересланный в группу обсуждений пост канала → (chat_id, message_id).

        Канал и группа обсуждений имеют РАЗНЫЕ message_id; чтобы ответить
        комментарием, нужно найти копию поста в группе (is_automatic_forward) и
        ответить на неё. Получаем её через getUpdates (бот без вебхука). Бот должен
        быть АДМИНОМ группы обсуждений — иначе авто-форвард ему не доставляется
        (privacy mode), и комментарий-в-обсуждении невозможен.
        """
        time.sleep(2)  # дать Telegram авто-переслать пост в группу обсуждений
        offset = None
        for _ in range(attempts):
            params = {"timeout": timeout, "allowed_updates": ["message"]}
            if offset is not None:
                params["offset"] = offset
            updates = self._call("getUpdates", params)
            if not isinstance(updates, list):
                break
            for u in updates:
                offset = u.get("update_id", 0) + 1
                msg = u.get("message") or {}
                fwd_id = msg.get("forward_from_message_id")
                if not fwd_id:
                    fwd_id = (msg.get("forward_origin") or {}).get("message_id")
                if not (msg.get("is_automatic_forward") and fwd_id == channel_msg_id):
                    continue
                grp_chat = msg.get("chat", {}).get("id")
                # если знаем id группы обсуждений — берём только её копию
                if linked_id is not None and grp_chat != linked_id:
                    continue
                return grp_chat, msg.get("message_id")
        return None, None

    def send_post(self, caption: str, image_url: str | None,
                  comment_html: str = "") -> str:
        """Основной пост (текст/фото, без ссылки) + ссылка первым комментарием.

        Комментарий уходит в группу обсуждений ответом на авто-пересланный пост
        (для канала). Запасной вариант — ответ в том же чате (обычная группа).
        Возвращает message_id основного поста. Ошибка комментария не валит пост.
        """
        text = caption or ""
        img = clean_image_url(image_url)
        mid = None
        if img:
            try:
                res = self._call("sendPhoto", {
                    "chat_id": self.chat_id, "photo": img,
                    "caption": text[:TG_CAPTION_LIMIT],
                })
                mid = res.get("message_id")
            except TGError:
                mid = None  # картинка не принялась — отправим текстом ниже
        if mid is None:
            res = self._call("sendMessage", {
                "chat_id": self.chat_id, "text": text[:TG_TEXT_LIMIT],
                "disable_web_page_preview": True,
            })
            mid = res.get("message_id")

        if comment_html.strip() and mid:
            try:
                self._post_comment(int(mid), comment_html)
            except Exception:
                pass  # комментарий best-effort
        return str(mid or "")

    def _post_comment(self, posted_msg_id: int, comment_html: str) -> None:
        """Отправить бэклинк первым комментарием.

        Канал с группой обсуждений: отвечаем на авто-пересланную копию поста в
        ГРУППЕ (→ комментарий в «Прокомментировать»). Если копию не нашли —
        НИЧЕГО не отправляем: ответ в самом канале превратился бы в пост-цитату
        («комментарий как бы в репосте»). Обычная группа (chat_id — сама группа):
        отвечаем на наш же пост в этом чате.
        """
        info = self._chat_info()
        ctype = info.get("type")
        linked = info.get("linked_chat_id")

        # Канал (или чат со связанной группой обсуждений)
        if ctype == "channel" or linked:
            grp_chat, grp_msg = self._find_discussion_message(posted_msg_id, linked)
            if grp_chat and grp_msg:
                self._call("sendMessage", {
                    "chat_id": grp_chat, "text": comment_html[:TG_TEXT_LIMIT],
                    "parse_mode": "HTML", "reply_to_message_id": grp_msg,
                    "disable_web_page_preview": True,
                })
            # не нашли копию в группе — пропускаем (без ответа в канале)
            return

        # Обычная группа/супергруппа: отвечаем на сам пост в этом же чате
        self._call("sendMessage", {
            "chat_id": self.chat_id, "text": comment_html[:TG_TEXT_LIMIT],
            "parse_mode": "HTML", "reply_to_message_id": posted_msg_id,
            "disable_web_page_preview": True,
        })
