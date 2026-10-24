# ruff: noqa: E402 — 需先注入仓库根到 sys.path 才能 import 后端模块
"""MCP server：把后端能力（唱歌、人物画像）以工具形式暴露给 opencode。

- sing_song: 点歌/唱歌 —— 本地曲库直接发语音，否则走网易云判断（是陈奕迅→给分享链接，不是→说明真实歌手）
- show_persona: 生成某人的群聊人格画像，返回文字（由 opencode 汇总后发回群里）

设计说明（见 CLAUDE.md）：
- 语音只能通过发送 [CQ:record] 送达，故 sing_song 的本地播放作为副作用直接发到群里；
  其余文字结果一律返回给 opencode 汇总，由 bot 统一发回。
- 画像遵循隐私规则：不披露真实群名/群号，不总结活跃群。
- 通过环境变量取配置；依赖 bot 进程已有的 DPSK_KEY / DB / 网易云 / OneBot 服务。
"""

import asyncio
import json
import os
import random
import re
import sys
import time
from urllib.parse import urljoin

# opencode 起的 MCP 子进程工作目录不一定是仓库根；自行把仓库根加进 sys.path
_HERE = os.path.dirname(os.path.abspath(__file__))  # .../opencode
_ROOT = os.path.dirname(_HERE)  # 仓库根（release 根），含 plugins/、utils/
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import asyncpg
import requests
from mcp.server.fastmcp import FastMCP

from plugins.A_music.music_api import search_music
from utils.AITools import get_llm_response

mcp = FastMCP("qq-bot-bridge")

ONEBOT = os.environ.get("ONEBOT_URL", "http://127.0.0.1:5700")

# 每次工具调用的耗时/结果日志，便于定位慢的工具
TIMING_LOG = os.environ.get("MCP_TIMING_LOG", "/opt/qq-bot/shared/opencode_mcp_timing.log")


