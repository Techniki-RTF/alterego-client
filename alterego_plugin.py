# region Metadata
__name__ = "AlterEgo"
__description__ = "Шифрование сообщений с использование семантической трансформации"
__version__ = "0.0.1"
__id__ = "alter_ego"
__author__ = "@renamq"
__icon__ = "exteraPlugins/1"
# endregion

import random
import math
import threading
import time
import traceback
from java import jclass
from datetime import datetime, timezone
from typing import Any, List
from urllib.parse import urlparse

import requests
from android_utils import run_on_ui_thread, OnClickListener
from base_plugin import (
    BasePlugin,
    MethodHook,
    HookResult,
    HookStrategy,
    MenuItemData,
    MenuItemType,
)
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
MASK_TIMEOUT_SEC = 30
DECODE_TIMEOUT_SEC = 5
DECODE_TIME_MARKER = " 🔓"


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


class ChatMessageBindHook(MethodHook):
    def __init__(self, plugin):
        self.plugin = plugin

    def after_hooked_method(self, param):
        try:
            message_object = None
            if param.args and len(param.args) > 0:
                message_object = param.args[0]
            if message_object is None:
                message_object = get_private_field(
                    param.thisObject, "currentMessageObject"
                )
            account = self.plugin._extract_account_from_cell(param.thisObject)
            self.plugin._decode_message_object_async(
                message_object, account, cell=param.thisObject
            )
        except Exception as error:
            self.plugin.log(f"[AlterEgo] ChatMessage bind hook failed: {error}")


class ChatMessageTimeHook(MethodHook):
    def __init__(self, plugin):
        self.plugin = plugin

    def after_hooked_method(self, param):
        try:
            self.plugin._apply_decoded_time_icon(param.thisObject)
        except Exception as error:
            self.plugin.log(f"[AlterEgo] ChatMessage time hook failed: {error}")


class ChatMessageDrawHook(MethodHook):
    def __init__(self, plugin):
        self.plugin = plugin

    def after_hooked_method(self, param):
        try:
            canvas = param.args[0] if param.args and len(param.args) > 0 else None
            self.plugin._draw_decoded_marker_on_canvas(param.thisObject, canvas)
        except Exception:
            pass


# endregion


