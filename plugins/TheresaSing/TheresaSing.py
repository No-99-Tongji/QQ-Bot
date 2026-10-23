import os

from plugins import Plugins, plugin_main
from src.event_handler.GroupMessageEventHandler import GroupMessageEvent
from src.PrintLog import Log
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
            similar = "、".join(os.path.basename(p) for p in songs.values())
            self.api.groupService.send_group_msg(
                group_id=event.group_id,
                message=f"{At(qq=event.user_id)} 没找到《{query}》。我会唱的歌单：{similar}",
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
