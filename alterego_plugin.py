# region Metadata
__name__ = "AlterEgo"
__description__ = "Шифрование сообщений с использование семантической трансформации"
__version__ = "0.0.1"
__id__ = "alter_ego"
__author__ = "@renamq"
__icon__ = "exteraPlugins/1"
# endregion

from typing import Any, List
from base_plugin import BasePlugin
from ui.settings import Header, Text, Divider, Input


# endregion

class AlterEgo(BasePlugin):
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