class AlterEgo(BasePlugin):
    # region Lifecycle
    def __init__(self):
        super().__init__()
        self._chat_create_view_unhooks = []
        self._chat_show_floating_date_unhooks = []
        self._chat_hide_floating_date_unhooks = []
        self._chat_action_touch_unhooks = []
        self._chat_message_bind_unhooks = []
        self._chat_message_time_unhooks = []
        self._chat_message_draw_unhooks = []
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
        self._decode_cache = {}
        self._decoded_message_texts = {}
        self._decode_in_flight = set()
        self._decode_denied = {}
        self._decode_recent_applied = {}
        self._bypass_text_counts = {}
        self._pending_stored_messages = []
        self._pending_lock = threading.Lock()
        self._status_refresh_lock = threading.Lock()
        self._last_successful_send_at_ms = {}
        self._status_touch = StatusTouchHelper(self)
        self._floating_view = FloatingDateViewHelper(self)
        self._dialog_menu_item_id = None

    def on_plugin_load(self):
        try:
            chat_activity_class = find_class("org.telegram.ui.ChatActivity")
            if not chat_activity_class:
                self.log("[AlterEgo] ChatActivity class not found")
                return

            try:
                self._register_dialog_toggle_menu()
            except Exception as error:
                self.log(f"[AlterEgo] Dialog menu item registration failed: {error}")

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

            try:
                chat_message_cell_class = find_class(
                    "org.telegram.ui.Cells.ChatMessageCell"
                )
                if chat_message_cell_class:
                    bind_hooks = []
                    for method_name in (
                        "setMessageObject",
                        "setMessageObjectInternal",
                        "setMessageObjectInternal2",
                    ):
                        unhooks = (
                            self.hook_all_methods(
                                chat_message_cell_class,
                                method_name,
                                ChatMessageBindHook(self),
                                priority=90,
                            )
                            or []
                        )
                        bind_hooks.extend(unhooks)
                    self._chat_message_bind_unhooks = bind_hooks

                    time_hooks = []
                    for method_name in ("measureTime", "measureTimeForMessage"):
                        unhooks = (
                            self.hook_all_methods(
                                chat_message_cell_class,
                                method_name,
                                ChatMessageTimeHook(self),
                                priority=90,
                            )
                            or []
                        )
                        time_hooks.extend(unhooks)
                    self._chat_message_time_unhooks = time_hooks

                    draw_hooks = []
                    for method_name in ("onDraw",):
                        unhooks = (
                            self.hook_all_methods(
                                chat_message_cell_class,
                                method_name,
                                ChatMessageDrawHook(self),
                                priority=90,
                            )
                            or []
                        )
                        draw_hooks.extend(unhooks)
                    self._chat_message_draw_unhooks = draw_hooks
            except Exception as error:
                self.log(f"[AlterEgo] ChatMessage hooks failed: {error}")

            self.log(
                f"[AlterEgo] Hooks active: createView={len(self._chat_create_view_unhooks)}, "
                f"showFloatingDateView={len(self._chat_show_floating_date_unhooks)}, "
                f"hideFloatingDateView={len(self._chat_hide_floating_date_unhooks)}, "
                f"chatActionTouch={len(self._chat_action_touch_unhooks)}, "
                f"chatMessageBind={len(self._chat_message_bind_unhooks)}, "
                f"chatMessageTime={len(self._chat_message_time_unhooks)}, "
                f"chatMessageDraw={len(self._chat_message_draw_unhooks)}"
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
                    "messages.sendMessage",
                    "messages.sendMedia",
                    "messages.sendMultiMedia",
                    "sendMessage",
                    "sendMedia",
                    "sendMultiMedia",
                ]
                for hook_name in request_hook_names:
                    hook_id = self.add_hook(
                        hook_name, match_substring=True, priority=80
                    )
                    if hook_id is not None:
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
                    "TL_updateShortSentMessage",
                    "TL_updateShortMessage",
                    "TL_updateShortChatMessage",
                    "TL_updateEditMessage",
                    "TL_updateEditChannelMessage",
                    "updateShortSentMessage",
                    "updateShortMessage",
                    "updateNewMessage",
                    "TL_update",
                    "updates",
                    "TL_updates",
                    "updatesCombined",
                    "TL_updatesCombined",
                    "updateShort",
                    "Updates",
                ]
                for hook_name in update_hook_names:
                    hook_id = self.add_hook(
                        hook_name, match_substring=True, priority=80
                    )
                    if hook_id is not None:
                        self._update_hook_ids.append(hook_id)
                self.log(
                    f"[AlterEgo] Update hooks registered: {len(self._update_hook_ids)}"
                )
            except Exception as error:
                self.log(f"[AlterEgo] Update hook registration failed: {error}")

        except Exception as error:
            self.log(f"[AlterEgo] Hook setup failed: {error}")
            self.log(f"[AlterEgo] Hook setup traceback: {traceback.format_exc()}")

    def on_plugin_unload(self):
        for hook_obj in (
            self._chat_create_view_unhooks
            + self._chat_show_floating_date_unhooks
            + self._chat_hide_floating_date_unhooks
            + self._chat_action_touch_unhooks
            + self._chat_message_bind_unhooks
            + self._chat_message_time_unhooks
            + self._chat_message_draw_unhooks
        ):
            try:
                self.unhook_method(hook_obj)
            except Exception as error:
                self.log(f"[AlterEgo] unhook failed: {error}")

        for hook_id in self._update_hook_ids + self._request_hook_ids:
            try:
                self.remove_hook(hook_id)
            except Exception as error:
                self.log(f"[AlterEgo] remove_hook({hook_id}) failed: {error}")

        self._chat_create_view_unhooks = []
        self._chat_show_floating_date_unhooks = []
        self._chat_hide_floating_date_unhooks = []
        self._chat_action_touch_unhooks = []
        self._chat_message_bind_unhooks = []
        self._chat_message_time_unhooks = []
        self._chat_message_draw_unhooks = []
        self._update_hook_ids = []
        self._request_hook_ids = []
        self._send_hook_registered = False

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
    def _extract_id_from_object(obj):
        if obj is None:
            return 0
        for attr in ("id", "user_id", "chat_id", "channel_id"):
            try:
                value = getattr(obj, attr)
            except Exception:
                value = get_private_field(obj, attr)
            try:
                value_int = int(value)
                if value_int:
                    return value_int
            except Exception:
                continue
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
        try:
            message_owner = getattr(update, "messageOwner")
            if message_owner is not None:
                return message_owner
        except Exception:
            pass
        try:
            message_owner = get_private_field(update, "messageOwner")
            if message_owner is not None:
                return message_owner
        except Exception:
            pass
        try:
            message_owner = update.getMessageOwner()
            if message_owner is not None:
                return message_owner
        except Exception:
            pass
        return None

    @staticmethod
    def _coerce_long_int(value):
        if value is None:
            return 0
        try:
            return int(value)
        except Exception:
            pass
        for meth in ("longValue", "intValue"):
            try:
                return int(getattr(value, meth)())
            except Exception:
                pass
        try:
            return int(str(value).strip())
        except Exception:
            return 0

    @staticmethod
    def _extract_update_message_fields(update):
        data = {
            "pendingRequestId": 0,
            "clientRandomId": 0,
            "messageId": 0,
            "dialogId": 0,
            "senderTelegramId": 0,
            "receivedAt": "",
            "text": "",
            "out": False,
        }

        if update is None:
            return data

        message = AlterEgo._extract_message_from_update(update)
        data["text"] = AlterEgo._extract_message_text(message)
        data["messageId"] = AlterEgo._extract_message_id(message)
        data["dialogId"] = AlterEgo._extract_dialog_id_from_message(message)
        data["senderTelegramId"] = AlterEgo._extract_sender_user_id(message)
        data["out"] = False

        received_at_ms = AlterEgo._extract_message_date_ms(message)
        if received_at_ms <= 0:
            received_at_ms = AlterEgo._extract_message_date_ms(update)
        data["receivedAt"] = AlterEgo._to_utc_iso(received_at_ms)

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
                    data["out"] = int(v) != 0
                    break
                except Exception:
                    pass

            for attr in ("random_id", "randomId", "randomId64"):
                rid = None
                try:
                    rid = getattr(message, attr)
                except Exception:
                    rid = get_private_field(message, attr)
                coerced = AlterEgo._coerce_long_int(rid)
                if coerced:
                    data["clientRandomId"] = coerced
                    break

        # Fallback for update types without nested Message object
        if data["messageId"] <= 0:
            for attr in ("id", "message_id", "messageId"):
                try:
                    data["messageId"] = AlterEgo._coerce_long_int(getattr(update, attr))
                    if data["messageId"] > 0:
                        break
                except Exception:
                    try:
                        data["messageId"] = AlterEgo._coerce_long_int(
                            get_private_field(update, attr)
                        )
                        if data["messageId"] > 0:
                            break
                    except Exception:
                        continue

        if not data["text"]:
            try:
                maybe_text = getattr(update, "message")
            except Exception:
                maybe_text = get_private_field(update, "message")
            if isinstance(maybe_text, str):
                data["text"] = maybe_text

        if data["clientRandomId"] == 0:
            for attr in ("random_id", "randomId", "randomId64"):
                try:
                    data["clientRandomId"] = AlterEgo._coerce_long_int(
                        getattr(update, attr)
                    )
                    if data["clientRandomId"]:
                        break
                except Exception:
                    try:
                        data["clientRandomId"] = AlterEgo._coerce_long_int(
                            get_private_field(update, attr)
                        )
                        if data["clientRandomId"]:
                            break
                    except Exception:
                        continue

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

        if not data["out"]:
            for attr in ("out", "isOut"):
                try:
                    v = getattr(update, attr)
                except Exception:
                    v = get_private_field(update, attr)
                if isinstance(v, bool):
                    data["out"] = v
                    break
                try:
                    data["out"] = int(v) != 0
                    break
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
    def _extract_message_date_ms(obj):
        if obj is None:
            return 0
        for attr in ("date", "date_ms", "dateMs"):
            try:
                value = getattr(obj, attr)
            except Exception:
                value = get_private_field(obj, attr)
            try:
                value_int = int(value)
                if value_int <= 0:
                    continue
                if value_int < 100000000000:
                    return value_int * 1000
                return value_int
            except Exception:
                continue
        return 0

    @staticmethod
    def _to_utc_iso(timestamp_ms):
        try:
            if int(timestamp_ms) <= 0:
                return ""
            dt = datetime.fromtimestamp(int(timestamp_ms) / 1000.0, tz=timezone.utc)
            return dt.isoformat()
        except Exception:
            return ""

    @staticmethod
    def _set_message_text(message, text):
        if message is None or not isinstance(text, str):
            return False
        for attr in ("message", "text"):
            try:
                setattr(message, attr, text)
                return True
            except Exception:
                pass
            try:
                if set_private_field(message, attr, text):
                    return True
            except Exception:
                pass
        return False

    @staticmethod
    def _get_message_time_text(cell):
        if cell is None:
            return ""
        for attr in ("currentTimeString", "timeText", "timeString"):
            try:
                value = getattr(cell, attr)
            except Exception:
                value = get_private_field(cell, attr)
            if isinstance(value, str) and value:
                return value
        return ""

    @staticmethod
    def _set_message_time_text(cell, text):
        if cell is None or not isinstance(text, str):
            return False
        for attr in ("currentTimeString", "timeText", "timeString"):
            try:
                setattr(cell, attr, text)
                return True
            except Exception:
                pass
            try:
                if set_private_field(cell, attr, text):
                    return True
            except Exception:
                pass
        return False

    @staticmethod
    def _mark_message_decoded(message):
        if message is None:
            return
        try:
            setattr(message, "alterEgoDecoded", True)
            return
        except Exception:
            pass
        try:
            set_private_field(message, "alterEgoDecoded", True)
        except Exception:
            pass

    @staticmethod
    def _set_object_text_fields(obj, text):
        if obj is None or not isinstance(text, str):
            return False
        changed = False
        for attr in (
            "message",
            "text",
            "messageText",
            "messageTextString",
        ):
            try:
                setattr(obj, attr, text)
                changed = True
                continue
            except Exception:
                pass
            try:
                if set_private_field(obj, attr, text):
                    changed = True
            except Exception:
                pass
        return changed

    @staticmethod
    def _is_message_marked_decoded(message):
        if message is None:
            return False
        for attr in ("alterEgoDecoded",):
            try:
                value = getattr(message, attr)
            except Exception:
                value = get_private_field(message, attr)
            if value is True:
                return True
        return False

    @staticmethod
    def _rebuild_time_layout_for_cell(cell, time_text):
        if cell is None or not isinstance(time_text, str) or not time_text:
            return False
        try:
            Theme = jclass("org.telegram.ui.ActionBar.Theme")
            StaticLayout = jclass("android.text.StaticLayout")
            Alignment = jclass("android.text.Layout$Alignment")
            paint = getattr(Theme, "chat_timePaint")
            if paint is None:
                return False
            width = int(
                math.ceil(float(paint.measureText(time_text, 0, len(time_text))))
            )
            layout = StaticLayout(
                time_text,
                paint,
                int(width + 100),
                getattr(Alignment, "ALIGN_NORMAL"),
                1.0,
                0.0,
                False,
            )
            set_private_field(cell, "timeTextWidth", int(width))
            set_private_field(cell, "timeWidth", int(width))
            set_private_field(cell, "timeLayout", layout)
            return True
        except Exception:
            return False

    @staticmethod
    def _get_time_draw_position(cell):
        if cell is None:
            return None
        try:
            x = float(get_private_field(cell, "drawTimeX"))
            y = float(get_private_field(cell, "drawTimeY"))
            if x == 0 and y == 0:
                return None
            return x, y
        except Exception:
            return None

    @staticmethod
    def _get_theme_time_paint():
        try:
            Theme = jclass("org.telegram.ui.ActionBar.Theme")
            return getattr(Theme, "chat_timePaint")
        except Exception:
            return None

    def _draw_decoded_marker_on_canvas(self, cell, canvas):
        if cell is None or canvas is None:
            return

        message_object = None
        try:
            message_object = get_private_field(cell, "currentMessageObject")
        except Exception:
            return
        message = self._extract_message_from_update(message_object)
        if message is None:
            return

        dialog_id = self._extract_dialog_id_from_message(message)
        message_id = self._extract_message_id(message)
        if not dialog_id or message_id <= 0:
            return
        if (int(dialog_id), int(message_id)) not in self._decoded_message_texts:
            return

        position = self._get_time_draw_position(cell)
        if position is None:
            return
        draw_x, draw_y = position

        paint = self._get_theme_time_paint()
        if paint is None:
            return

        marker = "🔓"
        try:
            layout = get_private_field(cell, "timeLayout")
            line_width = 0.0
            if layout is not None and hasattr(layout, "getLineWidth"):
                line_width = float(layout.getLineWidth(0))
            marker_x = draw_x + line_width + 4.0
            marker_y = (
                draw_y + float(layout.getHeight()) - 2.0
                if layout is not None
                else draw_y
            )
            canvas.drawText(marker, marker_x, marker_y, paint)
        except Exception:
            pass

    @staticmethod
    def _refresh_cell_layout(cell):
        if cell is None:
            return
        try:
            if hasattr(cell, "requestLayout"):
                cell.requestLayout()
            if hasattr(cell, "invalidate"):
                cell.invalidate()
        except Exception:
            pass

    def _apply_decoded_text_to_entities(self, message_object, message, text):
        changed = False

        if self._set_message_text(message, text):
            changed = True
        if self._set_object_text_fields(message, text):
            changed = True

        if message_object is not None and message_object is not message:
            if self._set_object_text_fields(message_object, text):
                changed = True

        self._mark_message_decoded(message)
        if message_object is not None and message_object is not message:
            self._mark_message_decoded(message_object)

        try:
            if message is not None and hasattr(message, "resetLayout"):
                message.resetLayout()
        except Exception:
            pass
        try:
            if message_object is not None and hasattr(message_object, "resetLayout"):
                message_object.resetLayout()
        except Exception:
            pass

        return changed

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
    def _get_context_value(context, key):
        if context is None:
            return None
        try:
            if hasattr(context, "get"):
                return context.get(key)
        except Exception:
            pass
        try:
            return getattr(context, key)
        except Exception:
            return get_private_field(context, key)

    def _extract_dialog_id_from_context(self, context):
        for key in ("dialogId", "dialog_id", "chatId", "chat_id"):
            value = self._get_context_value(context, key)
            try:
                value_int = int(value)
                if value_int:
                    return value_int
            except Exception:
                pass

        for key in ("peer", "peer_id", "peerId", "dialog", "dialog_id"):
            peer = self._get_context_value(context, key)
            dialog_id = self._extract_dialog_id_from_peer(peer)
            if dialog_id:
                return dialog_id

        message = self._get_context_value(context, "message")
        dialog_id = self._extract_dialog_id_from_message(message)
        if dialog_id:
            return dialog_id

        chat = self._get_context_value(context, "chat") or self._get_context_value(
            context, "channel"
        )
        chat_id = self._extract_id_from_object(chat)
        if chat_id:
            return -chat_id

        user = self._get_context_value(context, "user")
        user_id = self._extract_id_from_object(user)
        if user_id:
            return user_id

        return 0

    def _resolve_dialog_id_from_params_meta(self, params_meta):
        if not isinstance(params_meta, dict):
            return 0
        peer = params_meta.get("peer")
        if isinstance(peer, int):
            return peer
        return self._extract_dialog_id_from_peer(peer)

    def _get_enabled_dialog_ids(self):
        raw = self.get_setting("dialog_enabled_ids", "")
        if isinstance(raw, list):
            values = raw
        else:
            values = str(raw).replace(" ", "").split(",") if raw else []
        result = set()
        for value in values:
            try:
                value_int = int(value)
                if value_int:
                    result.add(value_int)
            except Exception:
                continue
        return result

    def _is_dialog_enabled(self, dialog_id):
        if not dialog_id:
            return False
        return dialog_id in self._get_enabled_dialog_ids()

    def _set_dialog_enabled(self, dialog_id, enabled):
        if not dialog_id:
            return
        current = self._get_enabled_dialog_ids()
        if enabled:
            current.add(dialog_id)
        else:
            current.discard(dialog_id)
        serialized = ",".join(str(value) for value in sorted(current))
        self.set_setting("dialog_enabled_ids", serialized)

    def _register_dialog_toggle_menu(self):
        if self._dialog_menu_item_id:
            return
        item = MenuItemData(
            menu_type=MenuItemType.CHAT_ACTION_MENU,
            text="Alter Ego",
            subtext="Включить/выключить для диалога",
            icon="ai_chat_solar",
            on_click=self._toggle_dialog_menu,
            item_id="alterego_dialog_toggle",
            priority=10,
        )
        self._dialog_menu_item_id = self.add_menu_item(item)

    def _clear_decoded_for_dialog(self, dialog_id):
        if not dialog_id:
            return
        did = int(dialog_id)
        for d in (self._decoded_message_texts, self._decode_denied, self._decode_recent_applied, self._decode_cache):
            keys = [k for k in d if (k[0] if isinstance(k, tuple) else None) == did]
            for k in keys:
                d.pop(k, None)

    def _toggle_dialog_menu(self, context):
        dialog_id = self._extract_dialog_id_from_context(context)
        if not dialog_id:
            context_keys = "n/a"
            try:
                if isinstance(context, dict):
                    context_keys = list(context.keys())
            except Exception:
                pass
            class_name = ""
            try:
                class_name = str(context.getClass().getName())
            except Exception:
                class_name = type(context).__name__ if context is not None else "None"
            self.log(
                f"[AlterEgo] dialog toggle: unknown context keys={context_keys}, class={class_name}"
            )
            self._show_bulletin("Не удалось определить диалог", "error")
            return
        enabled = not self._is_dialog_enabled(dialog_id)
        self._set_dialog_enabled(dialog_id, enabled)
        if enabled:
            self._show_bulletin("Alter Ego включен для диалога", "success")
        else:
            self._show_bulletin("Alter Ego выключен для диалога", "info")
            self._clear_decoded_for_dialog(dialog_id)

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
        pending_dialog_ids = set()
        for pending_item in self._pending_stored_messages:
            pending_dialog = int(pending_item.get("dialogId") or 0)
            if pending_dialog:
                pending_dialog_ids.add(pending_dialog)
        if dialog_id and pending_dialog_ids and dialog_id not in pending_dialog_ids:
            return False
        self.log(
            f"[AlterEgo][store/msg] incoming: dialog={dialog_id}, messageId={message_id}, text='{text[:80]}', pending={len(self._pending_stored_messages)}"
        )

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
            pending_snapshot = []
            for item in self._pending_stored_messages[-3:]:
                item_created = int(item.get("createdAtMs") or 0)
                item_age_ms = now - item_created if item_created else -1
                pending_snapshot.append(
                    {
                        "dialog": int(item.get("dialogId") or 0),
                        "randomId": int(item.get("clientRandomId") or 0),
                        "pendingReqId": int(item.get("pendingRequestId") or 0),
                        "ageMs": item_age_ms,
                        "cover": str(item.get("coverText") or "")[:40],
                    }
                )
            self.log(
                f"[AlterEgo] no pending match: text='{text[:80]}', dialog={dialog_id}, pending={len(self._pending_stored_messages)}, snapshot={pending_snapshot}"
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

        created_at = self._to_utc_iso(self._extract_message_date_ms(message))
        run_on_queue(
            lambda: self._store_message_sync(
                normalized,
                resolved_dialog,
                message_id,
                original_text,
                cover_text,
                created_at,
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
        is_outgoing = bool(fields.get("out"))
        sender_telegram_id = int(fields.get("senderTelegramId") or 0)
        pending_dialog_ids = set()
        for pending_item in self._pending_stored_messages:
            pending_dialog = int(pending_item.get("dialogId") or 0)
            if pending_dialog:
                pending_dialog_ids.add(pending_dialog)
        if dialog_id and pending_dialog_ids and dialog_id not in pending_dialog_ids:
            return False
        self.log(
            f"[AlterEgo][store/update] incoming: dialog={dialog_id}, messageId={message_id}, randomId={random_id}, pendingReqId={pending_req_id}, out={is_outgoing}, sender={sender_telegram_id}, currentUser={current_user_id}, text='{str(text)[:80]}', pending={len(self._pending_stored_messages)}"
        )

        # Ignore updates for dialogs where plugin is disabled.
        if dialog_id and not self._is_dialog_enabled(dialog_id):
            return False

        # For storing sent messages we trust only outgoing updates (or updates
        # with sender equal to current account user). This prevents binding to
        # foreign updates from other dialogs/accounts.
        if current_user_id and sender_telegram_id and sender_telegram_id != current_user_id:
            if not is_outgoing:
                return False

        # Some short-sent update variants do not expose dialog/sender/out flags.
        # Trust such updates only shortly after a successful send request.
        if dialog_id == 0 and not is_outgoing and sender_telegram_id == 0:
            recent_send_at = int(self._last_successful_send_at_ms.get(int(account)) or 0)
            if not recent_send_at or self._now_ms() - recent_send_at > 30000:
                return False
            if not isinstance(text, str) or not text.strip():
                return False

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

        if matched_index < 0 and len(self._pending_stored_messages) == 1:
            if is_outgoing and message_id > 0:
                single = self._pending_stored_messages[0]
                created_at = int(single.get("createdAtMs") or 0)
                item_dialog = int(single.get("dialogId") or 0)
                if created_at and now - created_at <= 15000:
                    if dialog_id == 0 or (
                        item_dialog and dialog_id == item_dialog
                    ):
                        matched_index = 0
                        self.log(
                            "[AlterEgo] pending match by outgoing-rpc (single pending, meta incomplete)"
                        )

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
                if not dialog_id:
                    # If update doesn't provide dialog id, require exact text match
                    # to avoid binding to unrelated outgoing updates.
                    item_cover = item.get("coverText") or ""
                    if (
                        not isinstance(text, str)
                        or not text.strip()
                        or not isinstance(item_cover, str)
                        or item_cover.strip() != text.strip()
                    ):
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

        # Do not use ambiguous last-resort fallback here:
        # it can bind pending entry to an unrelated outgoing update and produce
        # wrong telegramMessageId (e.g. tiny local IDs like 76).

        if matched_index < 0:
            pending_snapshot = []
            for item in self._pending_stored_messages[-3:]:
                item_created = int(item.get("createdAtMs") or 0)
                item_age_ms = now - item_created if item_created else -1
                pending_snapshot.append(
                    {
                        "account": int(item.get("account") or 0),
                        "dialog": int(item.get("dialogId") or 0),
                        "randomId": int(item.get("clientRandomId") or 0),
                        "pendingReqId": int(item.get("pendingRequestId") or 0),
                        "ageMs": item_age_ms,
                        "cover": str(item.get("coverText") or "")[:40],
                    }
                )
            self.log(
                f"[AlterEgo] no raw-update pending match: msgId={message_id}, dialog={dialog_id}, randomId={random_id}, pendingReqId={pending_req_id}, pending={len(self._pending_stored_messages)}, snapshot={pending_snapshot}"
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

        created_at = fields.get("receivedAt") or ""
        run_on_queue(
            lambda: self._store_message_sync(
                normalized,
                resolved_dialog,
                message_id,
                original_text,
                cover_text,
                created_at,
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
        resolved_dialog_id=0,
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

                def _send_on_ui():
                    try:
                        helper.sendMessage(send_params)
                        self.log(
                            f"[AlterEgo] resend done with keys={sorted(list(resend_param_keys))}"
                        )

                        dialog_id = int(resolved_dialog_id) if resolved_dialog_id else 0
                        self._pending_stored_messages.append(
                            {
                                "account": self._extract_current_user_id(account),
                                "dialogId": int(dialog_id)
                                if isinstance(dialog_id, int)
                                else 0,
                                "originalText": source_text,
                                "coverText": masked_text,
                                "createdAtMs": self._now_ms(),
                                "pendingRequestId": int(
                                    params_meta.get("pendingRequestId") or 0
                                ),
                                "clientRandomId": int(
                                    params_meta.get("clientRandomId") or 0
                                ),
                            }
                        )
                        self._last_successful_send_at_ms[int(account)] = self._now_ms()
                        self.log(
                            f"[AlterEgo] pending added: dialog={int(dialog_id) if isinstance(dialog_id, int) else 0}, pendingReqId={int(params_meta.get('pendingRequestId') or 0)}, randomId={int(params_meta.get('clientRandomId') or 0)}, cover='{masked_text[:80]}'"
                        )
                        if len(self._pending_stored_messages) > 120:
                            self._pending_stored_messages = (
                                self._pending_stored_messages[-120:]
                            )

                        self._show_bulletin("Отправлено с маской", "success")
                    except Exception as error:
                        self.log(f"[AlterEgo] resend failed: {error}")
                        self.log(
                            f"[AlterEgo] resend traceback: {traceback.format_exc()}"
                        )
                        self._show_bulletin("Ошибка повторной отправки", "error")

                run_on_ui_thread(_send_on_ui)
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
        self,
        api_base_url,
        dialog_id,
        telegram_message_id,
        source_text,
        cover_text,
        created_at="",
    ):
        payload = {
            "dialogId": int(dialog_id) if isinstance(dialog_id, int) else 0,
            "telegramMessageId": int(telegram_message_id)
            if isinstance(telegram_message_id, int)
            else 0,
            "originalText": source_text,
            "coverText": cover_text,
            "createdAt": created_at
            if isinstance(created_at, str) and created_at.strip()
            else datetime.now(timezone.utc).isoformat(),
        }
        self.log(
            f"[AlterEgo][store/api] POST /api/Messages payload: dialogId={payload['dialogId']}, telegramMessageId={payload['telegramMessageId']}, createdAt={payload['createdAt']}, originalLen={len(str(payload.get('originalText') or ''))}, coverLen={len(str(payload.get('coverText') or ''))}"
        )

        last_error = ""
        for attempt in range(3):
            headers = self._auth_headers(api_base_url)
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
                f"[AlterEgo] store message status: {response.status_code}, dialog_id={payload['dialogId']}, message_id={payload['telegramMessageId']}, attempt={attempt + 1}"
            )
            if response.status_code == 401:
                if attempt < 2:
                    self._auth_access_token = ""
                    if self._login_sync(api_base_url):
                        continue
                server_message = self._extract_server_message(
                    response, "Store message failed"
                )
                self.log(
                    f"[AlterEgo][store/api] client error: status=401, message={server_message}"
                )
                return
            if response.status_code >= 400 and response.status_code < 500:
                server_message = self._extract_server_message(
                    response, "Store message failed"
                )
                self.log(
                    f"[AlterEgo][store/api] client error: status={response.status_code}, message={server_message}"
                )
                return
            if response.status_code >= 500:
                last_error = self._extract_server_message(
                    response, "Store message failed"
                )
                if attempt < 2:
                    time.sleep(0.35)
                    continue
            return

    def _decode_by_message_id_sync(
        self,
        api_base_url,
        dialog_id,
        telegram_message_id,
        sender_telegram_id,
        received_at,
    ):
        if int(dialog_id) >= 0:
            return False, ""

        if int(telegram_message_id) <= 0:
            return False, ""

        headers = self._auth_headers(api_base_url)
        try:
            response = requests.get(
                f"{api_base_url}/api/Messages/{int(dialog_id)}/{int(telegram_message_id)}",
                headers=headers,
                timeout=DECODE_TIMEOUT_SEC,
            )
        except Exception:
            return False, ""

        if response.status_code != 200:
            return False, ""

        try:
            payload = response.json() if response.content else {}
        except Exception:
            return False, ""

        if isinstance(payload, dict):
            original_text = payload.get("originalText")
            if isinstance(original_text, str) and original_text.strip():
                return True, original_text.strip()
        return False, ""

    def _decode_private_sync(
        self, api_base_url, dialog_id, sender_telegram_id, cover_text, received_at
    ):
        if not dialog_id:
            self.log("[AlterEgo] decode_private skip: no dialog_id")
            return False, ""
        if int(sender_telegram_id) <= 0:
            self.log(f"[AlterEgo] decode_private skip: sender={sender_telegram_id}")
            return False, ""
        if not isinstance(cover_text, str) or not cover_text.strip():
            self.log("[AlterEgo] decode_private skip: empty cover_text")
            return False, ""

        headers = self._auth_headers(api_base_url)
        payload = {
            "dialogId": int(dialog_id),
            "coverText": cover_text,
            "receivedAt": received_at if isinstance(received_at, str) and received_at.strip() else datetime.now(timezone.utc).isoformat(),
        }

        self.log(f"[AlterEgo] decode_private POST: dialog={int(dialog_id)}, sender={int(sender_telegram_id)}, receivedAt={received_at!r}")
        try:
            response = requests.post(
                f"{api_base_url}/api/Messages/decode",
                json=payload,
                headers=headers,
                timeout=DECODE_TIMEOUT_SEC,
            )
        except Exception as e:
            self.log(f"[AlterEgo] decode_private request failed: {e}")
            return False, ""

        self.log(f"[AlterEgo] decode_private response: status={response.status_code}, body={response.text[:200]!r}")
        if response.status_code != 200:
            return False, ""

        try:
            data = response.json() if response.content else {}
        except Exception:
            return False, ""

        if isinstance(data, dict):
            original_text = data.get("originalText")
            if isinstance(original_text, str) and original_text.strip():
                return True, original_text.strip()
        return False, ""

    def _decode_message_text_sync(
        self,
        api_base_url,
        dialog_id,
        telegram_message_id,
        sender_telegram_id,
        cover_text,
        received_at,
    ):
        self.log(
            f"[AlterEgo] decode try dialog={int(dialog_id)}, messageId={int(telegram_message_id)}, sender={int(sender_telegram_id)}"
        )
        ok, original = self._decode_by_message_id_sync(
            api_base_url,
            dialog_id,
            telegram_message_id,
            sender_telegram_id,
            received_at,
        )
        if ok:
            return True, original

        return self._decode_private_sync(
            api_base_url,
            dialog_id,
            sender_telegram_id,
            cover_text,
            received_at,
        )

    def _remember_decode_cache(self, key, value):
        if not isinstance(value, str) or not value.strip():
            return
        self._decode_cache[key] = value
        if len(self._decode_cache) > 700:
            try:
                oldest_key = next(iter(self._decode_cache.keys()))
                self._decode_cache.pop(oldest_key, None)
            except Exception:
                pass

    @staticmethod
    def _extract_account_from_cell(cell):
        if cell is None:
            return 0
        for attr in ("currentAccount", "account"):
            try:
                value = getattr(cell, attr)
            except Exception:
                value = get_private_field(cell, attr)
            try:
                return int(value)
            except Exception:
                continue
        return 0

    @staticmethod
    def _extract_dialog_id_from_cell(cell):
        if cell is None:
            return 0
        for attr in ("currentDialogId", "dialogId", "dialog_id"):
            try:
                value = getattr(cell, attr)
            except Exception:
                value = get_private_field(cell, attr)
            try:
                value_int = int(value)
                if value_int:
                    return value_int
            except Exception:
                continue
        return 0

    def _cache_decoded_message(self, dialog_id, message_id, original_text):
        key = (int(dialog_id), int(message_id))
        self._decoded_message_texts[key] = original_text
        if len(self._decoded_message_texts) > 1200:
            try:
                oldest_key = next(iter(self._decoded_message_texts.keys()))
                self._decoded_message_texts.pop(oldest_key, None)
            except Exception:
                pass

    def _remember_decode_denied(self, dialog_id, message_id):
        self._decode_denied[(int(dialog_id), int(message_id))] = self._now_ms()
        if len(self._decode_denied) > 1200:
            try:
                oldest_key = next(iter(self._decode_denied.keys()))
                self._decode_denied.pop(oldest_key, None)
            except Exception:
                pass

    def _is_decode_denied_recently(self, dialog_id, message_id):
        key = (int(dialog_id), int(message_id))
        last = int(self._decode_denied.get(key) or 0)
        return bool(last and self._now_ms() - last < 180000)

    def _mark_decode_recently_applied(self, dialog_id, message_id):
        key = (int(dialog_id), int(message_id))
        self._decode_recent_applied[key] = self._now_ms()
        if len(self._decode_recent_applied) > 1500:
            try:
                oldest_key = next(iter(self._decode_recent_applied.keys()))
                self._decode_recent_applied.pop(oldest_key, None)
            except Exception:
                pass

    def _is_decode_recently_applied(self, dialog_id, message_id):
        key = (int(dialog_id), int(message_id))
        last = int(self._decode_recent_applied.get(key) or 0)
        return bool(last and self._now_ms() - last < 900)

    def _apply_decoded_time_icon(self, cell):
        message_object = None
        try:
            message_object = get_private_field(cell, "currentMessageObject")
        except Exception:
            return
        if message_object is None:
            return

        message = self._extract_message_from_update(message_object)
        dialog_id = self._extract_dialog_id_from_message(message)
        message_id = self._extract_message_id(message)
        if not dialog_id or message_id <= 0:
            current_time = self._get_message_time_text(cell)
            if isinstance(current_time, str) and current_time.endswith(
                DECODE_TIME_MARKER
            ):
                self._set_message_time_text(
                    cell, current_time[: -len(DECODE_TIME_MARKER)]
                )
            return

        if not self._is_dialog_enabled(dialog_id) or (int(dialog_id), int(message_id)) not in self._decoded_message_texts:
            current_time = self._get_message_time_text(cell)
            if isinstance(current_time, str) and current_time.endswith(
                DECODE_TIME_MARKER
            ):
                self._set_message_time_text(
                    cell, current_time[: -len(DECODE_TIME_MARKER)]
                )
            return

        current_time = self._get_message_time_text(cell)
        if not isinstance(current_time, str) or not current_time:
            return
        if current_time.endswith(DECODE_TIME_MARKER):
            return
        updated = f"{current_time}{DECODE_TIME_MARKER}"
        self._set_message_time_text(cell, updated)
        self._rebuild_time_layout_for_cell(cell, updated)

    def _decode_message_object_async(self, message_object, account, cell=None):
        message = self._extract_message_from_update(message_object)
        if message is None:
            return

        dialog_id = self._extract_dialog_id_from_message(message)
        if not dialog_id and cell is not None:
            dialog_id = self._extract_dialog_id_from_cell(cell)
        if (
            not dialog_id
            and message_object is not None
            and message_object is not message
        ):
            dialog_id = self._extract_dialog_id_from_message(message_object)
        if not dialog_id or not self._is_dialog_enabled(dialog_id):
            return

        sender_telegram_id = self._extract_sender_user_id(message)
        current_user_id = self._extract_current_user_id(account)
        if current_user_id and (
            not sender_telegram_id or sender_telegram_id <= 0
        ) and self._is_outgoing_message(message, account):
            sender_telegram_id = current_user_id

        cover_text = self._extract_message_text(message)
        if not isinstance(cover_text, str) or not cover_text.strip():
            return

        message_id = self._extract_message_id(message)
        if (
            message_id <= 0
            and message_object is not None
            and message_object is not message
        ):
            message_id = self._extract_message_id(message_object)
        if message_id <= 0:
            return

        if self._is_decode_recently_applied(dialog_id, message_id):
            if cell is not None:
                self._apply_decoded_time_icon(cell)
            return

        decoded_key = (int(dialog_id), int(message_id))
        cached_text = self._decoded_message_texts.get(decoded_key)
        if isinstance(cached_text, str) and cached_text.strip():
            current_text = self._extract_message_text(message)
            already_decoded = self._is_message_marked_decoded(message)
            needs_apply = not (
                isinstance(current_text, str)
                and current_text.strip() == cached_text.strip()
                and already_decoded
            )
            if needs_apply and self._apply_decoded_text_to_entities(
                message_object, message, cached_text
            ):
                self._mark_decode_recently_applied(dialog_id, message_id)
                if cell is not None:
                    self._apply_decoded_time_icon(cell)
                    self._refresh_cell_layout(cell)
            elif cell is not None:
                self._apply_decoded_time_icon(cell)
            return

        decode_key = (
            int(dialog_id),
            int(message_id),
            int(sender_telegram_id),
            cover_text.strip(),
        )
        if decode_key in self._decode_in_flight:
            return

        if self._is_decode_denied_recently(dialog_id, message_id):
            return

        cached = self._decode_cache.get(decode_key)
        if isinstance(cached, str) and cached.strip():
            current_text = self._extract_message_text(message)
            already_decoded = self._is_message_marked_decoded(message)
            needs_apply = not (
                isinstance(current_text, str)
                and current_text.strip() == cached.strip()
                and already_decoded
            )
            if needs_apply and self._apply_decoded_text_to_entities(
                message_object, message, cached
            ):
                self._cache_decoded_message(dialog_id, message_id, cached)
                self._mark_decode_recently_applied(dialog_id, message_id)
                if cell is not None:
                    self._apply_decoded_time_icon(cell)
                    self._refresh_cell_layout(cell)
            elif cell is not None:
                self._apply_decoded_time_icon(cell)
            return

        api_url = self.get_setting("api_url", "")
        if not isinstance(api_url, str) or not self._is_url(api_url):
            return

        received_at = self._to_utc_iso(self._extract_message_date_ms(message))
        self._decode_in_flight.add(decode_key)

        def _worker():
            try:
                success, original_text = self._decode_message_text_sync(
                    api_url.rstrip("/"),
                    dialog_id,
                    message_id,
                    sender_telegram_id,
                    cover_text,
                    received_at,
                )
                if (
                    not success
                    or not isinstance(original_text, str)
                    or not original_text.strip()
                ):
                    self.log(
                        f"[AlterEgo] decode miss dialog={dialog_id}, messageId={message_id}, sender={sender_telegram_id}"
                    )
                    self._remember_decode_denied(dialog_id, message_id)
                    return

                self._remember_decode_cache(decode_key, original_text)
                self._cache_decoded_message(dialog_id, message_id, original_text)
                self.log(
                    f"[AlterEgo] decoded dialog={dialog_id}, messageId={message_id}, sender={sender_telegram_id}"
                )

                def _apply():
                    try:
                        if self._apply_decoded_text_to_entities(
                            message_object, message, original_text
                        ):
                            self._mark_decode_recently_applied(dialog_id, message_id)
                            if cell is not None:
                                self._apply_decoded_time_icon(cell)
                                self._refresh_cell_layout(cell)
                    except Exception:
                        pass

                run_on_ui_thread(_apply)
            finally:
                self._decode_in_flight.discard(decode_key)

        run_on_queue(_worker)

    def _try_decode_message_from_update(self, update, account):
        message = self._extract_message_from_update(update)
        if message is None:
            return False
        self._decode_message_object_async(message, account)
        return False

    @staticmethod
    def _java_collection_to_list(seq):
        if seq is None:
            return []
        try:
            size = int(seq.size())
            return [seq.get(i) for i in range(size)]
        except Exception:
            try:
                return list(seq)
            except Exception:
                return []

    @staticmethod
    def _iter_updates_like_items(obj):
        if obj is None:
            return []
        items = []
        updates = None
        try:
            updates = getattr(obj, "updates", None)
        except Exception:
            updates = None
        if updates is None:
            try:
                updates = get_private_field(obj, "updates")
            except Exception:
                updates = None
        if updates is not None:
            items.extend(AlterEgo._java_collection_to_list(updates))
        for attr in ("messages", "new_messages", "sent_messages"):
            child = None
            try:
                child = getattr(obj, attr, None)
            except Exception:
                pass
            if child is None:
                try:
                    child = get_private_field(obj, attr)
                except Exception:
                    child = None
            if child is not None:
                items.extend(AlterEgo._java_collection_to_list(child))
        if items:
            return items
        return [obj]

    def post_request_hook(
        self, request_name: str, account: int, response: Any, error: Any
    ) -> HookResult:
        try:
            if not self._pending_stored_messages:
                return HookResult(strategy=HookStrategy.DEFAULT, response=response, error=error)
            if not isinstance(request_name, str):
                return HookResult(strategy=HookStrategy.DEFAULT, response=response, error=error)

            request_name_lower = request_name.lower()
            if (
                "sendmessage" not in request_name_lower
                and "sendmedia" not in request_name_lower
                and "sendmultimedia" not in request_name_lower
            ):
                return HookResult(strategy=HookStrategy.DEFAULT, response=response, error=error)

            self.log(
                f"[AlterEgo] post_request_hook incoming: request={request_name}, hasError={error is not None}, pending={len(self._pending_stored_messages)}"
            )
            if error is None:
                self._last_successful_send_at_ms[int(account)] = self._now_ms()

            stored = False
            for item in self._iter_updates_like_items(response):
                if self._try_store_pending_from_update(item, account):
                    self.log(
                        f"[AlterEgo] post_request_hook stored pending from {request_name}"
                    )
                    stored = True
                    break
                message = self._extract_message_from_update(item)
                if self._try_store_pending_from_message(message):
                    self.log(
                        f"[AlterEgo] post_request_hook stored pending(message) from {request_name}"
                    )
                    stored = True
                    break
            if not stored and self._pending_stored_messages:
                if self._try_store_pending_from_update(response, account):
                    self.log(
                        f"[AlterEgo] post_request_hook stored pending from {request_name} (raw response)"
                    )
                elif self._try_store_pending_from_message(
                    self._extract_message_from_update(response)
                ):
                    self.log(
                        f"[AlterEgo] post_request_hook stored pending(message) from {request_name} (raw response)"
                    )
        except Exception as hook_error:
            self.log(f"[AlterEgo] post_request_hook error: {hook_error}")
        return HookResult(strategy=HookStrategy.DEFAULT, response=response, error=error)

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
            resolved_dialog_id = self._resolve_dialog_id_from_params_meta(params_meta)
            if resolved_dialog_id and not self._is_dialog_enabled(resolved_dialog_id):
                self.log("[AlterEgo] send pipeline: dialog disabled, skip")
                return HookResult(strategy=HookStrategy.DEFAULT, params=params)
            self._show_bulletin("Маскирую сообщение...", "info")

            def _worker():
                success, masked_text, error_text = self._process_mask_pipeline(
                    resolved_dialog_id, original_text, api_url
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
                    int(resolved_dialog_id) if isinstance(resolved_dialog_id, int) else 0,
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
                    resolved_dialog_id,
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
            self._try_decode_message_from_update(update, account)
            if not self._pending_stored_messages:
                return HookResult(strategy=HookStrategy.DEFAULT, update=update)
            if self._try_store_pending_from_update(update, account):
                self.log("[AlterEgo] on_update_hook: stored by raw update")
                return HookResult(strategy=HookStrategy.DEFAULT, update=update)
            message = self._extract_message_from_update(update)
            if self._try_store_pending_from_message(message):
                self.log("[AlterEgo] on_update_hook: stored by single update")
            self._decode_message_object_async(update, account)
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
                if self._try_store_pending_from_update(updates, account):
                    self.log(
                        f"[AlterEgo] on_updates_hook: stored from container-object {container_name}"
                    )
                    return HookResult(strategy=HookStrategy.DEFAULT, updates=updates)
                single_message = self._extract_message_from_update(updates)
                if self._try_store_pending_from_message(single_message):
                    self.log(
                        f"[AlterEgo] on_updates_hook: stored from container-message {container_name}"
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
                self._try_decode_message_from_update(update, account)
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
                self._decode_message_object_async(update, account)
            else:
                if processed == 0:
                    # Some update containers do not expose iterable `updates`, but
                    # still keep the useful fields on the container itself.
                    if self._try_store_pending_from_update(updates, account):
                        self.log(
                            f"[AlterEgo] on_updates_hook: stored from empty-container-object {container_name}"
                        )
                        return HookResult(strategy=HookStrategy.DEFAULT, updates=updates)
                    single_message = self._extract_message_from_update(updates)
                    if self._try_store_pending_from_message(single_message):
                        self.log(
                            f"[AlterEgo] on_updates_hook: stored from empty-container-message {container_name}"
                        )
                        return HookResult(strategy=HookStrategy.DEFAULT, updates=updates)

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

    def _extract_dialog_id_from_chat_activity(self, chat_activity):
        if chat_activity is None:
            return 0
        dialog_id = 0
        for attr in ("dialogId", "dialog_id"):
            try:
                value = getattr(chat_activity, attr)
            except Exception:
                value = get_private_field(chat_activity, attr)
            try:
                dialog_id = int(value)
                if dialog_id:
                    return dialog_id
            except Exception:
                continue

        try:
            chat = get_private_field(chat_activity, "currentChat")
        except Exception:
            chat = None
        if chat is not None:
            chat_id = self._extract_id_from_object(chat)
            if chat_id:
                return -chat_id

        try:
            user = get_private_field(chat_activity, "currentUser")
        except Exception:
            user = None
        if user is not None:
            user_id = self._extract_id_from_object(user)
            if user_id:
                return user_id

        return 0

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

        dialog_id = self._extract_dialog_id_from_chat_activity(chat_activity)
        if dialog_id and not self._is_dialog_enabled(dialog_id):
            self._restore_native_floating_date(chat_activity)
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
