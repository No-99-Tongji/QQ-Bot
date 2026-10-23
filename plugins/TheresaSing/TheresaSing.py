import difflib
import os
import re

from plugins import Plugins, plugin_main
from plugins.A_music.music_api import lyric_v1, search_music
from src.event_handler.GroupMessageEventHandler import GroupMessageEvent
from src.PrintLog import Log
from utils.CQType import At

SONGS_SUBDIR = os.path.join("data", "songs")

# 歌词里常见但不算"代表歌词"的元信息关键词
LYRIC_META = [
    "作曲",
    "作词",
    "编曲",
    "混音",
    "母带",
    "制作",
    "监制",
    "出品",
    "和声",
    "主唱",
    "录音",
    "录音室",
    "混缩",
    "翻唱",
    "原曲",
    "鼓",
    "贝斯",
    "吉他",
    "键盘",
    "弦乐",
    "钢琴",
]


def _normalize(name: str) -> str:
    """归一化歌名：去空白、转小写、去 .mp3 后缀，用于模糊匹配"""
    return name.replace(".mp3", "").replace(" ", "").replace("　", "").lower().strip()


class TheresaSing(Plugins):
    def __init__(self, server_address, bot):
        super().__init__(server_address, bot)
        self.name = "TheresaSing"
        self.type = "Group"
        self.author = "Eason"
        self.introduction = """
                                让bot用语音唱陈奕迅的歌
                                usage: Eason sing <歌名> / Eason sing 歌单
                            """
        self.init_status()
        # 数据目录（服务器上是软链到 /opt/qq-bot/shared/plugins/TheresaSing/data）
        self.plugin_dir = os.path.dirname(os.path.abspath(__file__))
        self.songs_dir = os.path.join(self.plugin_dir, SONGS_SUBDIR)

    def _available_songs(self) -> dict[str, str]:
        """返回 {归一化名: 完整路径} 的歌单映射"""
        result: dict[str, str] = {}
        if not os.path.isdir(self.songs_dir):
            return result
        for fname in os.listdir(self.songs_dir):
            if fname.lower().endswith(".mp3"):
                result[_normalize(fname)] = os.path.join(self.songs_dir, fname)
        return result

    @staticmethod
    def _representative_line(lrc: str) -> str:
        """从 lrc 歌词里挑一句有实质内容的代表歌词（跳过元信息行/空行/纯时间戳）"""
        for line in lrc.splitlines():
            text = re.sub(r"\[\d{1,2}:\d{1,2}(\.\d+)?\]", "", line).strip()
            if not text or not re.search(r"[一-鿿A-Za-z]", text):
                continue
            if any(k in text for k in LYRIC_META) and len(text) < 14:
                continue
            if len(text) >= 3:
                return text
        return ""

    def _netease_resolve(self, query: str) -> tuple[str, int | None, str, str]:
        """本机没有的歌，去网易云判断。

        返回 (kind, song_id, song_name, lyric_line)：
        - kind="music"   → 确认是陈奕迅的歌，转发其网易云
        - kind="suggest" → 不是陈奕迅，给出最可能的陈奕迅歌名 + 代表歌词
        - kind="none"    → 没搜到
        """
        results = search_music(query, {}, limit=5)
        eason = next((r for r in results if "陈奕迅" in r["artists"]), None)
        if eason is not None:
            return ("music", eason["id"], eason["name"], "")

        # 不是陈奕迅的歌：从本地歌单里找一个最接近的歌名作为"最可能"
        names = [
            os.path.splitext(f)[0] for f in os.listdir(self.songs_dir) if f.lower().endswith(".mp3")
        ]
        guesses = difflib.get_close_matches(query, names, n=1, cutoff=0.25)
        if not guesses:
            return ("none", None, None, "")

        guess = guesses[0]
        lyric_line = ""
        try:
            gres = search_music(f"陈奕迅 {guess}", {}, limit=3)
            gs = next((r for r in gres if "陈奕迅" in r["artists"]), None)
            if gs is not None:
                ly = lyric_v1(gs["id"], {})
                lyric_line = self._representative_line((ly.get("lrc") or {}).get("lyric", ""))
        except Exception:
            lyric_line = ""
        return ("suggest", None, guess, lyric_line)

    @plugin_main(call_word=["Eason sing"])
    async def main(self, event: GroupMessageEvent, debug: bool):
        query = event.message[len("Eason sing") :].strip()

        if not query:
            self.api.groupService.send_group_msg(
                group_id=event.group_id,
                message=f"{At(qq=event.user_id)} 想听哪首？例如：Eason sing 浮夸（输入 Eason sing 歌单 看全部）",
            )
            return

        songs = self._available_songs()

        if query in ("歌单", "空单"):
            if not songs:
                self.api.groupService.send_group_msg(
                    group_id=event.group_id, message="歌单还是空的，法师还没上传歌~"
                )
                return
            # 用真实文件名展示
            display = "、".join(os.path.basename(p) for p in sorted(songs.values()))
            self.api.groupService.send_group_msg(
                group_id=event.group_id, message=f"我会唱的歌单（{len(songs)}首）：\n{display}"
            )
            return

        # 精确/归一化匹配
        norm_query = _normalize(query)
        path = songs.get(norm_query)
        if path is None:
            # 尝试子串匹配，给出最接近的
            candidates = [p for n, p in songs.items() if norm_query in n]
            if candidates:
                path = candidates[0]

        if path is None:
            # 本机没有这首歌：交给网易云处理（是陈奕迅的 → 转发；不是 → 提示说错了）
            try:
                kind, song_id, song_name, lyric_line = self._netease_resolve(query)
            except Exception as e:
                Log.error(f"插件{self.name}查询网易云失败：{e}")
                similar = "、".join(os.path.basename(p) for p in songs.values())
                self.api.groupService.send_group_msg(
                    group_id=event.group_id,
                    message=f"{At(qq=event.user_id)} 还没下载《{query}》，网抑云也暂时连不上。我会唱的歌单：{similar}",
                )
                return

            if kind == "music":
                self.api.groupService.send_group_msg(
                    group_id=event.group_id,
                    message=f"[CQ:music,type=163,id={song_id}] 这首我还没下载，给你推一下网易云的《{song_name}》~",
                )
            elif kind == "suggest":
                lyric_note = f"\n代表歌词：{lyric_line}" if lyric_line else ""
                self.api.groupService.send_group_msg(
                    group_id=event.group_id,
                    message=(
                        f"{At(qq=event.user_id)} 你是不是说错啦？这首好像不是陈奕迅的。"
                        f"陈奕迅应该是《{song_name}》{lyric_note}\n试试：Eason sing {song_name}"
                    ),
                )
            else:
                self.api.groupService.send_group_msg(
                    group_id=event.group_id,
                    message=(
                        f"{At(qq=event.user_id)} 网抑云上没搜到《{query}》相关的陈奕迅歌曲，"
                        "是不是歌名记错啦？（输入 Eason sing 歌单 看看我会唱哪些）"
                    ),
                )
            return

        Log.debug(f"插件{self.name}：群{event.group_id} 点歌《{os.path.basename(path)}》", debug)
        try:
            self.api.groupService.send_group_record_msg(group_id=event.group_id, file_path=path)
            Log.debug(f"插件{self.name}：已发送语音 {path}", debug)
        except Exception as e:
            Log.error(f"插件{self.name}发送语音失败：{e}")
            self.api.groupService.send_group_msg(
                group_id=event.group_id,
                message=f"{At(qq=event.user_id)} 播放《{os.path.basename(path)}》时出了点问题，待会儿再试~",
            )
