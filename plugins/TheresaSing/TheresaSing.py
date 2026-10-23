import difflib
import os

from plugins import Plugins, plugin_main
from plugins.A_music.music_api import search_music
from src.event_handler.GroupMessageEventHandler import GroupMessageEvent
from src.PrintLog import Log
from utils.AITools import get_llm_response
from utils.CQType import At

SONGS_SUBDIR = os.path.join("data", "songs")


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

    async def _representative_lyric(self, song_name: str) -> str:
        """让 llm 从知识库给出《陈奕迅·歌名》一句最有代表性的歌词"""
        try:
            resp = await get_llm_response(
                [
                    {
                        "role": "system",
                        "content": (
                            "你是陈奕迅歌曲的资深歌迷。只回答一句陈奕迅歌曲《"
                            + song_name
                            + "》里最有代表性、最经典的一句歌词原文。"
                            "不要解释，不要markdown，不要标点之外的修饰，直接把那句歌词发出来。"
                        ),
                    },
                    {"role": "user", "content": "给一句代表歌词"},
                ],
                model="deepseek-v4-pro",
                thinking_enabled=False,
            )
            if "[NO REPLY]" in resp:
                return ""
            return resp.strip().strip("「」\"'。")
        except Exception:
            return ""

    async def _netease_resolve(self, query: str) -> tuple[str, int | None, str, str, str, str]:
        """本机没有的歌，去网易云判断。

        返回 (kind, song_id, song_name, lyric_line, found_song, found_artist)：
        - kind="music"   → 确认是陈奕迅的歌，转发其网易云；found_* 即该歌
        - kind="suggest" → 不是陈奕迅，found_* 是搜到的真实歌，song_name 给出最可能的陈奕迅歌名，lyric_line 是代表歌词
        - kind="none"    → 没搜到
        """
        results = search_music(query, {}, limit=5)
        eason = next((r for r in results if "陈奕迅" in r["artists"]), None)
        if eason is not None:
            return ("music", eason["id"], eason["name"], "", eason["name"], "陈奕迅")

        found_song = results[0]["name"] if results else ""
        found_artist = results[0]["artists"] if results else ""

        # 搜到的不是陈奕迅的歌：从本地歌单里找一个最接近的歌名作为"最可能"
        names = [
            os.path.splitext(f)[0] for f in os.listdir(self.songs_dir) if f.lower().endswith(".mp3")
        ]
        guesses = difflib.get_close_matches(query, names, n=1, cutoff=0.25)
        if not guesses:
            return ("none", None, None, "", found_song, found_artist)

        guess = guesses[0]
        lyric_line = await self._representative_lyric(guess)
        return ("suggest", None, guess, lyric_line, found_song, found_artist)

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
                (
                    kind,
                    song_id,
                    song_name,
                    lyric_line,
                    found_song,
                    found_artist,
                ) = await self._netease_resolve(query)
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
            elif kind in ("suggest", "none"):
                if found_song:
                    real_info = "这首歌其实是"
                    if found_artist:
                        real_info += f"{found_artist}的《{found_song}》，不是陈奕迅的"
                    else:
                        real_info += f"《{found_song}》，不是陈奕迅的"
                else:
                    real_info = "网抑云上没搜到这首歌"
                if song_name:
                    lyric_note = f"\n代表歌词：{lyric_line}" if lyric_line else ""
                    self.api.groupService.send_group_msg(
                        group_id=event.group_id,
                        message=(
                            f"{At(qq=event.user_id)} 你是不是说错啦？{real_info}。"
                            f"你大概是想要陈奕迅的《{song_name}》？{lyric_note}"
                            f"\n试试：Eason sing {song_name}"
                        ),
                    )
                else:
                    self.api.groupService.send_group_msg(
                        group_id=event.group_id,
                        message=(
                            f"{At(qq=event.user_id)} {real_info}，是不是歌名记错啦？"
                            "（输入 Eason sing 歌单 看看我会唱哪些）"
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
