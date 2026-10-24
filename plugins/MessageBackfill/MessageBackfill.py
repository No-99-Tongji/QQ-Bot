import asyncio
from datetime import UTC, datetime

from sqlalchemy import BigInteger, Column, DateTime, Text, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker

from plugins import Plugins
from src.PrintLog import Log
from utils.CQHelper import CQHelper

Base = declarative_base()


class Message(Base):
    __tablename__ = "messages"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=False)
    group_id = Column(BigInteger, nullable=False)
    msg = Column(Text, nullable=False)
    send_time = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    msg_id = Column(BigInteger, nullable=False, default=0)
    message_seq = Column(BigInteger, nullable=True)
    user_nickname = Column(Text, nullable=False, default=" ")
    user_card = Column(Text, nullable=False, default=" ")


class MessageBackfill(Plugins):
    """掉线窗口断档补录。

    MessageRecorder 只被动记录在线时收到的事件，掉线期间(重启/网络/QQ 被踢)的群消息会缺失。
    本插件作为后台任务周期运行：对每个生效群调用 get_group_msg_history 拉取群历史，
    把库中缺失的 (按 msg_id 去重) 消息补录进去，图片照 MessageRecorder 转成本地路径。

    只在真正存在断档时工作：从最新消息往前翻页，遇到一整页都已在库(mid 全部命中)即到达连续区，
    停止往回，避免无限/无谓请求。首次(库为空)会从最新往回补 max_pages 页建起基础历史。
    """

    def __init__(self, server_address, bot):
        super().__init__(server_address, bot)
        self.name = "MessageBackfill"
        self.type = "Backfill"  # 不参与群消息事件分发，仅作为后台任务
        self.author = "Heai"
        self.introduction = """后台任务：周期补录掉线期间缺失的群消息到数据库
                                usage: 自动"""
        self.background = True  # 声明启动 run_background 后台循环
        self.init_status()
        self.session_factory = sessionmaker(
            bind=self.bot.database, class_=AsyncSession, expire_on_commit=False
        )
        # 注意：self.config 由 create_plugin_instance 在 __init__ 之后注入，这里不能读，
        # 统一在 run_background 里惰性读取。
        self._interval = None
        self._max_pages = None
        self._page_size = None

    def _load_tunables(self) -> None:
        if self._interval is None:
            self._interval = int(self.config.get("interval", "120")) if self.config else 120
            self._max_pages = int(self.config.get("max_pages", "20")) if self.config else 20
            self._page_size = int(self.config.get("page_size", "20")) if self.config else 20

    # ---- 图片路径解析（与 MessageRecorder.resolve_msg 一致）----
    def resolve_msg(self, message: str) -> str:
        cqs = CQHelper.loads_cq(message)
        for cq in cqs:
            if cq.cq_type == "image":
                origin = str(cq)
                try:
                    path = (
                        self.api.messageService.get_image(cq.file).get("data", {}).get("file", None)
                    )
                except Exception:
                    path = None
                if path is not None:
                    cq.path = path
                    del cq.url
                    message = message.replace(origin, str(cq))
        return message

    async def run_background(self) -> None:
        self._load_tunables()
        while True:
            try:
                await self._backfill_all()
            except Exception as e:
                Log.error(f"插件{self.name}：补录循环出错：{e}")
            Log.debug(f"插件{self.name}：本轮补录结束，{self._interval}s 后再跑", False)
            await asyncio.sleep(self._interval)

    async def _backfill_all(self) -> None:
        groups = list(self.effected_groups)
        if not groups:
            return
        for group_id in groups:
            try:
                await self._backfill_group(group_id)
            except Exception as e:
                Log.error(f"插件{self.name}：群{group_id} 补录失败：{e}")

    async def _existing_msg_ids(self, group_id: int) -> set[int]:
        async with self.session_factory() as session:
            from sqlalchemy import select

            res = await session.execute(select(Message.msg_id).where(Message.group_id == group_id))
            return {row[0] for row in res.all()}

    async def _insert(self, group_id: int, m: dict) -> None:
        sender = m.get("sender", {}) or {}
        msg = self.resolve_msg(m.get("raw_message") or m.get("message") or "")
        async with self.session_factory() as session:
            row = Message(
                user_id=m["user_id"],
                group_id=group_id,
                msg=msg,
                send_time=datetime.fromtimestamp(m["time"], tz=UTC),
                msg_id=m["message_id"],
                message_seq=m.get("message_seq"),
                user_nickname=sender.get("nickname", " "),
                user_card=sender.get("card", " "),
            )
            session.add(row)
            await session.commit()

    async def _backfill_group(self, group_id: int) -> None:
        existing = await self._existing_msg_ids(group_id)
        seq_cursor = 0  # 0 = 取该群最新一批
        inserted = 0
        for _ in range(self._max_pages):
            resp = self.api.messageService.get_group_msg_history(
                group_id, message_seq=seq_cursor, return_size=self._page_size
            )
            data = resp.get("data") or {}
            msgs = data.get("messages") or []
            if not msgs:
                break

            mid_seqs = [(m.get("message_id"), m.get("message_seq")) for m in msgs]
            lowest_seq = min(
                (seq for _, seq in mid_seqs if seq is not None),
                default=None,
            )
            page_all_known = True
            for m in msgs:
                mid = m.get("message_id")
                if mid in existing:
                    continue
                page_all_known = False
                await self._insert(group_id, m)
                existing.add(mid)
                inserted += 1

            # 一整页都已存在 → 已到达库中连续区，断档补完，停止往回翻
            if page_all_known:
                break
            if lowest_seq is None:
                break
            seq_cursor = lowest_seq - 1

        if inserted:
            Log.info(f"插件{self.name}：群{group_id} 补录 {inserted} 条断档消息")
