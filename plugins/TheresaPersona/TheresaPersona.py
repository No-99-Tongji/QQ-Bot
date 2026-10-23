import re
from collections import defaultdict

from sqlalchemy import BigInteger, Column, DateTime, Text, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker

from plugins import Plugins, plugin_main
from src.event_handler.GroupMessageEventHandler import GroupMessageEvent
from src.PrintLog import Log
from utils.AITools import get_llm_response
from utils.CQHelper import CQHelper
from utils.CQType import At

Base = declarative_base()

# 每个用户最多拉取的最近发言条数，限制上下文长度
MAX_MESSAGES = 400
# 拼接给 llm 的消息正文的最大字符数预算，超出则只保留最近的
CHAR_BUDGET = 12000

# 人格分析需要覆盖的维度
ANALYSIS_DIRECTIONS = (
    "兴趣爱好",
    "性格特点（外向/内向、乐观/悲观、幽默程度等）",
    "身份/出身（职业、专业、在校学生身份等）",
    "是哪里人（从口音、提到的地名、生活习惯推断）",
    "口头禅（反复出现的高频用语）",
    "在多少个群聊里活跃（只按匿名群标签描述数量和活跃程度，绝不披露真实群名或群号）",
    "常与谁互动、社会关系",
    "作息规律",
    "常用的语气、表情、网络用语",
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


class TheresaPersona(Plugins):
    def __init__(self, server_address, bot):
        super().__init__(server_address, bot)
        self.name = "TheresaPersona"
        self.type = "Group"
        self.author = "Eason"
        self.introduction = """
                                根据某人在各群聊的历史聊天记录，用大模型总结TA的人格画像
                                usage: Eason persona <@某人>
                            """
        self.init_status()
        self.session_factory = sessionmaker(
            bind=self.bot.database, class_=AsyncSession, expire_on_commit=False
        )

    @staticmethod
    def _strip_cq(message: str) -> str:
        """去掉消息里的 CQ 码（at/image/face 等），只保留纯文本"""
        return re.sub(r"\[CQ:[^\]]*\]", "", message).strip()

    def _resolve_target(self, event: GroupMessageEvent) -> tuple[int | None, str | None]:
        """解析触发消息里被 @ 的目标，返回 (QQ号, 未解析到的名字)。

        优先匹配 [CQ:at,qq=xxx] 段；若 @ 以纯文本形式到达（如 @小四），
        则从文字里抓出 `@名字` 并对照当前群成员列表（群名片/昵称）解析出 QQ 号。
        """
        # 1) 标准 CQ at 段
        for cq in CQHelper.loads_cq(event.message):
            if cq.cq_type == "at" and cq.qq.isdigit():
                return int(cq.qq), None

        # 2) 纯文本 @名字，结合当前群成员列表解析
        rest = event.message[len(self.bot.bot_name + " persona") :]
        m = re.search(r"@([^\s@，。？！：:]+)", rest)
        if not m:
            return None, None
        name = m.group(1)
        try:
            members = (
                self.api.groupService.get_group_member_list(group_id=event.group_id).get("data")
                or []
            )
        except Exception:
            members = []
        for mem in members:
            if (
                mem.get("card") == name
                or mem.get("nickname") == name
                or mem.get("card_or_nickname") == name
            ):
                return int(mem["user_id"]), None
        return None, name

    @plugin_main(call_word=["Eason persona"], require_db=True)
    async def main(self, event: GroupMessageEvent, debug: bool):
        target_id, unresolved_name = self._resolve_target(event)

        if target_id is None:
            if unresolved_name:
                tip = f"在群里没找到名为「{unresolved_name}」的成员，请用群名片/昵称或直接 @"
            else:
                tip = "请 @ 一个你感兴趣的人，例如：Eason persona @小明"
            self.api.groupService.send_group_msg(
                group_id=event.group_id, message=f"{At(qq=event.user_id)} {tip}"
            )
            return

        confirm_msg = "正在翻阅TA的群聊历史并生成人格画像，请稍等~"
        self.api.groupService.send_group_msg(group_id=event.group_id, message=confirm_msg)

        try:
            async with self.session_factory() as session:
                # 拉取该用户最近的发言（跨所有已记录的群），用于统计活跃群
                stmt = (
                    select(Message)
                    .where(Message.user_id == target_id)
                    .order_by(desc(Message.id))
                    .limit(MAX_MESSAGES)
                )
                result = await session.execute(stmt)
                rows = list(result.scalars().all())

                if not rows:
                    self.api.groupService.send_group_msg(
                        group_id=event.group_id,
                        message=f"{At(qq=event.user_id)} 数据库中暂无 [QQ:{target_id}] 的发言记录，无法生成人格画像",
                    )
                    return

            # 消息按 id 升序（时间正序），并统计每个群的发言量
            rows = sorted(rows, key=lambda r: r.id)
            group_count: dict[int, int] = defaultdict(int)
            for row in rows:
                group_count[row.group_id] += 1

            # 按活跃度把群打成匿名标签（群A/群B...），不披露真实群名或群号
            ordered_groups = sorted(group_count.items(), key=lambda kv: kv[1], reverse=True)
            group_label: dict[int, str] = {
                gid: "群" + chr(ord("A") + i) for i, (gid, _) in enumerate(ordered_groups)
            }

            # 组装正序 transcript，控制在字符预算内（超出则只保留最近的）
            transcript_lines = []
            used = 0
            nickname = ""
            for row in reversed(rows):
                text = self._strip_cq(row.msg)
                if not text:
                    continue
                line = f"[{group_label[row.group_id]}] {row.user_nickname}({row.user_id}): {text}"
                used += len(line) + 1
                if used > CHAR_BUDGET:
                    break
                transcript_lines.append(line)
            transcript = "\n".join(reversed(transcript_lines))
            if rows and not nickname:
                nickname = rows[-1].user_nickname

            # 活跃群排行：只用匿名标签，不含真实群名/群号
            active_groups_desc = "、".join(
                f"{group_label[gid]}（{cnt}条）" for gid, cnt in ordered_groups
            )

            directions_txt = "\n".join(f"- {d}" for d in ANALYSIS_DIRECTIONS)
            system_prompt = (
                "你是一位资深的行为分析专家。下面是一次性给你的一段某位QQ用户在若干群聊中的发言记录摘录，"
                "请你基于这些真实的发言内容，分析并总结这个人的人格画像。\n"
                "要求：\n"
                "1. 只能依据给定材料推断，不要编造材料中没有的信息；信息不足的维度明确说明'暂无法判断'。\n"
                "2. 分点输出，覆盖但不限于以下方向：\n"
                f"{directions_txt}\n"
                "3. 绝不披露任何真实群名、群号或能定位到具体群的细节（含群名片、群成员结构）；"
                "提到活跃群时只用匿名标签（如'群A'、'群B'）。\n"
                "4. 用中文、口语化、有条理地输出，开头不必寒暄，直接给出分析正文。"
            )
            user_prompt = (
                f"以下是用户在 [QQ:{target_id}]（{nickname}）近期的群聊发言摘录，按时间顺序排列。\n\n"
                f"【活跃群聊】{active_groups_desc}\n\n【发言记录】\n{transcript}"
            )
            Log.debug(f"插件{self.name}：已为 {target_id} 组装 {len(rows)} 条消息送审", debug)

            response = await get_llm_response(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                model="deepseek-v4-pro",
                thinking_enabled=False,
            )

            if "[NO REPLY]" in response:
                response = "抱歉，大模型暂时没有返回结果，请稍后再试~"

            self.api.groupService.send_group_msg(
                group_id=event.group_id,
                message=f"【{nickname or ('QQ:' + str(target_id))}的人格画像】\n{response}",
            )
            Log.debug(f"插件{self.name}：已输出 {target_id} 的人格画像", debug)

        except Exception as e:
            Log.error(f"插件{self.name}运行时出错：{e}")
            self.api.groupService.send_group_msg(
                group_id=event.group_id,
                message=f"{At(qq=event.user_id)} 生成人格画像时出错，请稍后再试：{e}",
            )
