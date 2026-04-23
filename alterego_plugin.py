# region Metadata
__name__ = "AlterEgo"
__description__ = "Шифрование сообщений с использование семантической трансформации"
__version__ = "0.0.1"
__id__ = "alter_ego"
__author__ = "@renamq"
__icon__ = "exteraPlugins/1"
# endregion

import random
import threading
import time
import traceback
from java import jclass
from datetime import datetime, timezone
from typing import Any, List
from urllib.parse import urlparse

import requests
from android_utils import run_on_ui_thread, OnClickListener
from base_plugin import BasePlugin, MethodHook, HookResult, HookStrategy
from client_utils import run_on_queue, get_last_fragment
from hook_utils import find_class, get_private_field, set_private_field
from ui.bulletin import BulletinHelper
from ui.settings import Header, Text, Divider, Input

ACTION_DOWN = 0
ACTION_UP = 1
ACTION_CANCEL = 3
LONG_PRESS_DELAY_MS = 430
STATUS_RETURN_DELAY_MS = 420
STATUS_REFRESH_INTERVAL_MS = 5000
MASK_TIMEOUT_SEC = 15


class StatusTouchHelper:
    # Handles tap and long-press for floating status view.
    def __init__(self, plugin):
        self.plugin = plugin
        self._cell_to_chat = {}
        self._touch_state = {}

    @staticmethod
    def _view_key(view):
        try:
            return int(view.hashCode())
        except Exception:
            return str(view)

    @staticmethod
    def _now_ms():
        return int(time.time() * 1000)

    def register_cell(self, cell, chat_activity):
        self._cell_to_chat[self._view_key(cell)] = chat_activity

    def unregister_cell(self, cell):
        key = self._view_key(cell)
        if key in self._cell_to_chat:
            del self._cell_to_chat[key]
        if key in self._touch_state:
            del self._touch_state[key]

    def is_status_cell(self, cell):
        cell_key = self._view_key(cell)
        chat_activity = self._cell_to_chat.get(cell_key)
        return chat_activity is not None and not self.plugin._is_native_date_mode(
            chat_activity
        )

    def _touch_down(self, cell_key):
        self.plugin.log(f"[AlterEgo][diag] touch down: {cell_key}")
        token = self._now_ms()
        self._touch_state[cell_key] = {"pressed": True, "fired": False, "token": token}

        def _fire_long_press():
            state = self._touch_state.get(cell_key)
            if not state or not state.get("pressed") or state.get("fired"):
                return
            if state.get("token") != token:
                return

            chat_activity = self._cell_to_chat.get(cell_key)
            if chat_activity is None or self.plugin._is_native_date_mode(chat_activity):
                self.plugin.log(
                    "[AlterEgo][diag] long press skipped: no chat or native mode"
                )
                return

            state["fired"] = True
            self.plugin.log("[AlterEgo][diag] long press fired")
            self.plugin._on_status_long_press(chat_activity)

        run_on_ui_thread(_fire_long_press, LONG_PRESS_DELAY_MS)

    def _touch_up(self, cell_key):
        self.plugin.log(f"[AlterEgo][diag] touch up: {cell_key}")
        state = self._touch_state.get(cell_key)
        if state is not None:
            state["pressed"] = False

    def _is_long_press_fired(self, cell_key):
        state = self._touch_state.get(cell_key)
        return bool(state and state.get("fired"))

    def handle_touch(self, cell, event):
        if event is None:
            return False

        action = event.getAction()
        cell_key = self._view_key(cell)

        if action == ACTION_DOWN:
            self._touch_down(cell_key)
        elif action == ACTION_UP:
            if not self._is_long_press_fired(cell_key):
                chat_activity = self._cell_to_chat.get(cell_key)
                if chat_activity is not None and not self.plugin._is_native_date_mode(
                    chat_activity
                ):
                    self.plugin._on_status_tap(chat_activity)
            self._touch_up(cell_key)
        elif action == ACTION_CANCEL:
            self._touch_up(cell_key)

        return True


class FloatingDateViewHelper:
    # Encapsulates floating date view mutations and native listener restore.
    def __init__(self, plugin):
        self.plugin = plugin
        self._native_action_click = {}

    def _chat_key(self, chat_activity):
        return self.plugin._chat_key(chat_activity)

    @staticmethod
    def _keep_visible(view):
        if hasattr(view, "setAlpha"):
            view.setAlpha(1.0)
        if hasattr(view, "setTag"):
            view.setTag(1)
        if hasattr(view, "setVisibility"):
            view.setVisibility(0)

    def _get_floating_view(self, chat_activity):
        return get_private_field(chat_activity, "floatingDateView")

    def apply_status_text(self, chat_activity, text):
        if chat_activity is None:
            return None

        floating_date_view = self._get_floating_view(chat_activity)
        if floating_date_view is None:
            return None

        chat_key = self._chat_key(chat_activity)
        if chat_key not in self._native_action_click:
            self._native_action_click[chat_key] = get_private_field(
                floating_date_view, "onActionClick"
            )

        if hasattr(floating_date_view, "setCustomText"):
            floating_date_view.setCustomText(text)

        if hasattr(floating_date_view, "setOnActionClickListener"):
            floating_date_view.setOnActionClickListener(
                OnClickListener(lambda view: None)
            )

        self._keep_visible(floating_date_view)
        return floating_date_view

    def restore_native_date(self, chat_activity):
        if chat_activity is None:
            return None

        floating_date_view = self._get_floating_view(chat_activity)
        if floating_date_view is None:
            return None

        if hasattr(floating_date_view, "getCustomDate") and hasattr(
            floating_date_view, "setCustomDate"
        ):
            current_date = floating_date_view.getCustomDate()
            if current_date:
                floating_date_view.setCustomDate(0, False, False)
                floating_date_view.setCustomDate(current_date, False, False)

        chat_key = self._chat_key(chat_activity)
        original_listener = self._native_action_click.get(chat_key)
        if original_listener is not None and hasattr(
            floating_date_view, "setOnActionClickListener"
        ):
            floating_date_view.setOnActionClickListener(original_listener)

        self._keep_visible(floating_date_view)
        return floating_date_view


class ChatCreateViewHook(MethodHook):
    def __init__(self, plugin):
        self.plugin = plugin

    def after_hooked_method(self, param):
        try:
            self.plugin._apply_floating_date_status(param.thisObject)
        except Exception as error:
            self.plugin.log(
                f"[AlterEgo] Failed to apply floating date status on createView: {error}"
            )


class ChatShowFloatingDateHook(MethodHook):
    def __init__(self, plugin):
        self.plugin = plugin

    def after_hooked_method(self, param):
        try:
            is_scroll = (
                bool(param.args[0]) if param.args and len(param.args) > 0 else False
            )
            if is_scroll:
                self.plugin._set_native_date_mode(param.thisObject, True)
                self.plugin._mark_scroll_activity(param.thisObject)
                self.plugin._restore_native_floating_date(param.thisObject)
                self.plugin._schedule_auto_status_return(param.thisObject)
            else:
                self.plugin._set_native_date_mode(param.thisObject, False)
                self.plugin._apply_floating_date_status(param.thisObject)
        except Exception as error:
            self.plugin.log(
                f"[AlterEgo] Failed to apply floating date status on show: {error}"
            )


