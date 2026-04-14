# region Metadata
__name__ = "AlterEgo"
__description__ = "Шифрование сообщений с использование семантической трансформации"
__version__ = "0.0.1"
__id__ = "alter_ego"
__author__ = "@renamq"
__icon__ = "exteraPlugins/1"
# endregion

import time
import traceback
from datetime import datetime, timezone
from typing import Any, List
from urllib.parse import urlparse

import requests
from base_plugin import BasePlugin, MethodHook
from client_utils import run_on_queue
from ui.bulletin import BulletinHelper
from ui.settings import Header, Text, Divider, Input
from hook_utils import find_class, get_private_field
from android_utils import run_on_ui_thread, OnClickListener

ACTION_DOWN = 0
ACTION_UP = 1
ACTION_CANCEL = 3
LONG_PRESS_DELAY_MS = 430
STATUS_RETURN_DELAY_MS = 420
STATUS_REFRESH_INTERVAL_MS = 5000


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
