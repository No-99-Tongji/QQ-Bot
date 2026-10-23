import json
import os
import re
import time

from openai import AsyncOpenAI
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
# 每次搭话的冷却时间（秒），避免连续刷屏/自循环
COOLDOWN_TIME = 2
# 组装给 llm 的群上下文条数
CONTEXT_LENGTH = 10
# 工具调用的最大往返次数
MAX_TOOL_TURNS = 6

# llm 可调用的"插件"工具。key 是对配置里 allowed_plugins 的简短名。
# tool: 暴露给 llm 的 function 定义
# plugin: 实际调用的插件类名（从 bot.plugins_list 里按 name 找）
# build_event: (args, self) -> 合成给该插件 main 的触发消息
TOOL_REGISTRY = {
    "sing": {
        "tool": {
            "type": "function",
            "function": {
                "name": "sing_song",
                "description": (
                    "真正播放某首歌（发语音）。只有当用户明确发出唱/点歌/播放的指令时才"
                    "调用，例如'唱首浮夸''点歌''放一首X'。若用户只是在问歌名、考歌、"
                    "讨论歌词或纠正你，绝对不要调用此工具，直接用文字回答。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"song": {"type": "string", "description": "歌名或点歌关键词"}},
                    "required": ["song"],
                },
            },
        },
        "plugin": "TheresaSing",
        "build_event": lambda a: f"Eason sing {a['song']}",
    },
    "persona": {
        "tool": {
            "type": "function",
            "function": {
                "name": "show_persona",
                "description": (
                    "展示某个 QQ 用户的群聊人格画像（基于TA的历史发言总结）。只有当用户明确"
                    "要求查看某人的人格/画像/为人/介绍时才调用。若不知目标QQ或身份，"
                    "先向用户询问是谁，不要凭空编造画像。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "target": {
                            "type": "string",
                            "description": "要画像的QQ号或群名片/昵称，可为空",
                        }
                    },
                    "required": [],
                },
            },
        },
        "plugin": "TheresaPersona",
        "build_event": lambda a: (
            f"Eason persona [CQ:at,qq={a['target']}]"
            if (a.get("target", "") or "").isdigit()
            else f"Eason persona {a.get('target', '')}".rstrip()
        ),
    },
}

