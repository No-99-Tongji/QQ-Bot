import re
import time

from sqlalchemy import BigInteger, Column, DateTime, Text, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker

from plugins import Plugins, plugin_main
from src.event_handler.GroupMessageEventHandler import GroupMessageEvent
from src.PrintLog import Log
from utils.AITools import get_llm_response

Base = declarative_base()

# 触发词：提到 bot 的名字就搭话
TRIGGER_WORDS = ["逸浔", "小逸"]
# 每次搭话的冷却时间（秒），避免连续刷屏/自o循环
COOLDOWN_TIME = 2
# 组装给 llm 的群上下文条数
CONTEXT_LENGTH = 10

SINGER_PERSONA = (
    "你是一个热爱陈奕迅（Eason Chan）的歌手，会唱很多陈奕迅的歌，像《十年》《浮夸》"
    "《富士山下》《K歌之王》《单车》《好久不见》这些张口就来。"
    "你在和朋友们的 QQ 群里。"
    "要求：\n"
    "1. 每次回复都必须非常简短，两三句话以内。\n"
    "2. 可以自然地引用陈奕迅的歌词或提到他的歌来回应。\n"
    "3. 语气亲切、随性，像个爱唱歌的朋友/歌手，不要说教。\n"
    "4. 不要使用 markdown，不要寒暄，直接回应内容。"
)


class Message(Base):
    __tablename__ = "messages"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=False)
    group_id = Column(BigInteger, nullable=False)
    msg = Column(Text, nullable=False)
    send_time = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    msg_id = Column(BigInteger, nullable=False, default=0)
    user_nickname = Column(Text, nullable=False, default=" ")
    user_card = Column(Text, nullable=False, default=" ")


class TheresaEason(Plugins):
    def __init__(self, server_address, bot):
        super().__init__(server_address, bot)
        self.name = "TheresaEason"
        self.type = "Group"
        self.author = "Eason"
        self.introduction = """
                                提到 bot 名字（逸浔/小逸）时，以陈奕迅歌迷+歌手的身份简短回应
                                usage: 消息里带上 逸浔 或 小逸 即可
                            """
        self.init_status()
        self.session_factory = sessionmaker(
            bind=self.bot.database, class_=AsyncSession, expire_on_commit=False
        )
        self.group_cooldown: dict[int, float] = {}

    @staticmethod
    def _clean(message: str) -> str:
        """去掉 CQ 码得到纯文本"""
        return re.sub(r"\[CQ:[^\]]*\]", "", message).strip()

    async def _load_context(self, group_id: int) -> list[dict]:
        """拉取该群最近的聊天上下文（排好序的正序消息）"""
        async with self.session_factory() as session:
            stmt = (
                select(Message)
                .where(Message.group_id == group_id)
                .order_by(desc(Message.id))
                .limit(CONTEXT_LENGTH)
            )
            result = await session.execute(stmt)
            rows = list(result.scalars().all())
        return [
            {
                "role": "assistant" if row.user_id == self.bot.bot_id else "user",
                "content": f"{row.user_nickname}({row.user_id}): {self._clean(row.msg)}",
                "name": str(row.user_id),
            }
            for row in reversed(rows)
        ]

    @plugin_main(check_call_word=False, require_db=True)
    async def main(self, event: GroupMessageEvent, debug: bool):
        group_id = event.group_id
        if self.bot.bot_id is not None and event.user_id == self.bot.bot_id:
            return  # 不回自己的消息

        text = self._clean(event.message)
        if not text:
            return
        if not any(word in text for word in TRIGGER_WORDS):
            return

        # 冷却，避免连续刷屏
        now = time.time()
        if now - self.group_cooldown.get(group_id, 0) < COOLDOWN_TIME:
            return
        self.group_cooldown[group_id] = now

        Log.debug(f"插件{self.name}：群{group_id} 被消息「{text}」触发，准备与 llm 对话", debug)
        try:
            context = await self._load_context(group_id)
            messages: list[dict] = [{"role": "system", "content": SINGER_PERSONA}]
            messages.extend(context)
            messages.append({"role": "user", "content": text})

            response = await get_llm_response(
                messages, model="deepseek-v4-pro", thinking_enabled=False
            )
            if "[NO REPLY]" in response or not response.strip():
                return
            self.api.groupService.send_group_msg(group_id=group_id, message=response.strip())
            Log.debug(f"插件{self.name}：群{group_id} 回复：「{response}」", debug)
        except Exception as e:
            Log.error(f"插件{self.name}运行时出错：{e}")
            self.api.groupService.send_group_msg(
                group_id=group_id, message=f"我嗓子卡壳了，待会儿再聊~（{e}）"
            )