def _log_tool(name: str, dur_s: float, ok: bool, **extra) -> None:
    try:
        rec = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "tool": name,
            "dur_s": round(dur_s, 3),
            "ok": ok,
        }
        rec.update(extra)
        with open(TIMING_LOG, "a", encoding="utf8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


# 画像不再总结活跃群，也不披露真实群名/群号
PERSONA_DIRECTIONS = [
    "兴趣爱好",
    "性格特点（外向/内向、乐观/悲观、幽默程度等）",
    "身份/出身（职业、专业、在校学生身份等）",
    "是哪里人（从口音、提到的地名、生活习惯推断）",
    "口头禅（反复出现的高频用语）",
    "常与谁互动、社会关系",
    "作息规律",
    "常用的语气、表情、网络用语",
]


def _songs_dir() -> str:
    d = os.environ.get("SONGS_DIR")
    if d:
        return d
    return "/opt/qq-bot/shared/plugins/TheresaSing/data/songs"


def _now_or_pass() -> str:
    return os.environ.get("DB_PASS") or open("/root/.db_pass").read().strip()


def _db_dsn() -> str:
    user = os.environ.get("DB_USER", "theresa")
    addr = os.environ.get("DB_ADDR", "localhost:5432")
    name = os.environ.get("DB_NAME", "bot")
    return f"postgresql://{user}:{_now_or_pass()}@{addr}/{name}"


def _onebot_post(endpoint: str, payload: dict) -> dict:
    return requests.post(urljoin(ONEBOT, endpoint), json=payload, timeout=20).json()


_IMAGE_PATH_RE = re.compile(r"\[CQ:image[^\]]*?path=([^,\]\s]+)")


def _records_text(msg: str) -> str:
    """把消息里的图片转成 [图:<本地路径>] 标记（供 opencode 直接 read 识图），其余 CQ 去掉"""
    paths = _IMAGE_PATH_RE.findall(msg)
    text = _strip_cq(msg)
    if paths:
        marker = (
            " " + " ".join(f"[图:{p}]" for p in paths)
            if text
            else " ".join(f"[图:{p}]" for p in paths)
        )
        text = text + marker if text else marker
    return text


def _normalize(name: str) -> str:
    return name.replace(".mp3", "").replace(" ", "").replace("　", "").lower().strip()


def _strip_cq(message: str) -> str:
    return re.sub(r"\[CQ:[^\]]*\]", "", message).strip()


# ----------------------------- sing -----------------------------


def _find_local_song(query: str) -> str | None:
    """本地曲库找歌：返回 mp3 完整路径，找不到返回 None"""
    d = _songs_dir()
    if not os.path.isdir(d):
        return None
    mapped = {
        _normalize(f): os.path.join(d, f) for f in os.listdir(d) if f.lower().endswith(".mp3")
    }
    q = _normalize(query)
    path = mapped.get(q)
    if path is None:
        cands = [p for n, p in mapped.items() if q in n]
        if cands:
            path = cands[0]
    return path


def _send_record(group_id: int, path: str) -> None:
    _onebot_post(
        "send_group_msg",
        {"group_id": group_id, "message": [{"type": "record", "data": {"file": f"file://{path}"}}]},
    )


async def _call_tool(name: str, group_id: int, impl, **args) -> str:
    """执行工具实现并记录每次调用耗时/结果到日志"""
    t0 = time.time()
    try:
        r = impl(group_id=group_id, **args)
        if asyncio.iscoroutine(r):
            r = await r
        _log_tool(
            name,
            time.time() - t0,
            True,
            group_id=group_id,
            args={k: v for k, v in args.items()},
            res_head=(str(r)[:60] or ""),
        )
        return r
    except Exception as e:
        _log_tool(
            name,
            time.time() - t0,
            False,
            group_id=group_id,
            args={k: v for k, v in args.items()},
            err=str(e)[:80],
        )
        raise


@mcp.tool()
async def sing_song(group_id: int, song: str) -> str:
    """让 bot 唱歌/点歌。参数 song 是歌名或点歌关键词，group_id 是当前群号。"""
    return await _call_tool("sing_song", group_id, _sing_impl, song=song)


def _sing_impl(group_id: int, song: str) -> str:
    """让 bot 唱歌/点歌。"""
    path = _find_local_song(song)
    if path is not None:
        try:
            _send_record(group_id, path)
        except Exception as e:
            return f"本地点歌失败：{e}"
        return f"已在群里播放《{os.path.basename(path).rsplit('.', 1)[0]}》"

    # 模糊/随意的点歌（"随便唱一首""挑你喜欢的"）→ 随机挑一首本地歌，保证真的放出来
    if any(
        k in song
        for k in ("随便", "任意", "随意", "挑一首", "来一首", "你喜欢", "你喜欢的", "什么歌都行")
    ):
        d = _songs_dir()
        local = [f for f in os.listdir(d) if f.lower().endswith(".mp3")] if os.path.isdir(d) else []
        if local:
            chosen = os.path.join(d, random.choice(local))
            try:
                _send_record(group_id, chosen)
            except Exception as e:
                return f"本地点歌失败：{e}"
            return f"随机唱首《{os.path.basename(chosen).rsplit('.', 1)[0]}》给你~"

    try:
        results = search_music(song, {}, limit=5)
    except Exception as e:
        return f"网易云查询失败：{e}"

    eason = next((r for r in results if "陈奕迅" in r["artists"]), None)
    if eason is not None:
        return f"这首我本地还没有，给你网易云的分享：网易云《{eason['name']}》 https://music.163.com/#/song?id={eason['id']}"
    if results:
        r0 = results[0]
        return (
            f"《{song}》其实是 {r0['artists']} 的《{r0['name']}》，不是陈奕迅的，你确定没记错歌名？"
        )
    return f"网抑云上没搜到《{song}》。"


# ----------------------------- persona -----------------------------


def _resolve_target(group_id: int, target: str) -> tuple[int | None, str | None]:
    """解析画像目标：QQ 号，或按群名片/昵称从该群成员列表解析"""
    if target.isdigit():
        return int(target), None
    try:
        members = (
            _onebot_post("get_group_member_list", {"group_id": group_id, "no_cache": True}).get(
                "data"
            )
            or []
        )
    except Exception:
        members = []
    for mem in members:
        if (
            mem.get("card") == target
            or mem.get("nickname") == target
            or mem.get("card_or_nickname") == target
        ):
            return int(mem["user_id"]), None
    return None, target


async def _fetch_sampled_messages(target_id: int, max_: int = 100) -> list[dict]:
    """取某人的发言：最多 max_ 条，随机抽后按时间正序；带 id/group_id/msg/user_nickname"""
    conn = await asyncpg.connect(_db_dsn())
    try:
        rows = await conn.fetch(
            "SELECT id, group_id, msg, user_nickname FROM messages "
            "WHERE user_id=$1 ORDER BY id DESC LIMIT 400",
            target_id,
        )
    finally:
        await conn.close()
    if not rows:
        return []
    rows = list(rows)
    if len(rows) > max_:
        rows = random.sample(rows, max_)
    rows.sort(key=lambda r: r["id"])
    return rows


async def _persona_text(target_id: int, nickname: str) -> str:
    """查库 + LLM，生成人格画像文字（隐私：不披露群名/活跃群）"""
    rows = await _fetch_sampled_messages(target_id, max_=100)
    if not rows:
        return f"数据库暂无 [QQ:{target_id}] 的发言记录，无法生成画像。"

    group_count: dict[int, int] = {}
    for r in rows:
        group_count[r["group_id"]] = group_count.get(r["group_id"], 0) + 1
    groups = sorted(group_count.items(), key=lambda kv: kv[1], reverse=True)
    label = {gid: "群" + chr(ord("A") + i) for i, (gid, _) in enumerate(groups)}
    if not nickname:
        nickname = rows[-1]["user_nickname"] or f"QQ:{target_id}"

    lines = []
    used = 0
    for r in rows:
        text = _strip_cq(r["msg"])
        if not text or text.lower().startswith("eason"):
            continue
        line = f"[{label[r['group_id']]}] {r['user_nickname']}: {text}"
        used += len(line) + 1
        if used > 12000:
            break
        lines.append(line)
    transcript = "\n".join(lines)

    directions = "\n".join(f"- {d}" for d in PERSONA_DIRECTIONS)
    system = (
        "你是一位资深的行为分析专家。下面是一次性给你的一段某位QQ用户在若干群聊中的发言记录摘录，"
        "请基于这些真实的发言内容，分析并总结这个人的人格画像。\n要求：\n"
        "1. 只能依据给定材料推断，不要编造；信息不足的维度明确说明'暂无法判断'。\n"
        "2. 分点输出，覆盖但不限于：\n" + directions + "\n"
        "3. 绝不披露任何真实群名、群号或能定位到具体群的细节（含群名片、群成员结构、活跃群信息）；不透露的。\n"
        "4. 用中文、口语化、有条理地输出，开头不必寒暄，直接给分析正文。"
    )
    resp = await get_llm_response(
        [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": f"以下是用户在 [QQ:{target_id}]（{nickname}）近期的群聊发言摘录（匿名群标签）：\n\n{transcript}",
            },
        ],
        model="deepseek-flash",
        thinking_enabled=False,
    )
    if "[NO REPLY]" in resp:
        return "大模型暂时没返回，稍后再试。"
    return f"【{nickname}的人格画像】\n{resp}"


