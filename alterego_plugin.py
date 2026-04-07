# region Metadata
__name__ = "AlterEgo"
__description__ = "Шифрование сообщений с использование семантической трансформации"
__version__ = "0.0.1"
__id__ = "alter_ego"
__author__ = "@renamq"
__icon__ = "exteraPlugins/1"
# endregion

from typing import Any, List
from base_plugin import BasePlugin, MethodHook
from ui.settings import Header, Text, Divider, Input
from hook_utils import find_class, get_private_field
from android_utils import run_on_ui_thread


class ChatCreateViewHook(MethodHook):
    def __init__(self, plugin):
        self.plugin = plugin

    def after_hooked_method(self, param):
        try:
            self.plugin._apply_floating_date_status(param.thisObject)
        except Exception as error:
            self.plugin.log(f"[AlterEgo] Failed to apply floating date status on createView: {error}")


class ChatShowFloatingDateHook(MethodHook):
    def __init__(self, plugin):
        self.plugin = plugin

    def after_hooked_method(self, param):
        try:
            is_scroll = bool(param.args[0]) if param.args and len(param.args) > 0 else False
            if is_scroll:
                self.plugin._set_native_date_mode(param.thisObject, True)
                self.plugin._mark_scroll_activity(param.thisObject)
                self.plugin._restore_native_floating_date(param.thisObject)
                self.plugin._schedule_auto_status_return(param.thisObject)
            else:
                self.plugin._set_native_date_mode(param.thisObject, False)
                self.plugin._apply_floating_date_status(param.thisObject)
        except Exception as error:
            self.plugin.log(f"[AlterEgo] Failed to apply floating date status on show: {error}")


class ChatHideFloatingDateHook(MethodHook):
    def __init__(self, plugin):
        self.plugin = plugin

    def before_hooked_method(self, param):
        try:
            if self.plugin._is_native_date_mode(param.thisObject):
                self.plugin._restore_native_floating_date(param.thisObject)
                param.setResult(None)
                return
            else:
                self.plugin._apply_floating_date_status(param.thisObject)
                param.setResult(None)
        except Exception as error:
            self.plugin.log(f"[AlterEgo] Failed to keep floating date visible: {error}")


# endregion

class AlterEgo(BasePlugin):
    def __init__(self):
        super().__init__()
        self._chat_create_view_unhooks = []
        self._chat_show_floating_date_unhooks = []
        self._chat_hide_floating_date_unhooks = []
        self._native_date_mode = {}
        self._last_scroll_activity_ms = {}

    def on_plugin_load(self):
        try:
            chat_activity_class = find_class("org.telegram.ui.ChatActivity")
            if not chat_activity_class:
                self.log("[AlterEgo] ChatActivity class not found")
                return

            self._chat_create_view_unhooks = self.hook_all_methods(
                chat_activity_class,
                "createView",
                ChatCreateViewHook(self),
                priority=80,
            ) or []

            self._chat_show_floating_date_unhooks = self.hook_all_methods(
                chat_activity_class,
                "showFloatingDateView",
                ChatShowFloatingDateHook(self),
                priority=90,
            ) or []

            self._chat_hide_floating_date_unhooks = self.hook_all_methods(
                chat_activity_class,
                "hideFloatingDateView",
                ChatHideFloatingDateHook(self),
                priority=80,
            ) or []

            self.log(
                f"[AlterEgo] Hooks active: createView={len(self._chat_create_view_unhooks)}, "
                f"showFloatingDateView={len(self._chat_show_floating_date_unhooks)}, "
                f"hideFloatingDateView={len(self._chat_hide_floating_date_unhooks)}"
            )
        except Exception as error:
            self.log(f"[AlterEgo] Hook setup failed: {error}")

    def _status(self):
        api_key = self.get_setting("api_key", "")
        if isinstance(api_key, str) and api_key.strip():
            return "Connected"
        return "API Unavailable"

    def _chat_key(self, chat_activity):
        return str(chat_activity)

    def _now_ms(self):
        import time
        return int(time.time() * 1000)

    def _set_native_date_mode(self, chat_activity, value):
        if chat_activity is None:
            return
        self._native_date_mode[self._chat_key(chat_activity)] = bool(value)

    def _is_native_date_mode(self, chat_activity):
        if chat_activity is None:
            return False
        return bool(self._native_date_mode.get(self._chat_key(chat_activity), False))

    def _mark_scroll_activity(self, chat_activity):
        if chat_activity is None:
            return
        key = self._chat_key(chat_activity)
        self._last_scroll_activity_ms[key] = self._now_ms()

    def _apply_floating_date_status(self, chat_activity):
        if chat_activity is None:
            return

        floating_date_view = get_private_field(chat_activity, "floatingDateView")
        if floating_date_view is None:
            return

        status_text = self._status()
        final_text = f"Alter Ego - {status_text}"

        if hasattr(floating_date_view, "setCustomText"):
            floating_date_view.setCustomText(final_text)

        if hasattr(floating_date_view, "setAlpha"):
            floating_date_view.setAlpha(1.0)

        if hasattr(floating_date_view, "setTag"):
            floating_date_view.setTag(1)

        if hasattr(floating_date_view, "setVisibility"):
            floating_date_view.setVisibility(0)

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

        run_on_ui_thread(_restore_if_idle, 420)

    def _restore_native_floating_date(self, chat_activity):
        if chat_activity is None:
            return

        floating_date_view = get_private_field(chat_activity, "floatingDateView")
        if floating_date_view is None:
            return

        if not hasattr(floating_date_view, "getCustomDate") or not hasattr(floating_date_view, "setCustomDate"):
            return

        current_date = floating_date_view.getCustomDate()
        if not current_date:
            return

        floating_date_view.setCustomDate(0, False, False)
        floating_date_view.setCustomDate(current_date, False, False)

        if hasattr(floating_date_view, "setAlpha"):
            floating_date_view.setAlpha(1.0)
        if hasattr(floating_date_view, "setTag"):
            floating_date_view.setTag(1)
        if hasattr(floating_date_view, "setVisibility"):
            floating_date_view.setVisibility(0)

    # region Settings
    def create_settings(self) -> List[Any]:
        return [
            Header(text="Настройки AlterEgo"),
            Divider(text="Статус"),
            Text(text="Online", accent=True, icon="msg_info_solar"),
            Divider(text="Настройки"),
            Input(
                key="api_key",
                text="Ключ API",
                default="",
                icon="ai_chat_solar"
            ),
            Input(
                key="context_manage",
                text="Управление контекстом",
                icon="menu_storage_path_solar"
            )
        ]
    # endregion
