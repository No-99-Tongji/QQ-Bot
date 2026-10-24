import asyncio
import json
import os
import re
import shlex
import signal
import subprocess
import time

from sqlalchemy import BigInteger, Column, DateTime, Text, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker

from plugins import Plugins, plugin_main
from src.event_handler.GroupMessageEventHandler import GroupMessageEvent
from src.PrintLog import Log
from utils.AITools import get_llm_response
from utils.CQHelper import CQHelper

Base = declarative_base()

# 触发词：提到 bot 的名字就搭话
TRIGGER_WORDS = ["逸浔", "小逸"]
# 每次搭话的冷却时间（秒），避免连续刷屏/自循环
COOLDOWN_TIME = 3
# 组装给 llm 的群上下文条数
CONTEXT_LENGTH = 10

SYSTEM_HINT = (
    "你是QQ群里的 bot「逸浔/小逸」，一个热爱陈奕迅的歌手，会唱很多陈奕迅的歌。\n"
    "规则：\n"
    "1. 回复必须非常简短，一两句话即可，亲切随性，可用陈奕迅歌词/提到他的歌。\n"
    "2. 用户要求唱歌/点歌/播放时，必须调用 sing_song 工具真正播放（歌名要从我给你的'可唱歌曲'里挑一个），"
    "绝不能只口头说'来一首/这就唱'而不调用工具。用户要求看某人的画像/人格时调用 show_persona。"
    "只是聊天/问歌名/考歌/纠正时不要调用工具。\n"
    "3. 每个请求最多调用一次工具。调用后立即基于结果简短回应，不要再做额外步骤或补话。\n"
    "4. 不要假装已经做了而实际没调用工具；不要用 markdown。\n"
    "5. 调用 show_persona 后，它会自动把完整画像原样发到群里；你只需简短确认一句"
    "（例如'画像发上去了'），不要再复述、概括或转发画像内容。"
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
                                提到 bot 名字（逸浔/小逸）时，转交给 opencode（自动模式）对话，
                                opencode 通过 MCP 调用唱歌/人物画像等能力，结果汇总后回群里
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

    async def _load_context(self, group_id: int) -> list[str]:
        """拉取该群最近的聊天上下文（正序）；跳过以 Eason 开头的 bot 指令"""
        async with self.session_factory() as session:
            stmt = (
                select(Message)
                .where(Message.group_id == group_id)
                .order_by(desc(Message.id))
                .limit(CONTEXT_LENGTH * 2)
            )
            result = await session.execute(stmt)
            rows = list(result.scalars().all())
        lines: list[str] = []
        for row in reversed(rows):
            # 用 _records_text 而非 _clean：保留上下文里的图片为 [图:本地路径]，
            # 让 opencode 的 read 也能读到群历史中的图片；其余 CQ 照常去掉。
            text = self._records_text(row.msg)
            if not text or text.lower().startswith("eason"):
                continue
            lines.append(f"{row.user_nickname}: {text}")
            if len(lines) >= CONTEXT_LENGTH:
                break
        return lines

    @staticmethod
    def _extract_at(message: str) -> tuple[int | None, str | None]:
        """从触发消息里提取被 @ 的 (QQ号, 名字)，无则 None"""
        for cq in CQHelper.loads_cq(message):
            qq = getattr(cq, "qq", "")
            if cq.cq_type == "at" and str(qq).isdigit():
                return int(qq), getattr(cq, "name", "") or str(qq)
        # 也接受纯文本 "@名字" 的常见写法
        m = re.search(r"@([^\s@，。？！：:]+)", message)
        if m:
            return None, m.group(1)
        return None, None

    @staticmethod
    def _available_song_names() -> list[str]:
        """本机曲库可唱的歌名（从共享数据目录读取）"""
        d = "/opt/qq-bot/shared/plugins/TheresaSing/data/songs"
        if not os.path.isdir(d):
            return []
        return sorted(os.path.splitext(f)[0] for f in os.listdir(d) if f.lower().endswith(".mp3"))

    _IMAGE_PATH_RE = re.compile(r"\[CQ:image[^\]]*?path=([^,\]\s]+)")

    @classmethod
    def _records_text(cls, msg: str) -> str:
        """把图片转成 [图:<本地路径>] 标记（供 opencode read 识图），其余 CQ 去掉"""
        paths = cls._IMAGE_PATH_RE.findall(msg)
        text = cls._clean(msg)
        if paths:
            marker = " ".join(f"[图:{p}]" for p in paths)
            text = (text + " " + marker).strip() if text else marker
        return text

    async def _resolve_reply_lines(self, message: str, group_id: int) -> list[str]:
        """解析 [CQ:reply,id=...]，从库取被引用消息的内容（含图片路径）"""
        lines: list[str] = []
        for cq in CQHelper.loads_cq(message):
            if cq.cq_type != "reply":
                continue
            rid = str(cq.id)
            if not rid.lstrip("-").isdigit():
                continue
            async with self.session_factory() as session:
                res = await session.execute(
                    select(Message)
                    .where(Message.group_id == group_id, Message.msg_id == int(rid))
                    .order_by(desc(Message.id))
                )
                row = res.scalars().first()
            if row is not None:
                nick = row.user_nickname or f"QQ:{row.user_id}"
                lines.append(f"[引用{nick}的消息] {self._records_text(row.msg)}")
        return lines

    @staticmethod
    def _parse_opencode_output(raw: str) -> str:
        """从 opencode `--format json`（PTY 下可能夹 ANSI）输出里提取最终文本回复"""
        raw = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", raw)
        texts: list[str] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            part = obj.get("part") or {}
            if part.get("type") == "text" and part.get("text"):
                texts.append(part["text"])
        return "\n".join(texts).strip()

    def _run_opencode_blocking(self, prompt: str) -> str:
        """在伪终端里跑 opencode（headless 需要 TTY 才能驱动），返回最终回复文本"""
        opencode_bin = os.environ.get("OPENCODE_BIN", "/usr/local/bin/opencode")
        model = os.environ.get("OPENCODE_MODEL", "deepseek/deepseek-flash")
        shell_cmd = f"{opencode_bin} run --format json -m {model} --auto " + shlex.quote(prompt)
        # 把 bot 的 key / DB / OneBot 环境传给 opencode（其 MCP server 也要用）
        env = dict(os.environ)
        env.setdefault("DEEPSEEK_API_KEY", env.get("DPSK_KEY", ""))
        env.setdefault("DB_USER", getattr(self.bot, "database_username", "theresa"))
        env.setdefault("DB_ADDR", getattr(self.bot, "database_address", "localhost:5432"))
        env.setdefault("DB_NAME", getattr(self.bot, "database_name", "bot"))
        env.setdefault("DB_PASS", env.get("DB_PASS") or getattr(self.bot, "database_passwd", ""))
        env.setdefault("ONEBOT_URL", f"http://{self.bot.server_address}")
        env.setdefault("SONGS_DIR", "/opt/qq-bot/shared/plugins/TheresaSing/data/songs")
        start = time.time()
        # 用独立的进程组，便于超时时把 opencode 及其脚本一起杀掉，避免孤儿进程堆积
        proc = subprocess.Popen(
            ["/usr/bin/script", "-qec", shell_cmd, "/dev/null"],
            cwd=os.getcwd(),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            out, _ = proc.communicate(timeout=240)
        except subprocess.TimeoutExpired:
            Log.error(f"插件{self.name}：opencode 超时(>{240}s)，已终止进程组")
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                pass
            proc.wait()
            raise TimeoutError("opencode 处理超时") from None
        Log.debug(f"插件{self.name}：opencode 本次耗时 {int(time.time() - start)}s", True)
        out = out or ""
        parsed = self._parse_opencode_output(out)
        if parsed:
            return parsed
        return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", out).strip() or "[NO REPLY]"

    async def _run_opencode(self, prompt: str) -> str:
        return await asyncio.to_thread(self._run_opencode_blocking, prompt)

    async def _fallback_chat(
        self, context: list[str], text: str, group_id: int, user_id: int
    ) -> str:
        """opencode 不可用时的简单兜底：单次 deepseek（不带工具循环）"""
        system = (
            SYSTEM_HINT
            + f"\n当前群号：{group_id}。若能承载就简短应答；无法调用工具时就直接文字回应。"
        )
        messages: list[dict] = [
            {"role": "system", "content": system},
            *[{"role": "user", "content": c} for c in context[-CONTEXT_LENGTH:]],
            {"role": "user", "content": text},
        ]
        return await get_llm_response(messages, model="deepseek-v4-pro", thinking_enabled=False)

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

        now = time.time()
        if now - self.group_cooldown.get(group_id, 0) < COOLDOWN_TIME:
            return
        self.group_cooldown[group_id] = now

        Log.debug(f"插件{self.name}：群{group_id} 被消息「{text}」触发，转交 opencode", debug)
        try:
            context = await self._load_context(group_id)
            at_qq, at_name = self._extract_at(event.message)
            at_line = ""
            if at_qq:
                at_line = (
                    f"\n本条消息@了用户：{at_name or at_qq}（QQ号 {at_qq}）。"
                    f"若用户要分析其画像/人格，请把 show_persona 的 target 填 {at_qq}。"
                )
            elif at_name:
                at_line = f"\n用户提到的目标名字：{at_name}（可尝试用 show_persona 的 target 填入该名字）。"
            song_names = self._available_song_names()
            songs_line = (
                f"\n我能唱的歌曲（调用 sing_song 时，song 请取其中之一）：{'、'.join(song_names)}。"
                if song_names
                else ""
            )
            reply_lines = await self._resolve_reply_lines(event.message, group_id)
            reply_line = ""
            if reply_lines:
                reply_line = (
                    "\n用户引用/回复了你这条消息，若要了解其中图片内容，请用 read 工具读取 [图:...] 里的路径：\n"
                    + "\n".join(reply_lines)
                )

            prompt = (
                SYSTEM_HINT
                + at_line
                + songs_line
                + reply_line
                + f"\n\n当前会话：群{group_id}（{event.group_name or ''}），时间 {time.strftime('%Y-%m-%d %H:%M')}，"
                f"发消息的用户：{event.nickname or event.user_id}（QQ号 {event.user_id}）。\n"
                + ("最近群聊内容：\n" + "\n".join(context) if context else "（暂无历史）")
                + f"\n\n用户最新消息：{text}\n请给出你的回复。"
            )

            self.api.groupService.send_group_msg(group_id=group_id, message="收到，我酝酿一下~")
            response = await self._run_opencode(prompt)
            if "[NO REPLY]" in response or not response.strip():
                response = await self._fallback_chat(context, text, group_id, event.user_id)
                if "[NO REPLY]" in response or not response.strip():
                    response = "我这会儿有点卡，你稍等再来找我呀~"
            self.api.groupService.send_group_msg(group_id=group_id, message=response)
            Log.debug(f"插件{self.name}：群{group_id} 回复：「{response}」", debug)
        except Exception as e:
            Log.error(f"插件{self.name}运行时出错：{e}")
            try:
                fb = await self._fallback_chat(
                    await self._load_context(group_id), text, group_id, event.user_id
                )
                if "[NO REPLY]" in fb or not fb.strip():
                    fb = "刚刚想事情走神了，要不你再和我说一遍？"
                self.api.groupService.send_group_msg(group_id=group_id, message=fb)
            except Exception as e2:
                Log.error(f"插件{self.name}兜底也失败：{e2}")
                self.api.groupService.send_group_msg(
                    group_id=group_id, message="我嗓子卡壳了，待会儿再聊~"
                )