@mcp.tool()
async def show_persona(group_id: int, target: str) -> str:
    """生成某人的画像并直接原样发送到群里（避免 opencode 二次概括/改写）。"""
    t0 = time.time()
    try:
        text = await _persona_impl(group_id, target)
        try:
            _onebot_post("send_group_msg", {"group_id": group_id, "message": text})
            res = "画像已原样发送到群里，你无需再复述或概括内容，简短确认即可。"
        except Exception as e:
            res = f"画像已生成但发到群里失败（{e}）。请把下面这段一字不改完整原样发出：\n{text}"
        _log_tool(
            "show_persona",
            time.time() - t0,
            True,
            group_id=group_id,
            args={"target": target},
            res_head=(str(text)[:50] or ""),
            posted=("发到群里" if "失败" not in res else "返回全文"),
        )
        return res
    except Exception as e:
        _log_tool(
            "show_persona",
            time.time() - t0,
            False,
            group_id=group_id,
            args={"target": target},
            err=str(e)[:80],
        )
        raise


async def _persona_impl(group_id: int, target: str) -> str:
    """生成某个用户在群聊里的画像/人格总结。"""
    if not target or not str(target).strip():
        return "请指定要画像的人（QQ号或群名片/昵称）。"
    target = str(target).strip()
    qq, unresolved = _resolve_target(group_id, target)
    if qq is None:
        return f"在当前群没找到名为「{unresolved}」的成员。"
    return await _persona_text(qq, target)


