from plugins import Plugins, plugin_main
from src.event_handler.NoticeEventHandler import GroupMsgEmojiLikeEvent
from src.PrintLog import Log
from utils.CQType import At


class EmojiLikeBan(Plugins):
    """
    插件名：EmojiLikeBan \n
    插件类型：群聊表情回应禁言插件 \n
    插件功能：禁止特定用户在特定群中对消息回应特定表情，违者禁言\n
    """

    def __init__(self, server_address, bot):
        super().__init__(server_address, bot)
        self.name = "EmojiLikeBan"
        self.type = "GroupMsgEmoji"
        self.author = "Heai"
        self.introduction = """
                                禁止特定用户在特定群中回应特定表情，违者禁言
                                usage: auto
                            """
        self.init_status()

    @plugin_main(check_call_word=False, check_group=False)
    async def main(self, event: GroupMsgEmojiLikeEvent, debug: bool):
        if not event.is_add:
            return

        ban_groups: list = self.config.get("ban_groups", [])
        ban_users: list = self.config.get("ban_users", [])
        ban_emojis: list = self.config.get("ban_emojis", [])
        ban_duration: int = self.config.get("ban_duration", 3600)

        if event.group_id not in ban_groups:
            return
        if event.user_id not in ban_users:
            return

        banned_emoji_set = {str(emoji) for emoji in ban_emojis}
        reacted = {str(like.get("emoji_id")) for like in event.likes}
        if not (reacted & banned_emoji_set):
            return

        self.api.groupService.set_group_ban(
            group_id=event.group_id, user_id=event.user_id, duration=ban_duration
        )
        self.api.groupService.send_group_msg(
            group_id=event.group_id,
            message=f"{At(qq=event.user_id)} 禁止回应该表情，已被禁言",
        )
        Log.debug(
            f"插件：{self.name}将群{event.group_id}的用户{event.user_id}禁言{ban_duration}秒（违规表情回应）",
            debug,
        )
