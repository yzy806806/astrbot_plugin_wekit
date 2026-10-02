# -*- coding: utf-8 -*-

"""AstrBot 插件入口：加载 WeKit 平台适配器。

AstrBot 通过 ``main.py`` 发现并加载插件，插件必须至少有一个 ``Star`` 子类
（``Star.__init_subclass__`` 会自动完成注册）。本类本身不提供命令，只作为
承载者触发 ``wekit_adapter`` 的 import，让 ``@register_platform_adapter``
装饰器把 WeKit 平台注册进 platform_cls_map。
"""

from astrbot.api.star import Context, Star


class WeKitPlugin(Star):
    """WeKit 微信平台适配器（无命令，仅承载平台注册）。"""

    def __init__(self, context: Context) -> None:
        super().__init__(context)
        # import 触发 @register_platform_adapter 平台注册
        from .wekit_adapter import WeKitAdapter  # noqa: F401
        self.logger.info("WeKit 平台适配器已注册（轮询 REST API 模式）")


__all__ = ["WeKitPlugin"]