SINGER_PERSONA = (
    "你是一个热爱陈奕迅（Eason Chan）的歌手，会唱很多陈奕迅的歌，像《十年》《浮夸》"
    "《富士山下》《K歌之王》《单车》《好久不见》这些张口就来。"
    "你在和朋友们的 QQ 群里。\n"
    "关于工具：\n"
    "1. 只有在用户明确要求你唱歌/点歌/播放某首歌时，才调用 sing_song 工具真正播放。\n"
    "2. 只有在用户明确要求查看某个人的画像/人格/介绍时，才调用 show_persona 工具。\n"
    "3. 如果用户只是在聊天、问歌名、考你歌、纠正你、讨论歌词，绝不要调用唱歌工具。\n"
    "4. 当你调用了工具，就等待工具结果，再依据结果简短回应。绝不要用文字假装你已经"
    "唱了或介绍了——除非你真的调用了工具并等到成功。\n"
    "回复要求：\n"
    "- 每次回复都非常简短，两三句话以内。\n"
    "- 可以自然引用陈奕迅的歌词或提到他的歌。\n"
    "- 语气亲切、随性，像个爱唱歌的朋友，不要说教，不要用 markdown。"
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
        # 触发当前对话的群/用户上下文，供工具合成事件用
        self._last_group_id = 0
        self._last_user_id = 0
        self._last_nick = ""

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

    def _enabled_tools(self) -> list[dict]:
        """从配置 allowed_plugins 读取允许调用的插件，返回对应的工具定义"""
        raw = self.config.get("allowed_plugins", "") if self.config else ""
        allowed = [s.strip() for s in raw.split(",") if s.strip()]
        return [entry["tool"] for name, entry in TOOL_REGISTRY.items() if name in allowed]

    def _synthetic_event(self, message: str) -> GroupMessageEvent:
        """构造一个模拟的群消息事件，驱动目标插件的 main"""
        return GroupMessageEvent(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": self._last_group_id,
                "user_id": self._last_user_id,
                "message_id": -1,
                "message": message,
                "raw_message": message,
                "sender": {
                    "user_id": self._last_user_id,
                    "nickname": self._last_nick,
                    "card": "",
                    "role": "",
                },
            }
        )

    async def _execute_plugin(self, plugin_name: str, ev_message: str) -> str:
        """在群里执行目标插件（构造合成事件调用其 main），返回一句成功/失败反馈"""
        inst = next((p for p in self.bot.plugins_list if p.name == plugin_name), None)
        if inst is None:
            return f"执行失败：插件 {plugin_name} 未加载"
        try:
            await inst.main(self._synthetic_event(ev_message), debug=False)
        except Exception as e:
            Log.error(f"插件{self.name} 调用 {plugin_name} 失败：{e}")
            return f"执行失败:{e}"
        return f"已完成（已在群里处理：{ev_message}）"

    async def _run_tool(self, fn_name: str, arguments_json: str) -> str:
        """根据 llm 发出的工具名 + 参数，执行对应插件，返回给 llm 的结果"""
        for entry in TOOL_REGISTRY.values():
            if entry["tool"]["function"]["name"] != fn_name:
                continue
            try:
                a = json.loads(arguments_json or "{}") or {}
            except Exception:
                a = {}
            if fn_name == "show_persona" and not str(a.get("target", "")).strip():
                a["target"] = str(self._last_user_id)  # 未指定目标 → 本人
            msg = entry["build_event"](a)
            return await self._execute_plugin(entry["plugin"], msg)
        return f"未知工具:{fn_name}"

    async def _chat(self, messages: list[dict], tools: list[dict]) -> str:
        """带工具调用的对话主循环；若无工具则退化为普通单轮对话"""
        if not tools:
            return await get_llm_response(messages, model="deepseek-v4-pro", thinking_enabled=False)
        client = AsyncOpenAI(api_key=os.environ["DPSK_KEY"], base_url="https://api.deepseek.com")
        Log.debug(
            f"插件{self.name}：本次对话启用工具 {[t['function']['name'] for t in tools]}", True
        )
        for _ in range(MAX_TOOL_TURNS):
            response = await client.chat.completions.create(
                model="deepseek-v4-pro",
                messages=messages,
                tools=tools,
                temperature=0.7,
                reasoning_effort="medium",
                extra_body={"thinking": {"type": "disabled"}},
            )
            message = response.choices[0].message
            if not message.tool_calls:
                return message.content or "[NO REPLY]"
            Log.debug(
                f"插件{self.name}：llm 发起工具调用 "
                + ", ".join(
                    f"{t.function.name}({t.function.arguments})" for t in message.tool_calls
                ),
                True,
            )
            messages.append(message)
            for tc in message.tool_calls:
                result = await self._run_tool(tc.function.name, tc.function.arguments)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
        return "[NO REPLY]"

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
        self._last_group_id = group_id
        self._last_user_id = event.user_id
        self._last_nick = event.nickname or ""
        try:
            context = await self._load_context(group_id)
            messages: list[dict] = [{"role": "system", "content": SINGER_PERSONA}]
            messages.extend(context)
            messages.append({"role": "user", "content": text})

            tools = self._enabled_tools()
            response = await self._chat(messages, tools)
            if "[NO REPLY]" in response or not response.strip():
                return
            self.api.groupService.send_group_msg(group_id=group_id, message=response.strip())
            Log.debug(f"插件{self.name}：群{group_id} 回复：「{response}」", debug)
        except Exception as e:
            Log.error(f"插件{self.name}运行时出错：{e}")
            self.api.groupService.send_group_msg(
                group_id=group_id, message=f"我嗓子卡壳了，待会儿再聊~（{e}）"
            )