async def _user_messages_impl(group_id: int, target: str, limit: int = 100) -> str:
    """取某人的最近（随机抽取）聊天记录，供补充/确认这个人的信息用"""
    if not target or not str(target).strip():
        return "请指定要查的人（QQ号或群名片/昵称）。"
    target = str(target).strip()
    qq, unresolved = _resolve_target(group_id, target)
    if qq is None:
        return f"在当前群没找到名为「{unresolved}」的成员。"
    limit = max(1, min(int(limit), 200))
    rows = await _fetch_sampled_messages(qq, max_=limit)
    if not rows:
        return f"数据库暂无 [QQ:{qq}] 的发言记录。"
    groups = sorted({r["group_id"] for r in rows})
    label = {gid: "群" + chr(ord("A") + i) for i, gid in enumerate(groups)}
    nickname = rows[-1]["user_nickname"] or f"QQ:{qq}"
    lines = []
    for r in rows:
        text = _records_text(r["msg"])
        if not text:
            continue
        lines.append(f"[{label[r['group_id']]}] {r['user_nickname']}: {text}")
    if not lines:
        return f"[QQ:{qq}]（{nickname}）没有可显示的发言记录。"
    return f"《{nickname}（QQ:{qq}）》最近发言（随机{len(lines)}条，群名已匿名）：\n" + "\n".join(
        lines
    )


@mcp.tool()
async def get_user_messages(group_id: int, target: str, limit: int = 100) -> str:
    """获取某人的聊天记录。用于某些场景需要补充/确认这个人的信息、了解TA说过什么时调用。
    参数 target 是QQ号或群名片/昵称，group_id 是当前群号，limit 是返回条数上限。"""
    return await _call_tool(
        "get_user_messages", group_id, _user_messages_impl, target=target, limit=limit
    )


async def _resolve_target_impl(group_id: int, name: str) -> str:
    """把一个人名/昵称解析成对应的 QQ 号（模糊匹配群名片/昵称 + 聊天记录回退）"""
    name = (name or "").strip()
    if name.isdigit():
        return f"{name}"
    try:
        members = (
            _onebot_post("get_group_member_list", {"group_id": group_id, "no_cache": True}).get(
                "data"
            )
            or []
        )
    except Exception:
        members = []
    ln = name.lower()
    hits: list[tuple[str, int]] = []
    for m in members:
        card = str(m.get("card") or "")
        nn = str(m.get("nickname") or "")
        con = str(m.get("card_or_nickname") or "")
        if any(ln == c.lower() or (ln and ln in c.lower()) for c in (card, nn, con) if c):
            hits.append((card or nn or con, int(m["user_id"])))
    # 去重
    seen: set[int] = set()
    uniq = [h for h in hits if not (h[1] in seen or seen.add(h[1]))]
    if len(uniq) == 1:
        return f"{uniq[0][0]} 的QQ号是 {uniq[0][1]}"
    if len(uniq) > 1:
        return "匹配到多个，请确认：" + "；".join(f"{n}({q})" for n, q in uniq)
    # 回退：从该群聊天记录的用户名/群名片里找
    try:
        conn = await asyncpg.connect(_db_dsn())
        try:
            rws = await conn.fetch(
                "SELECT DISTINCT user_id, user_nickname, user_card FROM messages "
                "WHERE group_id=$1 AND (user_nickname ILIKE '%'||$2||'%' OR user_card ILIKE '%'||$2||'%') LIMIT 10",
                group_id,
                ln,
            )
        finally:
            await conn.close()
        seen2: set[int] = set()
        u2 = []
        for r in rws:
            if r["user_id"] in seen2:
                continue
            seen2.add(r["user_id"])
            u2.append(
                (r["user_card"] or r["user_nickname"] or f"{r['user_id']}", int(r["user_id"]))
            )
        if len(u2) == 1:
            return f"{u2[0][0]} 的QQ号是 {u2[0][1]}"
        if u2:
            return "匹配到多个，请确认：" + "；".join(f"{n}({q})" for n, q in u2)
    except Exception:
        pass
    return f"在当前群和聊天记录里都没找到「{name}」对应的人。"


@mcp.tool()
async def resolve_target(group_id: int, name: str) -> str:
    """把一个名字/昵称解析成对应的QQ号。当用户只用名字指代某人（没at、也没给QQ号）时，
    先用本工具拿到TA的QQ号，再用于 show_persona / get_user_messages。若结果不唯一会列出候选。"""
    return await _call_tool("resolve_target", group_id, _resolve_target_impl, name=name)


if __name__ == "__main__":
    mcp.run()