class ChatHideFloatingDateHook(MethodHook):
    def __init__(self, plugin):
        self.plugin = plugin

    def before_hooked_method(self, param):
        try:
            # Keep floating view always visible. In native mode we keep native date text,
            # in status mode we keep custom plugin text.
            if self.plugin._is_native_date_mode(param.thisObject):
                self.plugin._restore_native_floating_date(param.thisObject)
                param.setResult(None)
                return
            else:
                self.plugin._apply_floating_date_status(param.thisObject)
                param.setResult(None)
        except Exception as error:
            self.plugin.log(f"[AlterEgo] Failed to keep floating date visible: {error}")


class FloatingDateTouchHook(MethodHook):
    def __init__(self, plugin):
        self.plugin = plugin

    def before_hooked_method(self, param):
        try:
            cell = param.thisObject
            if not self.plugin._status_touch.is_status_cell(cell):
                return

            event = param.args[0] if param.args and len(param.args) > 0 else None
            if self.plugin._status_touch.handle_touch(cell, event):
                param.setResult(True)
        except Exception as error:
            self.plugin.log(f"[AlterEgo] FloatingDate touch hook failed: {error}")


# endregion


class AlterEgo(BasePlugin):
    # region Lifecycle
    def __init__(self):
        super().__init__()
        self._chat_create_view_unhooks = []
        self._chat_show_floating_date_unhooks = []
        self._chat_hide_floating_date_unhooks = []
        self._chat_action_touch_unhooks = []
        self._native_date_mode = {}
        self._last_scroll_activity_ms = {}
        self._status_cache = "API Unavailable"
        self._status_cache_at_ms = 0
        self._status_refresh_in_flight = False
        self._auth_access_token = ""
        self._auth_refresh_token = ""
        self._auth_expires_at_ms = 0
        self._auth_feedback = ""
        self._send_hook_registered = False
        self._update_hook_ids = []
        self._request_hook_ids = []
        self._mask_cache = {}
        self._bypass_text_counts = {}
        self._pending_stored_messages = []
        self._status_touch = StatusTouchHelper(self)
        self._floating_view = FloatingDateViewHelper(self)

    def on_plugin_load(self):
        try:
            chat_activity_class = find_class("org.telegram.ui.ChatActivity")
            if not chat_activity_class:
                self.log("[AlterEgo] ChatActivity class not found")
                return

            self._chat_create_view_unhooks = (
                self.hook_all_methods(
                    chat_activity_class,
                    "createView",
                    ChatCreateViewHook(self),
                    priority=80,
                )
                or []
            )

            self._chat_show_floating_date_unhooks = (
                self.hook_all_methods(
                    chat_activity_class,
                    "showFloatingDateView",
                    ChatShowFloatingDateHook(self),
                    priority=90,
                )
                or []
            )

            self._chat_hide_floating_date_unhooks = (
                self.hook_all_methods(
                    chat_activity_class,
                    "hideFloatingDateView",
                    ChatHideFloatingDateHook(self),
                    priority=80,
                )
                or []
            )

            chat_action_cell_class = find_class("org.telegram.ui.Cells.ChatActionCell")
            if chat_action_cell_class:
                self._chat_action_touch_unhooks = (
                    self.hook_all_methods(
                        chat_action_cell_class,
                        "onTouchEvent",
                        FloatingDateTouchHook(self),
                        priority=100,
                    )
                    or []
                )

            self.log(
                f"[AlterEgo] Hooks active: createView={len(self._chat_create_view_unhooks)}, "
                f"showFloatingDateView={len(self._chat_show_floating_date_unhooks)}, "
                f"hideFloatingDateView={len(self._chat_hide_floating_date_unhooks)}, "
                f"chatActionTouch={len(self._chat_action_touch_unhooks)}"
            )

            try:
                self.add_on_send_message_hook(priority=90)
                self._send_hook_registered = True
                self.log("[AlterEgo] Send pipeline hook registered")
            except Exception as error:
                self.log(f"[AlterEgo] Send pipeline hook failed: {error}")

            try:
                request_hook_names = [
                    "TL_messages_sendMessage",
                    "TL_messages_sendMedia",
                    "TL_messages_sendMultiMedia",
                ]
                for hook_name in request_hook_names:
                    hook_id = self.add_hook(
                        hook_name, match_substring=True, priority=80
                    )
                    if hook_id:
                        self._request_hook_ids.append(hook_id)
                self.log(
                    f"[AlterEgo] Request hooks registered: {len(self._request_hook_ids)}"
                )
            except Exception as error:
                self.log(f"[AlterEgo] Request hook registration failed: {error}")

            try:
                update_hook_names = [
                    "TL_updateNewMessage",
                    "TL_updateNewChannelMessage",
                    "updates",
                    "updatesCombined",
                ]
                for hook_name in update_hook_names:
                    hook_id = self.add_hook(
                        hook_name, match_substring=True, priority=80
                    )
                    if hook_id:
                        self._update_hook_ids.append(hook_id)
                self.log(
                    f"[AlterEgo] Update hooks registered: {len(self._update_hook_ids)}"
                )
            except Exception as error:
                self.log(f"[AlterEgo] Update hook registration failed: {error}")

        except Exception as error:
            self.log(f"[AlterEgo] Hook setup failed: {error}")
            self.log(f"[AlterEgo] Hook setup traceback: {traceback.format_exc()}")

    # endregion

    # region Status text
    def _status(self, chat_activity=None):
        now = self._now_ms()
        if now - self._status_cache_at_ms >= STATUS_REFRESH_INTERVAL_MS:
            self._refresh_status_async(chat_activity)
        return self._status_cache

    def _compute_status_sync(self):
        api_url = self.get_setting("api_url", "").strip()
        if not isinstance(api_url, str) or not api_url:
            return "API Unavailable"

        if not self._is_url(api_url):
            return "Invalid URL"

        try:
            normalized = api_url.rstrip("/")
            headers = self._auth_headers(normalized)
            response = requests.get(
                f"{normalized}/api/Status", timeout=2, headers=headers
            )
            if response.status_code == 200:
                return "Connected"
            if response.status_code == 401:
                if self._login_sync(normalized):
                    retry_headers = self._auth_headers(normalized)
                    retry_response = requests.get(
                        f"{normalized}/api/Status", timeout=2, headers=retry_headers
                    )
                    if retry_response.status_code == 200:
                        return "Connected"
                if self._has_auth_credentials():
                    return "Unauthorized"
                return "Auth Required"
        except Exception:
            return "API Unavailable"

        return "API Unavailable"

    def _refresh_status_async(self, chat_activity=None):
        if self._status_refresh_in_flight:
            return

        self._status_refresh_in_flight = True

        def _worker():
            new_status = self._compute_status_sync()

            def _apply():
                self._status_cache = new_status
                self._status_cache_at_ms = self._now_ms()
                self._status_refresh_in_flight = False
                if chat_activity is not None and not self._is_native_date_mode(
                    chat_activity
                ):
                    self._apply_floating_date_status(chat_activity)

            run_on_ui_thread(_apply)

        def _worker_safe():
            try:
                _worker()
            except Exception:

                def _reset():
                    self._status_refresh_in_flight = False
                    self._status_cache_at_ms = self._now_ms()

                run_on_ui_thread(_reset)

        run_on_queue(_worker_safe)

    @staticmethod
    def _is_url(url):
        try:
            result = urlparse(url)
            return all([result.scheme, result.netloc])
        except ValueError:
            return False

    @staticmethod
    def _parse_datetime_to_ms(value):
        if not isinstance(value, str) or not value.strip():
            return 0

        try:
            normalized = value.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return int(parsed.timestamp() * 1000)
        except Exception:
            return 0

    def _has_auth_credentials(self):
        username = self.get_setting("auth_username", "")
        password = self.get_setting("auth_password", "")
        return (
            isinstance(username, str)
            and bool(username.strip())
            and isinstance(password, str)
            and bool(password.strip())
        )

    def _login_sync(self, api_base_url):
        username = self.get_setting("auth_username", "")
        password = self.get_setting("auth_password", "")
        if not isinstance(username, str) or not isinstance(password, str):
            return False

        username = username.strip()
        password = password.strip()
        if not username or not password:
            return False

        success, _message = self._login_with_credentials_sync(
            api_base_url, username, password
        )
        return success

    @staticmethod
    def _extract_server_message(response, fallback):
        if response is None:
            return fallback

        try:
            payload = response.json() if response.content else {}
            if isinstance(payload, dict):
                errors = payload.get("errors")
                if isinstance(errors, dict) and errors:
                    parts = []
                    for field, msgs in errors.items():
                        if isinstance(msgs, list) and msgs:
                            parts.append(f"{field}: {msgs[0]}")
                        elif isinstance(msgs, str) and msgs.strip():
                            parts.append(f"{field}: {msgs.strip()}")
                    if parts:
                        return "; ".join(parts[:4])
                for key in ("message", "detail", "title"):
                    value = payload.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
        except Exception:
            pass

        try:
            text = response.text.strip() if isinstance(response.text, str) else ""
            if text:
                return text[:200]
        except Exception:
            pass

        return fallback

    def _login_with_credentials_sync(self, api_base_url, username, password):
        try:
            response = requests.post(
                f"{api_base_url}/api/Auth/login",
                json={"username": username, "password": password},
                timeout=3,
            )
        except Exception:
            return False, "Сервер недоступен"

        if response.status_code != 200:
            message = self._extract_server_message(response, "Ошибка авторизации")
            return False, message

        try:
            payload = response.json() if response.content else {}
        except Exception:
            return False, "Некорректный ответ сервера"

        access_token = payload.get("accessToken") if isinstance(payload, dict) else None
        refresh_token = (
            payload.get("refreshToken") if isinstance(payload, dict) else None
        )
        expires_at = payload.get("expiresAt") if isinstance(payload, dict) else None
        if not isinstance(access_token, str) or not access_token.strip():
            return False, "Токен не получен"

        self._auth_access_token = access_token.strip()
        self._auth_refresh_token = (
            refresh_token.strip() if isinstance(refresh_token, str) else ""
        )
        self._auth_expires_at_ms = self._parse_datetime_to_ms(expires_at)
        self.set_setting("auth_session_user", username, reload_settings=True)
        return True, "Вход выполнен"

    def _refresh_token_sync(self, api_base_url):
        if not self._auth_refresh_token:
            return False

        try:
            response = requests.post(
                f"{api_base_url}/api/Auth/refresh",
                json={"refreshToken": self._auth_refresh_token},
                timeout=3,
            )
            if response.status_code != 200:
                return False

            payload = response.json() if response.content else {}
            access_token = (
                payload.get("accessToken") if isinstance(payload, dict) else None
            )
            refresh_token = (
                payload.get("refreshToken") if isinstance(payload, dict) else None
            )
            expires_at = payload.get("expiresAt") if isinstance(payload, dict) else None
            if not isinstance(access_token, str) or not access_token.strip():
                return False

            self._auth_access_token = access_token.strip()
            if isinstance(refresh_token, str) and refresh_token.strip():
                self._auth_refresh_token = refresh_token.strip()
            self._auth_expires_at_ms = self._parse_datetime_to_ms(expires_at)
            return True
        except Exception:
            return False

    def _auth_headers(self, api_base_url):
        now = self._now_ms()
        expires_soon = (
            self._auth_expires_at_ms > 0 and self._auth_expires_at_ms - now <= 15000
        )

        if not self._auth_access_token or expires_soon:
            if not self._refresh_token_sync(api_base_url):
                self._login_sync(api_base_url)

        if not self._auth_access_token:
            return {}

        return {"Authorization": f"Bearer {self._auth_access_token}"}

    def _call_mask_sync(self, api_base_url, dialog_id, original_text):
        headers = self._auth_headers(api_base_url)
        payload = {
            "dialogId": int(dialog_id) if isinstance(dialog_id, int) else 0,
            "originalText": original_text,
        }
        self.log(
            f"[AlterEgo] mask request: dialog={payload['dialogId']}, text_len={len(original_text)}"
        )

        last_error = ""
        for attempt in range(3):
            try:
                response = requests.post(
                    f"{api_base_url}/api/Messages/mask",
                    json=payload,
                    headers=headers,
                    timeout=MASK_TIMEOUT_SEC,
                )
            except Exception as error:
                last_error = "Сервер недоступен"
                self.log(f"[AlterEgo] mask request failed: {error}")
                if attempt < 2:
                    time.sleep(0.35)
                    continue
                return False, "", last_error

            self.log(f"[AlterEgo] mask response status: {response.status_code}")
            if response.status_code >= 500:
                last_error = self._extract_server_message(
                    response, "Ошибка генерации маски"
                )
                if attempt < 2:
                    time.sleep(0.35)
                    continue
                return False, "", last_error
            if response.status_code >= 400:
                message = self._extract_server_message(
                    response, "Ошибка генерации маски"
                )
                return False, "", message

            try:
                data = response.json() if response.content else {}
            except Exception as error:
                self.log(f"[AlterEgo] mask parse failed: {error}")
                return False, "", "Некорректный ответ сервера"

            if isinstance(data, dict):
                cover_text = data.get("coverText")
                if isinstance(cover_text, str) and cover_text.strip():
                    return True, cover_text.strip(), ""

            return False, "", "Сервер не вернул coverText"

        return False, "", last_error or "Ошибка генерации маски"

    @staticmethod
    def _extract_text_from_send_params(params):
        if params is None:
            return ""

        if isinstance(params, dict):
            for key in (
                "text",
                "message",
                "messageText",
                "caption",
                "content",
            ):
                value = params.get(key)
                if isinstance(value, str) and value.strip():
                    return value

        for attr in (
            "text",
            "message",
            "messageText",
            "caption",
            "content",
        ):
            try:
                value = getattr(params, attr)
            except Exception:
                value = get_private_field(params, attr)
            if isinstance(value, str) and value.strip():
                return value

        return ""

    @staticmethod
    def _extract_send_params_meta(params):
        data = {
            "peer": None,
            "scheduleDate": None,
            "scheduleRepeatPeriod": None,
            "pendingRequestId": 0,
            "clientRandomId": 0,
        }

        for key in data.keys():
            value = None
            try:
                value = getattr(params, key)
            except Exception:
                value = get_private_field(params, key)
            data[key] = value

        try:
            pending_id = int(data.get("pendingRequestId") or 0)
        except Exception:
            pending_id = 0
        try:
            random_id = int(data.get("clientRandomId") or 0)
        except Exception:
            random_id = 0
        try:
            maybe_params = get_private_field(params, "params")
            if isinstance(maybe_params, dict):
                for key in ("pending_id", "pendingId", "req_id", "request_id"):
                    value = maybe_params.get(key)
                    try:
                        pending_id = int(value)
                        if pending_id:
                            break
                    except Exception:
                        continue
                for key in ("random_id", "randomId", "client_random_id"):
                    value = maybe_params.get(key)
                    try:
                        random_id = int(value)
                        if random_id:
                            break
                    except Exception:
                        continue
        except Exception:
            pass

        if pending_id:
            data["pendingRequestId"] = pending_id
        for key in ("random_id", "randomId", "clientRandomId"):
            if random_id:
                break
            try:
                value = getattr(params, key)
            except Exception:
                value = get_private_field(params, key)
            try:
                random_id = int(value)
            except Exception:
                continue

        if random_id:
            data["clientRandomId"] = random_id

        return data

    @staticmethod
    def _extract_dialog_id_from_peer(peer):
        if peer is None:
            return 0

        for peer_attr in ("user_id", "userId"):
            try:
                v = getattr(peer, peer_attr)
            except Exception:
                v = get_private_field(peer, peer_attr)
            try:
                n = int(v)
                if n:
                    return n
            except Exception:
                pass

        for peer_attr in ("chat_id", "chatId", "channel_id", "channelId"):
            try:
                v = getattr(peer, peer_attr)
            except Exception:
                v = get_private_field(peer, peer_attr)
            try:
                n = int(v)
                if n:
                    return -n
            except Exception:
                pass

        return 0

    @staticmethod
    def _extract_message_from_update(update):
        if update is None:
            return None
        try:
            class_name = str(update.getClass().getName())
            if class_name.endswith("TLRPC$Message"):
                return update
        except Exception:
            pass
        try:
            message = getattr(update, "message")
            if message is not None:
                return message
        except Exception:
            pass
        try:
            message = get_private_field(update, "message")
            if message is not None:
                return message
        except Exception:
            pass
        return None

    @staticmethod
    def _extract_update_message_fields(update):
        data = {
            "pendingRequestId": 0,
            "clientRandomId": 0,
            "messageId": 0,
            "dialogId": 0,
            "text": "",
            "out": False,
        }

        if update is None:
            return data

        message = AlterEgo._extract_message_from_update(update)
        data["text"] = AlterEgo._extract_message_text(message)
        data["messageId"] = AlterEgo._extract_message_id(message)
        data["dialogId"] = AlterEgo._extract_dialog_id_from_message(message)
        data["out"] = False

        if message is not None:
            for attr in ("out", "isOut"):
                try:
                    v = getattr(message, attr)
                except Exception:
                    v = get_private_field(message, attr)
                if isinstance(v, bool):
                    data["out"] = v
                    break

            try:
                random_id = getattr(message, "random_id")
            except Exception:
                random_id = get_private_field(message, "random_id")
            try:
                data["clientRandomId"] = int(random_id)
            except Exception:
                pass

        # Fallback for update types without nested Message object
        if data["messageId"] <= 0:
            try:
                data["messageId"] = int(getattr(update, "id"))
            except Exception:
                try:
                    data["messageId"] = int(get_private_field(update, "id"))
                except Exception:
                    pass

        if not data["text"]:
            try:
                maybe_text = getattr(update, "message")
            except Exception:
                maybe_text = get_private_field(update, "message")
            if isinstance(maybe_text, str):
                data["text"] = maybe_text

        if data["clientRandomId"] == 0:
            try:
                data["clientRandomId"] = int(getattr(update, "random_id"))
            except Exception:
                try:
                    data["clientRandomId"] = int(get_private_field(update, "random_id"))
                except Exception:
                    pass

        if data["dialogId"] == 0:
            # Common shape for TL_updateShortSentMessage
            try:
                user_id = int(getattr(update, "user_id"))
                if user_id:
                    data["dialogId"] = user_id
            except Exception:
                pass

        if data["dialogId"] == 0:
            peer = None
            for attr in ("peer", "peer_id", "peerId"):
                try:
                    peer = getattr(update, attr)
                except Exception:
                    peer = get_private_field(update, attr)
                if peer is not None:
                    break
            data["dialogId"] = AlterEgo._extract_dialog_id_from_peer(peer)

        for attr in ("pts", "pts_count"):
            try:
                _ = getattr(update, attr)
            except Exception:
                pass

        for attr in ("qts", "seq"):
            try:
                _ = getattr(update, attr)
            except Exception:
                pass

        try:
            pending = get_private_field(update, "pending_req_id")
            data["pendingRequestId"] = int(pending)
        except Exception:
            pass

        return data

    @staticmethod
    def _extract_message_text(message):
        if message is None:
            return ""
        for attr in ("message", "text"):
            try:
                value = getattr(message, attr)
            except Exception:
                value = get_private_field(message, attr)
            if isinstance(value, str) and value.strip():
                return value
        return ""

    @staticmethod
    def _extract_message_id(message):
        if message is None:
            return 0
        for attr in ("id", "message_id"):
            try:
                value = getattr(message, attr)
            except Exception:
                value = get_private_field(message, attr)
            try:
                value_int = int(value)
                if value_int > 0:
                    return value_int
            except Exception:
                continue
        return 0

    @staticmethod
    def _extract_dialog_id_from_message(message):
        if message is None:
            return 0

        for attr in ("dialog_id", "dialogId"):
            try:
                value = getattr(message, attr)
            except Exception:
                value = get_private_field(message, attr)
            try:
                value_int = int(value)
                if value_int != 0:
                    return value_int
            except Exception:
                pass

        peer = None
        for attr in ("peer_id", "peerId", "to_id", "toId"):
            try:
                peer = getattr(message, attr)
            except Exception:
                peer = get_private_field(message, attr)
            if peer is not None:
                break

        return AlterEgo._extract_dialog_id_from_peer(peer)

    @staticmethod
    def _generate_random_id():
        value = int(random.getrandbits(63))
        if value == 0:
            value = int(random.getrandbits(62)) + 1
        return value

    @staticmethod
    def _extract_sender_user_id(message):
        if message is None:
            return 0

        from_id = None
        for attr in ("from_id", "fromId"):
            try:
                from_id = getattr(message, attr)
            except Exception:
                from_id = get_private_field(message, attr)
            if from_id is not None:
                break

        if from_id is None:
            return 0

        for attr in ("user_id", "userId"):
            try:
                value = getattr(from_id, attr)
            except Exception:
                value = get_private_field(from_id, attr)
            try:
                value_int = int(value)
                if value_int > 0:
                    return value_int
            except Exception:
                continue

        return 0

    @staticmethod
    def _extract_current_user_id(account):
        try:
            UserConfig = jclass("org.telegram.messenger.UserConfig")
            config = UserConfig.getInstance(int(account))
            return int(config.getClientUserId())
        except Exception:
            return 0

    def _is_outgoing_message(self, message, account):
        if message is None:
            return False

        for attr in ("out", "isOut"):
            try:
                v = getattr(message, attr)
            except Exception:
                v = get_private_field(message, attr)
            if isinstance(v, bool):
                return v
            try:
                v_int = int(v)
                return v_int != 0
            except Exception:
                continue

        current_user_id = self._extract_current_user_id(account)
        sender_user_id = self._extract_sender_user_id(message)
        return bool(
            current_user_id and sender_user_id and current_user_id == sender_user_id
        )

        return False

    def _try_store_pending_from_message(self, message):
        if message is None or not self._pending_stored_messages:
            return False

        text = self._extract_message_text(message)
        if not isinstance(text, str) or not text.strip():
            return False

        message_id = self._extract_message_id(message)
        if message_id <= 0:
            return False

        dialog_id = self._extract_dialog_id_from_message(message)

        now = self._now_ms()
        matched_index = -1
        for idx in range(len(self._pending_stored_messages) - 1, -1, -1):
            item = self._pending_stored_messages[idx]
            created_at = int(item.get("createdAtMs") or 0)
            if created_at and now - created_at > 180000:
                continue

            item_text = item.get("coverText")
            item_dialog = int(item.get("dialogId") or 0)
            if (
                isinstance(item_text, str)
                and item_text.strip() == text.strip()
                and (item_dialog == 0 or dialog_id == 0 or item_dialog == dialog_id)
            ):
                matched_index = idx
                break

        if matched_index < 0:
            for idx in range(len(self._pending_stored_messages) - 1, -1, -1):
                item = self._pending_stored_messages[idx]
                created_at = int(item.get("createdAtMs") or 0)
                if created_at and now - created_at > 30000:
                    continue
                item_dialog = int(item.get("dialogId") or 0)
                if item_dialog and dialog_id and item_dialog != dialog_id:
                    continue
                matched_index = idx
                self.log(
                    f"[AlterEgo] pending fallback match used: dialog={dialog_id}, messageId={message_id}"
                )
                break

        if matched_index < 0:
            self.log(
                f"[AlterEgo] no pending match: text='{text[:80]}', dialog={dialog_id}, pending={len(self._pending_stored_messages)}"
            )
            return False

        item = self._pending_stored_messages.pop(matched_index)
        api_url = self.get_setting("api_url", "")
        if not isinstance(api_url, str) or not self._is_url(api_url):
            return False

        normalized = api_url.rstrip("/")
        resolved_dialog = dialog_id if dialog_id else int(item.get("dialogId") or 0)
        original_text = item.get("originalText") or text
        cover_text = item.get("coverText") or ""
        if not isinstance(cover_text, str) or not cover_text.strip():
            return False
        if text.strip() != cover_text.strip():
            return False

        run_on_queue(
            lambda: self._store_message_sync(
                normalized,
                resolved_dialog,
                message_id,
                original_text,
                cover_text,
            )
        )
        self.log(
            f"[AlterEgo] updated stored message with real telegramMessageId={message_id}"
        )
        return True

    def _try_store_pending_from_update(self, update, account):
        if update is None or not self._pending_stored_messages:
            return False

        fields = self._extract_update_message_fields(update)

        message_id = int(fields.get("messageId") or 0)
        if message_id <= 0:
            return False

        dialog_id = int(fields.get("dialogId") or 0)
        pending_req_id = int(fields.get("pendingRequestId") or 0)
        random_id = int(fields.get("clientRandomId") or 0)
        text = fields.get("text") or ""
        current_user_id = self._extract_current_user_id(account)

        now = self._now_ms()
        matched_index = -1

        if random_id:
            for idx in range(len(self._pending_stored_messages) - 1, -1, -1):
                item = self._pending_stored_messages[idx]
                if int(item.get("clientRandomId") or 0) == random_id:
                    matched_index = idx
                    self.log(
                        f"[AlterEgo] pending match by random_id={random_id}, messageId={message_id}"
                    )
                    break

        if matched_index < 0 and pending_req_id:
            for idx in range(len(self._pending_stored_messages) - 1, -1, -1):
                item = self._pending_stored_messages[idx]
                if int(item.get("pendingRequestId") or 0) == pending_req_id:
                    matched_index = idx
                    self.log(
                        f"[AlterEgo] pending match by pending_req_id={pending_req_id}, messageId={message_id}"
                    )
                    break

        if matched_index < 0:
            # For short-sent updates with no random/pending ids,
            # match by account+dialog and nearest timestamp.
            for idx in range(len(self._pending_stored_messages) - 1, -1, -1):
                item = self._pending_stored_messages[idx]
                created_at = int(item.get("createdAtMs") or 0)
                if created_at and now - created_at > 30000:
                    continue
                item_dialog = int(item.get("dialogId") or 0)
                if dialog_id and item_dialog and dialog_id != item_dialog:
                    continue
                item_account = int(item.get("account") or 0)
                if current_user_id and item_account and current_user_id != item_account:
                    continue
                matched_index = idx
                self.log(
                    f"[AlterEgo] pending fallback by account+dialog, messageId={message_id}"
                )
                break

        if matched_index < 0:
            for idx in range(len(self._pending_stored_messages) - 1, -1, -1):
                item = self._pending_stored_messages[idx]
                created_at = int(item.get("createdAtMs") or 0)
                if created_at and now - created_at > 180000:
                    continue

                item_text = item.get("coverText")
                item_dialog = int(item.get("dialogId") or 0)
                if (
                    isinstance(item_text, str)
                    and isinstance(text, str)
                    and item_text.strip() == text.strip()
                    and (item_dialog == 0 or dialog_id == 0 or item_dialog == dialog_id)
                ):
                    matched_index = idx
                    break

        if matched_index < 0:
            self.log(
                f"[AlterEgo] no raw-update pending match: msgId={message_id}, dialog={dialog_id}, randomId={random_id}, pendingReqId={pending_req_id}, pending={len(self._pending_stored_messages)}"
            )

        if matched_index < 0:
            return False

        item = self._pending_stored_messages.pop(matched_index)
        api_url = self.get_setting("api_url", "")
        if not isinstance(api_url, str) or not self._is_url(api_url):
            return False

        normalized = api_url.rstrip("/")
        resolved_dialog = dialog_id if dialog_id else int(item.get("dialogId") or 0)
        original_text = item.get("originalText") or text
        cover_text = item.get("coverText") or ""

        if not isinstance(cover_text, str) or not cover_text.strip():
            return False

        run_on_queue(
            lambda: self._store_message_sync(
                normalized,
                resolved_dialog,
                message_id,
                original_text,
                cover_text,
            )
        )
        self.log(
            f"[AlterEgo] updated stored message with real telegramMessageId={message_id}"
        )
        return True

    def _resend_masked_text_async(
        self,
        account,
        params_meta,
        masked_text,
        api_base_url,
        source_text,
        source_params,
    ):
        if not isinstance(masked_text, str) or not masked_text.strip():
            return

        self._bypass_text_counts[masked_text] = (
            int(self._bypass_text_counts.get(masked_text, 0)) + 1
        )

        def _worker():
            try:
                from java import jclass

                SendMessagesHelper = jclass("org.telegram.messenger.SendMessagesHelper")
                helper = SendMessagesHelper.getInstance(int(account))
                if helper is None:
                    self.log("[AlterEgo] resend failed: SendMessagesHelper is None")
                    return

                resend_param_keys = set()
                send_params = source_params
                if send_params is None:
                    SendMessageParamsClass = jclass(
                        "org.telegram.messenger.SendMessagesHelper$SendMessageParams"
                    )
                    send_params = SendMessageParamsClass()
                    for key, value in params_meta.items():
                        if value is None:
                            continue
                        try:
                            setattr(send_params, key, value)
                            resend_param_keys.add(key)
                        except Exception:
                            try:
                                if set_private_field(send_params, key, value):
                                    resend_param_keys.add(key)
                            except Exception:
                                pass

                random_id = int(params_meta.get("clientRandomId") or 0)
                if not random_id:
                    for key in ("random_id", "randomId", "clientRandomId"):
                        try:
                            value = getattr(send_params, key)
                        except Exception:
                            value = get_private_field(send_params, key)
                        try:
                            random_id = int(value)
                            if random_id:
                                break
                        except Exception:
                            continue

                if not random_id:
                    random_id = self._generate_random_id()
                    params_meta["clientRandomId"] = random_id

                for key in ("random_id", "randomId", "clientRandomId"):
                    try:
                        setattr(send_params, key, random_id)
                        resend_param_keys.add(key)
                    except Exception:
                        try:
                            if set_private_field(send_params, key, random_id):
                                resend_param_keys.add(key)
                        except Exception:
                            pass

                try:
                    setattr(send_params, "message", masked_text)
                    resend_param_keys.add("message")
                except Exception:
                    if not set_private_field(send_params, "message", masked_text):
                        self.log("[AlterEgo] resend failed: cannot set message")
                        return

                helper.sendMessage(send_params)
                self.log(
                    f"[AlterEgo] resend done with keys={sorted(list(resend_param_keys))}"
                )

                dialog_id = params_meta.get("peer")
                self._pending_stored_messages.append(
                    {
                        "account": self._extract_current_user_id(account),
                        "dialogId": int(dialog_id) if isinstance(dialog_id, int) else 0,
                        "originalText": source_text,
                        "coverText": masked_text,
                        "createdAtMs": self._now_ms(),
                        "pendingRequestId": int(
                            params_meta.get("pendingRequestId") or 0
                        ),
                        "clientRandomId": int(params_meta.get("clientRandomId") or 0),
                    }
                )
                self.log(
                    f"[AlterEgo] pending added: dialog={int(dialog_id) if isinstance(dialog_id, int) else 0}, pendingReqId={int(params_meta.get('pendingRequestId') or 0)}, randomId={int(params_meta.get('clientRandomId') or 0)}, cover='{masked_text[:80]}'"
                )
                if len(self._pending_stored_messages) > 120:
                    self._pending_stored_messages = self._pending_stored_messages[-120:]

                self._show_bulletin("Отправлено с маской", "success")
            except Exception as error:
                self.log(f"[AlterEgo] resend failed: {error}")
                self.log(f"[AlterEgo] resend traceback: {traceback.format_exc()}")
                self._show_bulletin("Ошибка повторной отправки", "error")

        run_on_queue(_worker)

    def _consume_bypass_text(self, text):
        if not isinstance(text, str) or not text:
            return False
        count = int(self._bypass_text_counts.get(text, 0))
        if count <= 0:
            return False
        if count == 1:
            self._bypass_text_counts.pop(text, None)
        else:
            self._bypass_text_counts[text] = count - 1
        return True

    @staticmethod
    def _is_ui_thread():
        try:
            from java import jclass

            Looper = jclass("android.os.Looper")
            return Looper.myLooper() == Looper.getMainLooper()
        except Exception:
            return False

    def _process_mask_pipeline(self, dialog_id, source_text, api_url):
        if not isinstance(source_text, str) or not source_text.strip():
            return False, "", "Empty source text"
        if not isinstance(api_url, str) or not self._is_url(api_url):
            return False, "", "Invalid API URL"

        cache_key = (int(dialog_id) if isinstance(dialog_id, int) else 0, source_text)
        cached = self._mask_cache.get(cache_key)
        if isinstance(cached, str) and cached.strip():
            self.log("[AlterEgo] mask cache hit")
            return True, cached, ""

        normalized = api_url.rstrip("/")
        success, cover_text, error_text = self._call_mask_sync(
            normalized, dialog_id, source_text
        )
        if not success or not isinstance(cover_text, str) or not cover_text.strip():
            return False, "", error_text or "Masking failed"
        self._mask_cache[cache_key] = cover_text
        if len(self._mask_cache) > 80:
            try:
                oldest_key = next(iter(self._mask_cache.keys()))
                self._mask_cache.pop(oldest_key, None)
            except Exception:
                pass
        return True, cover_text, ""

    def _store_message_sync(
        self, api_base_url, dialog_id, telegram_message_id, source_text, cover_text
    ):
        headers = self._auth_headers(api_base_url)
        payload = {
            "dialogId": int(dialog_id) if isinstance(dialog_id, int) else 0,
            "telegramMessageId": int(telegram_message_id)
            if isinstance(telegram_message_id, int)
            else 0,
            "originalText": source_text,
            "coverText": cover_text,
            "createdAt": datetime.now(timezone.utc).isoformat(),
        }

        last_error = ""
        for attempt in range(3):
            try:
                response = requests.post(
                    f"{api_base_url}/api/Messages",
                    json=payload,
                    headers=headers,
                    timeout=4,
                )
            except Exception as error:
                last_error = str(error)
                self.log(f"[AlterEgo] store message failed: {error}")
                if attempt < 2:
                    time.sleep(0.35)
                    continue
                return

            self.log(
                f"[AlterEgo] store message status: {response.status_code}, dialog_id={payload['dialogId']}, message_id={payload['telegramMessageId']}"
            )
            if response.status_code >= 500:
                last_error = self._extract_server_message(
                    response, "Store message failed"
                )
                if attempt < 2:
                    time.sleep(0.35)
                    continue
            return

    def on_send_message_hook(self, account: int, params: Any) -> HookResult:
        try:
            current_thread = threading.current_thread().name
            self.log(
                f"[AlterEgo] on_send_message_hook thread={current_thread}, is_ui={self._is_ui_thread()}"
            )
            self.log(
                f"[AlterEgo] on_send_message_hook called: account={account}, params_type={type(params)}"
            )
            self.log(f"[AlterEgo] send params dump: {params}")
            original_text = self._extract_text_from_send_params(params)
            if not isinstance(original_text, str) or not original_text.strip():
                self.log("[AlterEgo] send pipeline: empty text, skip")
                return HookResult(strategy=HookStrategy.DEFAULT, params=params)

            self.log(f"[AlterEgo] send pipeline source text: {original_text[:120]}")

            if self._consume_bypass_text(original_text):
                self.log("[AlterEgo] send pipeline: bypass for internal resend")
                return HookResult(strategy=HookStrategy.DEFAULT, params=params)

            api_url = self.get_setting("api_url", "")
            if not isinstance(api_url, str) or not self._is_url(api_url):
                self.log("[AlterEgo] fail-closed: invalid API URL, cancel send")
                self._show_bulletin("Отправка отменена: неверный API URL", "error")
                return HookResult(strategy=HookStrategy.CANCEL, params=params)

            params_meta = self._extract_send_params_meta(params)
            self._show_bulletin("Маскирую сообщение...", "info")
            dialog_id = params_meta.get("peer")

            def _worker():
                success, masked_text, error_text = self._process_mask_pipeline(
                    dialog_id, original_text, api_url
                )
                if (
                    not success
                    or not isinstance(masked_text, str)
                    or not masked_text.strip()
                ):
                    self.log(
                        f"[AlterEgo] fail-closed: masking failed, send cancelled ({error_text})"
                    )
                    self._show_bulletin(
                        f"Отправка отменена: {error_text or 'ошибка маскирования'}",
                        "error",
                    )
                    return

                cache_key = (
                    int(dialog_id) if isinstance(dialog_id, int) else 0,
                    original_text,
                )
                self._mask_cache[cache_key] = masked_text
                if len(self._mask_cache) > 80:
                    try:
                        oldest_key = next(iter(self._mask_cache.keys()))
                        self._mask_cache.pop(oldest_key, None)
                    except Exception:
                        pass

                self._resend_masked_text_async(
                    account,
                    params_meta,
                    masked_text,
                    api_url.rstrip("/"),
                    original_text,
                    params,
                )

            run_on_queue(_worker)
            self.log("[AlterEgo] fail-closed: cancel original send, start async mask")
            return HookResult(strategy=HookStrategy.CANCEL, params=params)
        except Exception as error:
            self.log(f"[AlterEgo] send pipeline error: {error}")
            self.log(f"[AlterEgo] send pipeline traceback: {traceback.format_exc()}")
            return HookResult(strategy=HookStrategy.DEFAULT, params=params)

    def on_update_hook(self, update_name: str, account: int, update: Any) -> HookResult:
        try:
            if not self._pending_stored_messages:
                return HookResult(strategy=HookStrategy.DEFAULT, update=update)
            if self._try_store_pending_from_update(update, account):
                self.log("[AlterEgo] on_update_hook: stored by raw update")
                return HookResult(strategy=HookStrategy.DEFAULT, update=update)
            message = self._extract_message_from_update(update)
            if self._try_store_pending_from_message(message):
                self.log("[AlterEgo] on_update_hook: stored by single update")
            return HookResult(strategy=HookStrategy.DEFAULT, update=update)
        except Exception as error:
            self.log(f"[AlterEgo] on_update_hook error: {error}")
            self.log(f"[AlterEgo] on_update_hook traceback: {traceback.format_exc()}")
            return HookResult(strategy=HookStrategy.DEFAULT, update=update)

    def on_updates_hook(
        self, container_name: str, account: int, updates: Any
    ) -> HookResult:
        try:
            if not self._pending_stored_messages or updates is None:
                return HookResult(strategy=HookStrategy.DEFAULT, updates=updates)

            items = None
            try:
                items = getattr(updates, "updates")
            except Exception:
                items = get_private_field(updates, "updates")

            if items is None:
                self.log(
                    f"[AlterEgo] on_updates_hook: no updates field for container={container_name}"
                )
                return HookResult(strategy=HookStrategy.DEFAULT, updates=updates)

            def _iter_items(java_or_py_list):
                if java_or_py_list is None:
                    return []
                try:
                    size = int(java_or_py_list.size())
                    return [java_or_py_list.get(i) for i in range(size)]
                except Exception:
                    try:
                        return list(java_or_py_list)
                    except Exception:
                        return []

            processed = 0
            for update in _iter_items(items):
                processed += 1
                if self._try_store_pending_from_update(update, account):
                    self.log(
                        f"[AlterEgo] on_updates_hook: stored from raw update container={container_name}, processed={processed}"
                    )
                    break
                message = self._extract_message_from_update(update)
                if self._try_store_pending_from_message(message):
                    self.log(
                        f"[AlterEgo] on_updates_hook: stored from container={container_name}, processed={processed}"
                    )
                    break
            else:
                self.log(
                    f"[AlterEgo] on_updates_hook: no store match in container={container_name}, processed={processed}"
                )

            return HookResult(strategy=HookStrategy.DEFAULT, updates=updates)
        except Exception as error:
            self.log(f"[AlterEgo] on_updates_hook error: {error}")
            self.log(f"[AlterEgo] on_updates_hook traceback: {traceback.format_exc()}")
            return HookResult(strategy=HookStrategy.DEFAULT, updates=updates)

    def _status_banner_text(self, chat_activity=None):
        # User-defined text has priority over computed status text.
        custom_text = self.get_setting("status_text", "")
        if isinstance(custom_text, str):
            custom_text = custom_text.strip()
            if custom_text:
                return custom_text
        return f"Alter Ego - {self._status(chat_activity)}"

    def _auth_status_text(self):
        session_user = self.get_setting("auth_session_user", "")
        if self._auth_access_token:
            return f"Вы вошли как {self._auth_username_text()}"
        if isinstance(session_user, str) and session_user.strip():
            return f"Вы вошли как {session_user.strip()}"
        return "Вход не выполнен"

    def _auth_menu_text(self):
        return self._auth_status_text()

    def _auth_username_text(self):
        username = self.get_setting("auth_username", "")
        if isinstance(username, str) and username.strip():
            return username.strip()
        return "пользователь"

    # endregion

    # region Helpers
    @staticmethod
    def _chat_key(chat_activity):
        try:
            return int(chat_activity.hashCode())
        except Exception:
            return str(chat_activity)

    @staticmethod
    def _now_ms():
        return int(time.time() * 1000)

    def _set_native_date_mode(self, chat_activity, value):
        if chat_activity is None:
            return
        self._native_date_mode[self._chat_key(chat_activity)] = bool(value)

    def _is_native_date_mode(self, chat_activity):
        if chat_activity is None:
            return False
        return bool(self._native_date_mode.get(self._chat_key(chat_activity), False))

    # endregion

    # region Interaction callbacks
    def _on_status_tap(self, chat_activity):
        if chat_activity is None:
            return

    def _on_status_long_press(self, chat_activity):
        if chat_activity is None:
            self.log("[AlterEgo][diag] long press handler: chat is None")
            return

        self.log("[AlterEgo][diag] long press handler: opening plugin settings")
        self._open_plugin_settings()

    def _create_auth_sub_fragment(self):
        items = [Header(text="Вход в Alter Ego API")]

        if self._auth_access_token:
            items.append(
                Text(text=f"Логин: {self._auth_username_text()}", icon="user_solar")
            )
            items.append(Text(text="Пароль: ********", icon="lock_password"))
            items.append(
                Text(
                    text="Выйти",
                    red=True,
                    icon="cross_circle_solar",
                    on_click=self._logout_from_auth_page,
                )
            )
            return items

        items.append(
            Input(
                key="auth_username",
                text="Логин",
                default=self.get_setting("auth_username", ""),
                icon="user_solar",
            )
        )
        items.append(
            Input(
                key="auth_password",
                text="Пароль",
                default=self.get_setting("auth_password", ""),
                icon="lock_password",
            )
        )
        items.append(
            Text(
                text="Войти",
                accent=True,
                icon="msg_send_solar",
                on_click=self._login_from_auth_page,
            )
        )

        return items

    def _show_bulletin(self, message, kind="info"):
        if not isinstance(message, str) or not message.strip():
            return

        def _show():
            try:
                fragment = get_last_fragment()
                if kind == "success":
                    BulletinHelper.show_success(message, fragment)
                elif kind == "error":
                    BulletinHelper.show_error(message, fragment)
                else:
                    BulletinHelper.show_info(message, fragment)
            except Exception as error:
                self.log(f"[AlterEgo] Bulletin failed: {error}")

        run_on_ui_thread(_show)

    def _login_from_auth_page(self, _view=None):
        api_url = self.get_setting("api_url", "")
        username = self.get_setting("auth_username", "")
        password = self.get_setting("auth_password", "")

        username = username.strip() if isinstance(username, str) else ""
        password = password.strip() if isinstance(password, str) else ""
        if not isinstance(api_url, str) or not self._is_url(api_url):
            self._auth_feedback = "Некорректный API URL"
            self._show_bulletin(self._auth_feedback, "error")
            return
        if not username or not password:
            self._auth_feedback = "Заполните логин и пароль"
            self._show_bulletin(self._auth_feedback, "error")
            return

        self.set_setting("auth_username", username)
        self.set_setting("auth_password", password)
        self._auth_feedback = "Вход..."
        self._show_bulletin("Вход...", "info")
        normalized = api_url.rstrip("/")

        def _worker():
            success, message = self._login_with_credentials_sync(
                normalized, username, password
            )

            def _apply():
                self._auth_feedback = message
                self._status_cache = "Connected" if success else "Unauthorized"
                self._status_cache_at_ms = self._now_ms()
                self._show_bulletin(message, "success" if success else "error")
                if success:
                    self._open_plugin_settings()

            run_on_ui_thread(_apply)

        run_on_queue(_worker)

    def _logout_from_auth_page(self, _view=None):
        api_url = self.get_setting("api_url", "")
        normalized = api_url.rstrip("/") if isinstance(api_url, str) else ""
        self._auth_feedback = "Выход..."
        self._show_bulletin("Выход...", "info")

        def _worker():
            try:
                if (
                    normalized
                    and self._is_url(normalized)
                    and isinstance(self._auth_access_token, str)
                    and self._auth_access_token
                ):
                    requests.post(
                        f"{normalized}/api/Auth/logout",
                        timeout=2,
                        headers={"Authorization": f"Bearer {self._auth_access_token}"},
                    )
            except Exception:
                pass

            self._auth_access_token = ""
            self._auth_refresh_token = ""
            self._auth_expires_at_ms = 0
            self.set_setting("auth_session_user", "", reload_settings=True)
            self._status_cache = "Auth Required"
            self._status_cache_at_ms = self._now_ms()
            self._auth_feedback = "Выход выполнен"

            def _apply():
                self._show_bulletin("Выход выполнен", "success")
                self._open_plugin_settings()

            run_on_ui_thread(_apply)

        run_on_queue(_worker)

    # endregion

    # region Floating date flow
    def _mark_scroll_activity(self, chat_activity):
        if chat_activity is None:
            return
        key = self._chat_key(chat_activity)
        self._last_scroll_activity_ms[key] = self._now_ms()

    def _apply_floating_date_status(self, chat_activity):
        if chat_activity is None:
            return

        floating_date_view = self._floating_view.apply_status_text(
            chat_activity, self._status_banner_text(chat_activity)
        )
        if floating_date_view is None:
            return

        self._status_touch.register_cell(floating_date_view, chat_activity)

    # region Plugin settings navigation
    def _open_plugin_settings(self):
        plugin_id = self.id if getattr(self, "id", None) else __id__
        plugins_controller_class = find_class(
            "com.exteragram.messenger.plugins.PluginsController"
        )
        if not plugins_controller_class:
            self.log("[AlterEgo] PluginsController class not found")
            return

        controller = None
        for args in ((), (0,)):
            try:
                controller = plugins_controller_class.getInstance(*args)
                if controller is not None:
                    break
            except Exception:
                continue

        if controller is None:
            self.log("[AlterEgo] PluginsController instance not found")
            return

        try:
            controller.openPluginSettings(plugin_id)
        except Exception as error:
            self.log(f"[AlterEgo] Failed to open plugin settings: {error}")

    # endregion

    # region Timers
    def _schedule_auto_status_return(self, chat_activity):
        if chat_activity is None:
            return

        key = self._chat_key(chat_activity)
        marker = self._last_scroll_activity_ms.get(key, 0)

        def _restore_if_idle():
            if not self._is_native_date_mode(chat_activity):
                return
            last = self._last_scroll_activity_ms.get(key, 0)
            if last != marker:
                return
            self._set_native_date_mode(chat_activity, False)
            self._apply_floating_date_status(chat_activity)

        run_on_ui_thread(_restore_if_idle, STATUS_RETURN_DELAY_MS)

    # endregion

    # region Native date restore
    def _restore_native_floating_date(self, chat_activity):
        if chat_activity is None:
            return

        floating_date_view = self._floating_view.restore_native_date(chat_activity)
        if floating_date_view is None:
            return

        self._status_touch.unregister_cell(floating_date_view)

    # endregion
    # endregion

    # region Settings
    def create_settings(self) -> List[Any]:
        return [
            Header(text="Настройки AlterEgo"),
            Divider(text="Статус"),
            Text(
                text=self._auth_menu_text(),
                accent=True,
                icon="msg_info_solar",
                create_sub_fragment=self._create_auth_sub_fragment,
            ),
            Divider(text="Настройки"),
            Input(key="api_url", text="API URL", default="", icon="ai_chat_solar"),
        ]

    # endregion
