"""
Lolicon色图插件 - Lolicon Setu Plugin

基于 api.lolicon.app 的色图获取插件
支持标签搜索、关键词搜索、长宽比筛选等高级功能

功能特性:
- 命令获取图片 (/setu)
- 支持标签AND/OR组合搜索
- 支持关键词模糊搜索
- 支持长宽比筛选
- AI作品排除选项
- 简短命令格式 (#标签、直接输入关键词)
- 内置help命令
- LLM工具调用支持
- 灵活的配置选项

Author: Claude
Version: 2.1.1 (修复版)
"""

from typing import List, Tuple, Type, Any, Optional, Dict
import asyncio
import json
import aiohttp
import time
import re

from src.plugin_system import (
    BasePlugin,
    register_plugin,
    BaseCommand,
    ComponentInfo,
    ConfigField,
)
from src.common.logger import get_logger
from src.config.config import global_config

logger = get_logger("lolicon_setu")


class LoliconAPI:
    """Lolicon API v2封装类"""

    # API端点
    API_ENDPOINT = "https://api.lolicon.app/setu/v2"

    # API限制常量
    MAX_CONCURRENT_REQUESTS = 5  # 最大并发请求数
    MAX_NUM_PER_REQUEST = 20  # 单次请求最大图片数
    MIN_NUM_PER_REQUEST = 1  # 单次请求最小图片数
    MAX_UID_COUNT = 20  # 最大UID数量
    MAX_TAG_GROUPS = 3  # 最大标签组数（AND条件）

    def __init__(self, timeout: int = 30):
        self.timeout = timeout
        self.semaphore = asyncio.Semaphore(self.MAX_CONCURRENT_REQUESTS)

    async def fetch_setu(
        self,
        r18: int = 0,
        num: int = 1,
        uid: Optional[List[int]] = None,
        keyword: Optional[str] = None,
        tag: Optional[List[List[str]]] = None,
        size: Optional[List[str]] = None,
        proxy: str = "i.pixiv.re",
        date_after: Optional[int] = None,
        date_before: Optional[int] = None,
        dsc: bool = False,
        exclude_ai: bool = False,
        aspect_ratio: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        获取色图

        Args:
            r18: 0为非R18，1为R18，2为混合
            num: 返回数量 (1-20)
            uid: 作者UID列表，最多20个
            keyword: 关键字搜索（标题、作者、标签）
            tag: 标签数组，支持AND/OR规则
            size: 图片规格列表
            proxy: 反代服务器
            date_after: 开始时间戳(毫秒)
            date_before: 结束时间戳(毫秒)
            dsc: 禁用缩写转换
            exclude_ai: 排除AI作品
            aspect_ratio: 图片长宽比

        Returns:
            API响应数据
        """
        params = {
            "r18": r18,
            "num": min(max(self.MIN_NUM_PER_REQUEST, num), self.MAX_NUM_PER_REQUEST),
            "proxy": proxy,
            "dsc": dsc,
            "exclude_ai": exclude_ai,  # 修复：使用小写下划线格式
        }

        if uid:
            params["uid"] = uid[:self.MAX_UID_COUNT]
        if keyword:
            params["keyword"] = keyword
        if tag:
            # 将二维数组转换为字符串数组（用|连接）
            params["tag"] = ["|".join(t) if isinstance(t, list) else t for t in tag[:self.MAX_TAG_GROUPS]]
        if size:
            params["size"] = size
        if date_after:
            params["dateAfter"] = date_after
        if date_before:
            params["dateBefore"] = date_before
        if aspect_ratio:
            params["aspectRatio"] = aspect_ratio

        try:
            async with self.semaphore:  # 并发控制
                async with aiohttp.ClientSession() as session:
                    # 添加自定义headers，避免br编码
                    headers = {
                        "Accept-Encoding": "gzip, deflate"  # 移除br，只保留支持的编码
                    }
                    async with session.post(
                        self.API_ENDPOINT, 
                        json=params, 
                        timeout=aiohttp.ClientTimeout(total=self.timeout),
                        headers=headers  # 添加headers参数
                    ) as resp:
                        if resp.status == 200:
                            raw: bytes = await resp.read()
                            result = json.loads(raw.decode("utf-8"))
                            # 检查API是否返回错误
                            if result.get("error"):
                                logger.error(f"API返回错误: {result['error']}")
                                return {"error": result["error"], "data": []}
                            return result
                        else:
                            logger.error(f"API请求失败，状态码: {resp.status}")
                            return {"error": f"HTTP {resp.status}", "data": []}
        except asyncio.TimeoutError:
            logger.error("API请求超时")
            return {"error": "请求超时", "data": []}
        except aiohttp.ClientConnectorError as e:
            logger.error(f"网络连接失败: {str(e)}")
            return {"error": "网络连接失败，请检查网络", "data": []}
        except aiohttp.ClientResponseError as e:
            logger.error(f"API响应错误: {str(e)}")
            return {"error": f"API响应错误: {e.status}", "data": []}
        except aiohttp.ContentTypeError as e:
            logger.error(f"API返回数据格式错误: {str(e)}")
            return {"error": "API返回数据格式错误", "data": []}
        except Exception as e:
            logger.error(f"API请求异常: {str(e)}", exc_info=True)
            return {"error": str(e), "data": []}


class SetuCommand(BaseCommand):
    """色图获取命令"""

    # 类常量
    MAX_COOLDOWN_CACHE_SIZE = 1000  # 冷却缓存最大容量
    COOLDOWN_CLEANUP_THRESHOLD = 1200  # 触发清理的阈值

    # 长宽比验证正则
    ASPECT_RATIO_PATTERN = re.compile(r"^(gt|gte|lt|lte|eq)(\d+(?:\.\d+)?)(gt|gte|lt|lte|eq)?(\d+(?:\.\d+)?)?$")

    command_name = "setu"
    command_description = "从Lolicon API获取色图"
    command_pattern = r"^/setu(?:\s+(?P<args>.+))?$"  # 使用命名捕获组
    command_help = """使用方法:
基础命令:
/setu - 获取1张随机图片
/setu <数量> - 获取指定数量图片 (1-20)

筛选条件:
/setu r18 - R18内容（需配置允许）
/setu noai - 排除AI作品
/setu 横图 - 只要横图(>1)
/setu 竖图 - 只要竖图(<1)
/setu 方图 - 只要方图(=1)

搜索功能:
/setu 原神 - 关键词搜索（直接输入文本）
/setu #萝莉 - 标签搜索（用#号）
/setu #萝莉,少女 - OR搜索（逗号分隔）
/setu #萝莉 #白丝 - AND搜索（多个#标签）
/setu uid:12345 - 按作者UID筛选

组合用法:
/setu 3 #萝莉,少女 - 获取3张萝莉或少女的图
/setu #白丝 #JK 横图 - 白丝且JK的横图
/setu 原神 5 noai - 搜索5张原神，排除AI
/setu 5 #风景 横图 - 获取5张风景横图"""

    command_examples = [
        "/setu - 获取1张随机图片",
        "/setu help - 显示帮助信息",
        "/setu 3 - 获取3张图片",
        "/setu #萝莉 - 搜索萝莉标签",
        "/setu 原神 - 搜索原神关键词",
        "/setu 横图 - 获取横图",
        "/setu noai - 排除AI作品",
        "/setu #白丝,黑丝 - OR搜索",
        "/setu 5 #风景 横图 - 组合筛选",
    ]
    enable_command = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.api = LoliconAPI()
        self.cooldown_cache: Dict[str, float] = {}  # 冷却缓存

    async def execute(self) -> Tuple[bool, str, bool]:
        """执行命令"""
        try:
            # 解析参数
            args_str = ""
            if self.matched_groups and "args" in self.matched_groups:
                args_str = self.matched_groups["args"] or ""
            args_str = args_str.strip()

            # 处理 help 命令
            if args_str.lower() in ["help", "帮助", "?", "？"]:
                help_msg = """🎨 Lolicon色图插件使用帮助

📌 基础命令:
  /setu              获取1张随机图片
  /setu 3            获取3张图片
  /setu help         显示此帮助信息

🏷️ 标签搜索 (用 # 号):
  /setu #萝莉        搜索萝莉标签
  /setu #白丝,黑丝   OR搜索（白丝或黑丝）
  /setu #萝莉 #白丝  AND搜索（萝莉且白丝）

🔍 关键词搜索 (直接输入):
  /setu 原神         搜索原神关键词
  /setu 初音未来      搜索初音未来

📐 长宽比筛选:
  /setu 横图         横图 (长宽比>1)
  /setu 竖图         竖图 (长宽比<1)
  /setu 方图         方图 (长宽比=1)
  /setu gt1.5        自定义长宽比

🤖 其他选项:
  /setu noai         排除AI作品
  /setu r18          R18内容 (需配置)
  /setu uid:12345    指定作者UID

💡 组合示例:
  /setu 3 #萝莉,少女 横图
  /setu #白丝 #JK noai
  /setu 原神 5 竖图
  /setu #风景 #唯美 gt1.5 noai

📝 兼容旧格式:
  keyword:原神 = 原神
  tag:萝莉 = #萝莉
  kw:风景 = 风景"""
                await self.send_text(help_msg)
                return True, "显示帮助信息", True

            # 冷却检查
            user_id = self.message.message_info.user_info.user_id
            cooldown = self.get_config("features.cooldown_seconds", 10)
            cooldown_result = self._check_cooldown(user_id, cooldown)
            if not cooldown_result["ready"]:
                remaining = cooldown_result["remaining"]
                await self.send_text(f"⏰ 冷却中，还需等待 {remaining} 秒")
                return False, "冷却中", True

            # 解析参数并获取图片
            params = self._parse_args(args_str)
            result = await self._fetch_setu(params)

            if result:
                self._update_cooldown(user_id)
                return True, "已发送色图", True
            else:
                return False, "获取失败", True

        except Exception as e:
            logger.error(f"命令执行失败: {str(e)}", exc_info=True)
            await self.send_text(f"❌ 执行失败: {str(e)}")
            return False, f"执行失败: {str(e)}", True

    def _parse_args(self, args_str: str) -> Dict[str, Any]:
        """解析命令参数"""
        params = {
            "num": self.get_config("features.default_num", 1),
            "r18": 0,
            "exclude_ai": False,
            "keyword": None,
            "tag": [],
            "uid": None,
            "aspect_ratio": None,
        }

        if not args_str:
            return params

        args = args_str.split()

        # 保留的关键词（不会被识别为搜索关键词）
        reserved_keywords = {
            "r18",
            "noai",
            "no_ai",
            "排除ai",
            "horizontal",
            "横图",
            "vertical",
            "竖图",
            "square",
            "方图",
        }

        # 解析参数
        for arg in args:
            arg_lower = arg.lower()

            # 数量
            if arg_lower.isdigit():
                params["num"] = min(max(LoliconAPI.MIN_NUM_PER_REQUEST, int(arg_lower)), LoliconAPI.MAX_NUM_PER_REQUEST)
            # R18
            elif arg_lower == "r18":
                if self.get_config("features.allow_r18", False):
                    params["r18"] = 1
                else:
                    logger.warning("R18功能未启用")
            # 排除AI
            elif arg_lower in ["noai", "no_ai", "排除ai"]:
                params["exclude_ai"] = True
            # 长宽比快捷方式
            elif arg_lower in ["horizontal", "横图"]:
                params["aspect_ratio"] = "gt1"
            elif arg_lower in ["vertical", "竖图"]:
                params["aspect_ratio"] = "lt1"
            elif arg_lower in ["square", "方图"]:
                params["aspect_ratio"] = "eq1"
            # 标签搜索 - 新格式 #标签
            elif arg.startswith("#"):
                tag_str = arg[1:]  # 去掉#号
                # 支持逗号分隔的OR搜索
                tags = [t.strip() for t in tag_str.split(",")]
                params["tag"].append(tags)
            # 标签搜索 - 旧格式 tag:标签（兼容）
            elif arg.startswith("tag:"):
                tag_str = arg.split(":", 1)[1]
                tags = [t.strip() for t in tag_str.split(",")]
                params["tag"].append(tags)
            # 关键词搜索 - 旧格式 keyword:关键词（兼容）
            elif arg.startswith("keyword:") or arg.startswith("kw:"):
                params["keyword"] = arg.split(":", 1)[1]
            # UID搜索
            elif arg.startswith("uid:"):
                try:
                    uid = int(arg.split(":", 1)[1])
                    params["uid"] = [uid]
                except ValueError:
                    logger.warning(f"无效的UID: {arg}")
            # 长宽比表达式
            elif self._validate_aspect_ratio(arg_lower):
                params["aspect_ratio"] = arg_lower
            # 关键词搜索 - 新格式（纯文本）
            elif arg_lower not in reserved_keywords:
                # 如果已有关键词，添加空格连接
                if params["keyword"]:
                    params["keyword"] += " " + arg
                else:
                    params["keyword"] = arg

        return params

    def _validate_aspect_ratio(self, ratio_str: str) -> bool:
        """验证长宽比表达式格式

        Args:
            ratio_str: 长宽比表达式，如 "gt1.5", "lt0.8", "gt1.5lt2.0"

        Returns:
            bool: 是否有效
        """
        if not ratio_str:
            return False

        # 使用正则验证格式
        match = self.ASPECT_RATIO_PATTERN.match(ratio_str)
        if not match:
            return False

        # 验证数值范围（长宽比应该在合理范围内，如 0.1 到 10）
        groups = match.groups()
        try:
            ratio1 = float(groups[1])
            if ratio1 < 0.1 or ratio1 > 10:
                logger.warning(f"长宽比数值超出合理范围: {ratio1}")
                return False

            # 如果有第二个条件
            if groups[2] and groups[3]:
                ratio2 = float(groups[3])
                if ratio2 < 0.1 or ratio2 > 10:
                    logger.warning(f"长宽比数值超出合理范围: {ratio2}")
                    return False

            return True
        except (ValueError, IndexError) as e:
            logger.warning(f"长宽比验证失败: {str(e)}")
            return False

    async def _fetch_setu(self, params: Dict[str, Any]) -> bool:
        """获取色图"""
        size_list = self.get_config("features.size_list", ["regular"])
        use_forward = self.get_config("features.use_forward_message", True)
        proxy = self.get_config("features.proxy", "i.pixiv.re")

        # 调用API获取图片
        result = await self.api.fetch_setu(
            r18=params["r18"],
            num=params["num"],
            uid=params.get("uid"),
            keyword=params.get("keyword"),
            tag=params.get("tag") if params.get("tag") else None,
            size=size_list,
            proxy=proxy,
            exclude_ai=params.get("exclude_ai", False),
            aspect_ratio=params.get("aspect_ratio"),
        )

        if result.get("error"):
            await self.send_text(f"❌ 获取失败: {result['error']}")
            return False

        data = result.get("data", [])
        if not data:
            await self.send_text("😢 没有找到符合条件的图片")
            return False

        # 使用合并转发格式发送
        if use_forward:
            forward_messages = []
            # 使用bot的QQ和昵称
            bot_qq = str(global_config.bot.qq_account)
            bot_name = str(global_config.bot.nickname)

            for i, item in enumerate(data, 1):
                # 构建信息文本
                info_text = f"【{i}/{len(data)}】{item['title']}\n"
                info_text += f"👤 {item['author']}\n"
                info_text += f"🆔 PID: {item['pid']}\n"
                info_text += f"📏 {item['width']}x{item['height']}\n"

                # AI标签
                ai_type = item.get("aiType", 0)
                if ai_type == 2:
                    info_text += "🤖 AI作品\n"
                elif ai_type == 1:
                    info_text += "✋ 非AI作品\n"

                # R18标签
                if item.get("r18"):
                    info_text += "🔞 R18\n"

                # 标签
                tags = item.get("tags", [])
                if tags:
                    tag_str = " ".join(tags[:5])
                    info_text += f"🏷️ {tag_str}"

                # 图片URL
                urls = item.get("urls", {})
                # 优先使用regular，fallback到其他规格
                url = urls.get("regular") or urls.get("original") or urls.get("small")

                if url:
                    # 构建消息节点：文本 + 图片URL
                    message_content = [("text", info_text), ("imageurl", url)]
                    forward_messages.append((bot_qq, bot_name, message_content))

            # 发送合并转发消息
            if forward_messages:
                await self.send_forward(forward_messages, storage_message=True)
                return True

        # 传统方式发送（一条条发送）
        else:
            for i, item in enumerate(data, 1):
                # 构建信息文本
                info_text = f"【{i}/{len(data)}】{item['title']}\n"
                info_text += f"👤 {item['author']}\n"
                info_text += f"🆔 PID: {item['pid']}\n"
                info_text += f"📏 {item['width']}x{item['height']}\n"

                # AI标签
                ai_type = item.get("aiType", 0)
                if ai_type == 2:
                    info_text += "🤖 AI作品\n"
                elif ai_type == 1:
                    info_text += "✋ 非AI作品\n"

                # R18标签
                if item.get("r18"):
                    info_text += "🔞 R18\n"

                # 标签
                tags = item.get("tags", [])
                if tags:
                    tag_str = " ".join(tags[:5])
                    info_text += f"🏷️ {tag_str}\n"

                # 图片URL
                urls = item.get("urls", {})
                url = urls.get("regular") or urls.get("original") or urls.get("small")

                if url:
                    info_text += f"🔗 {url}"

                    # 发送文本
                    await self.send_text(info_text)

                    # 直接发送图片URL
                    try:
                        await self.send_custom("imageurl", url)
                    except Exception as e:
                        logger.error(f"发送图片失败: {str(e)}")
                        await self.send_text("⚠️ 图片发送失败，请使用上方链接查看")

                    # 间隔避免刷屏
                    if i < len(data):
                        await asyncio.sleep(1)

        return True

    def _check_cooldown(self, user_id: str, cooldown: int) -> dict:
        """检查冷却时间

        Returns:
            dict: {"ready": bool, "remaining": int} ready表示是否可以执行，remaining表示剩余冷却时间（秒）
        """
        # 定期清理过期缓存
        self._cleanup_cooldown_cache(cooldown)

        if user_id not in self.cooldown_cache:
            return {"ready": True, "remaining": 0}

        last_use = self.cooldown_cache[user_id]
        elapsed = time.time() - last_use

        if elapsed >= cooldown:
            return {"ready": True, "remaining": 0}
        else:
            remaining = int(cooldown - elapsed) + 1  # 向上取整
            return {"ready": False, "remaining": remaining}

    def _update_cooldown(self, user_id: str):
        """更新冷却时间"""
        self.cooldown_cache[user_id] = time.time()

    def _cleanup_cooldown_cache(self, cooldown: int):
        """清理过期的冷却缓存"""
        # 当缓存超过阈值时才清理
        if len(self.cooldown_cache) <= self.COOLDOWN_CLEANUP_THRESHOLD:
            return

        current_time = time.time()
        # 清理所有已过冷却期的条目
        expired_users = [
            user_id for user_id, last_use in self.cooldown_cache.items()
            if current_time - last_use >= cooldown
        ]

        for user_id in expired_users:
            del self.cooldown_cache[user_id]

        # 如果清理后仍然超过最大容量，删除最旧的条目
        if len(self.cooldown_cache) > self.MAX_COOLDOWN_CACHE_SIZE:
            sorted_items = sorted(self.cooldown_cache.items(), key=lambda x: x[1])
            excess_count = len(self.cooldown_cache) - self.MAX_COOLDOWN_CACHE_SIZE
            for user_id, _ in sorted_items[:excess_count]:
                del self.cooldown_cache[user_id]

            logger.info(f"冷却缓存已清理 {len(expired_users) + excess_count} 个过期/过量条目")


@register_plugin
class LoliconSetuPlugin(BasePlugin):
    """
    Lolicon色图插件

    基于 api.lolicon.app 的色图获取插件
    支持标签搜索、关键词搜索、长宽比筛选等高级功能

    功能特性:
    - /setu命令获取图片
    - 支持标签AND/OR组合搜索
    - 支持关键词模糊搜索
    - 支持长宽比筛选
    - AI作品排除选项
    - 合并转发格式

    注意事项:
    - 多图发送可能会失败，建议一次1-3张图最好
    """

    # 插件基本信息
    plugin_name: str = "lolicon_setu_plugin"
    enable_plugin: bool = True
    dependencies: List[str] = []
    python_dependencies: List[str] = ["aiohttp"]
    config_file_name: str = "config.toml"

    # 配置节描述
    config_section_descriptions = {"plugin": "插件基本配置", "components": "组件启用控制", "features": "功能配置"}

    # 配置Schema定义
    config_schema: dict = {
        "plugin": {
            "config_version": ConfigField(type=str, default="2.0.0", description="配置文件版本"),
            "enabled": ConfigField(type=bool, default=True, description="是否启用插件"),
        },
        "components": {
            "enable_command": ConfigField(type=bool, default=True, description="是否启用/setu命令"),
        },
        "features": {
            "default_num": ConfigField(type=int, default=1, description="默认获取数量"),
            "allow_r18": ConfigField(type=bool, default=False, description="是否允许R18内容"),
            "cooldown_seconds": ConfigField(type=int, default=10, description="命令冷却时间(秒)"),
            "size_list": ConfigField(type=list, default=["regular"], description="图片规格列表"),
            "api_timeout": ConfigField(type=int, default=30, description="API请求超时时间(秒)"),
            "use_forward_message": ConfigField(
                type=bool,
                default=True,
                description="是否使用合并转发(聊天记录)格式发送图片。注意: 多图发送可能会失败，建议一次1-3张图最好",
            ),
            "proxy": ConfigField(type=str, default="i.pixiv.re", description="图片代理服务器"),
        },
    }

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        """返回插件包含的组件列表"""
        # 验证配置
        self._validate_config()

        components = []

        if self.get_config("components.enable_command", True):
            components.append((SetuCommand.get_command_info(), SetuCommand))

        return components

    def _validate_config(self):
        """验证配置的有效性"""
        # 验证 default_num
        default_num = self.get_config("features.default_num", 1)
        if not isinstance(default_num, int) or default_num < 1 or default_num > 20:
            logger.warning(f"配置 default_num 无效: {default_num}，将使用默认值 1")
            # 这里无法直接修改配置，只能记录警告

        # 验证 cooldown_seconds
        cooldown = self.get_config("features.cooldown_seconds", 10)
        if not isinstance(cooldown, (int, float)) or cooldown < 0:
            logger.warning(f"配置 cooldown_seconds 无效: {cooldown}，将使用默认值 10")

        # 验证 api_timeout
        timeout = self.get_config("features.api_timeout", 30)
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            logger.warning(f"配置 api_timeout 无效: {timeout}，将使用默认值 30")

        # 验证 size_list
        size_list = self.get_config("features.size_list", ["regular"])
        valid_sizes = {"original", "regular", "small", "thumb", "mini"}
        if not isinstance(size_list, list) or not size_list:
            logger.warning(f"配置 size_list 无效: {size_list}，将使用默认值 ['regular']")
        else:
            invalid_sizes = [s for s in size_list if s not in valid_sizes]
            if invalid_sizes:
                logger.warning(f"配置 size_list 包含无效值: {invalid_sizes}，有效值为: {valid_sizes}")

        # 验证 proxy
        proxy = self.get_config("features.proxy", "i.pixiv.re")
        if not isinstance(proxy, str) or not proxy.strip():
            logger.warning(f"配置 proxy 无效或为空: {proxy}，可能导致图片无法访问")

        logger.info("配置校验完成")