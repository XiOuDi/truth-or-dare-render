"""
真心话大冒险 Telegram Bot
========================
群组游戏 Bot，支持投骰子比点数，点数最大为胜利者，最小为失败者。
"""

import json
import logging
import os
import random
import sys
import time
from collections import defaultdict
import asyncio
import httpx
from aiohttp import web
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.request import HTTPXRequest

# Windows 下强制 UTF-8 输出，避免 print 中 emoji 在 GBK 控制台下崩溃
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ============================================================
# 配置（Render 云部署版）
# ============================================================

def load_env_file(path: str = ".env") -> None:
    """轻量加载 .env 文件到环境变量（零依赖，兼容 BOT_TOKEN=xxx 形式；仅本地调试用）"""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                os.environ.setdefault(key, value)


load_env_file()


def _upstash_url() -> str:
    """Upstash REST 地址：优先 Render 约定的 UPSTASH_REDIS_REST_URL，兼容本地 UPSTASH_URL"""
    return (os.environ.get("UPSTASH_REDIS_REST_URL") or os.environ.get("UPSTASH_URL") or "").rstrip("/")


def _upstash_token() -> str:
    return os.environ.get("UPSTASH_REDIS_REST_TOKEN") or os.environ.get("UPSTASH_TOKEN") or ""


def load_secrets_from_upstash() -> dict:
    """从 Upstash 读取敏感配置（优先级高于环境变量）。

    设计目标：GitHub 仓库中不存放任何密钥。Render 只需配置
    UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN 两个环境变量，
    Bot Token 等敏感值全部存放在 Upstash 的 key 中，启动时自动拉取。
    """
    url, token = _upstash_url(), _upstash_token()
    if not url or not token:
        return {}
    secrets = {}
    mapping = {
        "tod:config:bot_token": "BOT_TOKEN",
        "tod:config:proxy": "PROXY",
        "tod:config:mode": "MODE",
    }
    try:
        import httpx
        headers = {"Authorization": f"Bearer {token}"}
        with httpx.Client(timeout=10.0) as client:
            for key, cfg in mapping.items():
                try:
                    r = client.get(f"{url}/get/{key}", headers=headers)
                    if r.status_code == 200:
                        val = (r.json() or {}).get("result")
                        if val:
                            secrets[cfg] = val
                except Exception:
                    continue
    except Exception:
        pass
    return secrets


_secrets = load_secrets_from_upstash()

# 敏感配置来源：Upstash 优先（Render 部署），环境变量兜底（本地调试）
BOT_TOKEN = _secrets.get("BOT_TOKEN") or os.environ.get("BOT_TOKEN", "")
PROXY = _secrets.get("PROXY") or os.environ.get("PROXY", "")  # 例如 http://127.0.0.1:7890（仅本地需要）

# 运行模式：webhook（Render 自动启用）| polling（本地默认）
IS_RENDER = bool(os.environ.get("RENDER_EXTERNAL_URL"))
MODE = (_secrets.get("MODE") or os.environ.get("MODE", "")).lower()
if not MODE:
    MODE = "webhook" if IS_RENDER else "polling"

# webhook 配置（MODE=webhook 时生效）
# Render 自动注入 RENDER_EXTERNAL_URL（https://xxx.onrender.com）与 PORT，无需手动配置
WEBHOOK_URL = (os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/webhook")
WEBHOOK_LISTEN = os.environ.get("WEBHOOK_LISTEN", "0.0.0.0")
WEBHOOK_PORT = int(os.environ.get("PORT") or os.environ.get("WEBHOOK_PORT") or ("8080" if IS_RENDER else "8081"))
WEBHOOK_CERT = os.environ.get("WEBHOOK_CERT", "")    # 保留兼容（Render 由平台终结 TLS，无需配置）
WEBHOOK_KEY = os.environ.get("WEBHOOK_KEY", "")

# Upstash Redis（游戏数据持久化：tod:game:* / tod:stats:*）
UPSTASH_URL = _upstash_url()
UPSTASH_TOKEN = _upstash_token()
COOLDOWN_SECONDS = 10
GAME_TIMEOUT_SECONDS = 15 * 60  # 15 分钟
MIN_PLAYERS = 2
DICE_MIN = 1
DICE_MAX = 99

# ============================================================
# 日志
# ============================================================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ============================================================
# 游戏状态
# ============================================================

class GameState:
    """管理单个群组的游戏状态"""

    def __init__(self):
        self.players: dict[int, dict] = {}  # user_id -> {name, username}
        self.host_id: int | None = None
        self.is_rolling: bool = False
        self.is_active: bool = False
        self.created_at: float = 0.0
        self.last_activity: float = 0.0
        self.roll_results: dict[int, int] = {}  # user_id -> dice result
        self.has_rolled: set[int] = set()
        self.round_number: int = 0
        self.choice_done: bool = False  # 当前轮是否已由失败者做出选择

    def reset(self):
        self.__init__()

    def to_dict(self) -> dict:
        return {
            "players": {str(k): v for k, v in self.players.items()},
            "host_id": self.host_id,
            "is_rolling": self.is_rolling,
            "is_active": self.is_active,
            "created_at": self.created_at,
            "last_activity": self.last_activity,
            "roll_results": {str(k): v for k, v in self.roll_results.items()},
            "has_rolled": list(self.has_rolled),
            "round_number": self.round_number,
            "choice_done": self.choice_done,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "GameState":
        g = cls()
        g.players = {int(k): v for k, v in d.get("players", {}).items()}
        g.host_id = d.get("host_id")
        g.is_rolling = d.get("is_rolling", False)
        g.is_active = d.get("is_active", False)
        g.created_at = d.get("created_at", 0)
        g.last_activity = d.get("last_activity", 0)
        g.roll_results = {int(k): v for k, v in d.get("roll_results", {}).items()}
        g.has_rolled = set(d.get("has_rolled", []))
        g.round_number = d.get("round_number", 0)
        g.choice_done = d.get("choice_done", False)
        return g


# (chat_id, thread_id) -> GameState；thread_id 用于隔离话题群组（Topics）中的不同话题
games: dict[tuple[int, int], GameState] = {}
# user_id -> last_command_time (用于冷却)
cooldowns: dict[int, float] = defaultdict(float)
# webhook 更新去重（Telegram 重试时避免重复处理）
_seen_update_ids: set[int] = set()


def get_thread_id(update: Update) -> int:
    """获取消息所在话题线程 ID；非话题群组（或私聊）返回 0"""
    msg = update.effective_message
    if msg is None:
        return 0
    return getattr(msg, "message_thread_id", None) or 0


def get_game(chat_id: int, thread_id: int = 0) -> GameState:
    key = (chat_id, thread_id)
    if key not in games:
        games[key] = GameState()
    return games[key]


def check_cooldown(user_id: int) -> tuple[bool, float]:
    """检查冷却，返回 (是否可用, 剩余秒数)"""
    now = time.time()
    elapsed = now - cooldowns[user_id]
    if elapsed < COOLDOWN_SECONDS:
        return False, COOLDOWN_SECONDS - elapsed
    return True, 0.0


def set_cooldown(user_id: int):
    cooldowns[user_id] = time.time()


# ============================================================
# Upstash Redis 持久化
# ============================================================

GAME_TTL = 86400  # 游戏状态 24 小时过期（崩溃后不留僵尸游戏）

_redis_client: httpx.AsyncClient | None = None


def _redis_available() -> bool:
    return bool(UPSTASH_URL and UPSTASH_TOKEN)


async def _redis():
    global _redis_client
    if _redis_client is None:
        _redis_client = httpx.AsyncClient(
            proxy=PROXY or None, timeout=10.0,
            headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"},
        )
    return _redis_client


async def redis_call(*args) -> object:
    """执行一条 Redis 命令（POST JSON 数组），失败返回 None"""
    if not _redis_available():
        return None
    try:
        client = await _redis()
        r = await client.post(UPSTASH_URL, json=list(args))
        data = r.json()
        return data.get("result")
    except Exception as e:
        logger.warning("Redis 操作失败 %s: %s", args[0], e)
        return None


def game_key(chat_id: int, thread_id: int) -> str:
    return f"tod:game:{chat_id}:{thread_id}"


def stats_key(chat_id: int, user_id: int) -> str:
    return f"tod:stats:{chat_id}:{user_id}"


async def save_game(chat_id: int, thread_id: int, game: GameState):
    """保存游戏状态到 Redis"""
    if not game.is_active:
        await redis_call("DEL", game_key(chat_id, thread_id))
        return
    await redis_call(
        "SET", game_key(chat_id, thread_id),
        json.dumps(game.to_dict(), ensure_ascii=False),
        "EX", str(GAME_TTL),
    )


async def load_all_games():
    """启动时从 Redis 恢复所有游戏状态"""
    if not _redis_available():
        return 0
    keys = await redis_call("KEYS", "tod:game:*")
    if not keys:
        return 0
    count = 0
    for key in keys:
        try:
            _, _, chat_s, thread_s = key.split(":")
            chat_id, thread_id = int(chat_s), int(thread_s)
            val = await redis_call("GET", key)
            if val:
                g = GameState.from_dict(json.loads(val))
                # 恢复后重置滚动锁，避免崩溃时卡在 rolling
                g.is_rolling = False
                # 超时检查：超过 15 分钟无活动则丢弃
                if time.time() - g.last_activity > GAME_TIMEOUT_SECONDS:
                    await redis_call("DEL", key)
                    continue
                games[(chat_id, thread_id)] = g
                count += 1
        except Exception as e:
            logger.warning("恢复游戏 %s 失败: %s", key, e)
    return count


async def update_stats(chat_id: int, user_id: int, name: str, username: str,
                       win=False, lose=False, truth=False, dare=False):
    """更新玩家统计（Redis hash）"""
    key = stats_key(chat_id, user_id)
    args = ["HSET", key, "name", name[:64]]
    if username:
        args.extend(["username", username[:64]])
    await redis_call(*args)
    if win:
        await redis_call("HINCRBY", key, "wins", "1")
    if lose:
        await redis_call("HINCRBY", key, "losses", "1")
    if win or lose:
        await redis_call("HINCRBY", key, "rounds", "1")
    if truth:
        await redis_call("HINCRBY", key, "truth", "1")
    if dare:
        await redis_call("HINCRBY", key, "dare", "1")


async def get_stats(chat_id: int, user_id: int) -> dict:
    """读取玩家统计"""
    result = await redis_call("HGETALL", stats_key(chat_id, user_id))
    if not result:
        return {}
    return {result[i]: result[i + 1] for i in range(0, len(result), 2)}


async def get_chat_stats(chat_id: int, limit: int = 10) -> list:
    """读取群排行榜"""
    keys = await redis_call("KEYS", f"tod:stats:{chat_id}:*")
    if not keys:
        return []
    rows = []
    for key in keys:
        result = await redis_call("HGETALL", key)
        if not result:
            continue
        d = {result[i]: result[i + 1] for i in range(0, len(result), 2)}
        d["_key"] = key
        rows.append(d)
    rows.sort(key=lambda x: int(x.get("wins", 0)), reverse=True)
    return rows[:limit]


def get_display_name(user) -> str:
    """获取用户显示名称"""
    name = user.first_name or ""
    if user.last_name:
        name += f" {user.last_name}"
    return name.strip() or user.username or str(user.id)


# ============================================================
# 辅助函数
# ============================================================

async def is_group_chat(update: Update) -> bool:
    """检查是否在群组中"""
    return update.effective_chat.type in ("group", "supergroup")


async def send_reply(update: Update, text: str, reply_markup=None, **kwargs):
    """发送回复消息"""
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=reply_markup, **kwargs
    )


async def check_game_timeout(context: ContextTypes.DEFAULT_TYPE):
    """定时检查游戏超时"""
    now = time.time()
    expired = []
    for key, game in games.items():
        if game.is_active and now - game.last_activity > GAME_TIMEOUT_SECONDS:
            expired.append(key)

    for (chat_id, thread_id) in expired:
        games[(chat_id, thread_id)].reset()
        try:
            kwargs = {}
            if thread_id:
                kwargs["message_thread_id"] = thread_id
            await context.bot.send_message(
                chat_id,
                "⏰ 游戏已超过15分钟无操作，自动结束。\n"
                "使用 /createnewgame 开始新游戏！",
                parse_mode=ParseMode.HTML,
                **kwargs,
            )
        except Exception:
            pass


# ============================================================
# 命令处理
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """私聊 - 显示欢迎信息"""
    if await is_group_chat(update):
        await send_reply(
            update,
            "👋 在群组中请使用以下指令：\n"
            "/createnewgame - 创建新游戏\n"
            "/join - 加入游戏\n"
            "/roll - 投骰子\n"
            "/help - 查看帮助",
        )
        return

    text = (
        "🎮 <b>真心话大冒险 Bot</b> 欢欢迎您！\n\n"
        "📖 <b>游戏规则：</b>\n"
        "• 所有玩家投骰子比点数（1-99）\n"
        "• 点数最大的为胜利者\n"
        "• 点数最小的为失败者\n\n"
        "🎯 <b>游戏内容：</b>\n"
        "• 真心话：失败者必须如实回答胜利者的问题\n"
        "• 大冒险：失败者必须做胜利者要求的事情\n"
        "• 如果要求过于苛刻，失败者有拒绝的权利\n\n"
        "📱 <b>使用方法：</b>\n"
        "1. 将我添加到Telegram群组中\n"
        "2. 在群组中发送 /createnewgame 开始新游戏\n"
        "3. 其他玩家使用 /join 加入游戏\n"
        "4. 主持人使用 /roll 开始投骰子\n\n"
        "💡 <b>在私聊中，您可以使用：</b>\n"
        "/start - 查看此说明\n"
        "/help - 查看详细帮助\n\n"
        "🎯 现在就去群组中开始游戏吧！"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """显示帮助信息"""
    text = (
        "📚 <b>真心话大冒险 Bot 帮助</b>\n\n"
        "🎮 <b>游戏指令：</b>\n"
        "/createnewgame - 创建新游戏（群组专用）\n"
        "/join - 加入游戏（群组专用）\n"
        "/leave - 离开游戏（群组专用）\n"
        "/roll - 开始投骰子（主持人专用）\n"
        "/players - 查看玩家列表（群组专用）\n"
        "/stop - 结束游戏（主持人专用）\n"
        "/votestop - 投票结束游戏（群组专用）\n"
        "/kick - 踢出玩家（主持人专用）\n"
        "/trans - 转让主持人（主持人专用）\n"
        "/adminstop - 强制结束游戏（管理员专用）\n"
        "/admintrans - 强制转让主持人（管理员专用）\n"
        "/votetrans - 投票转让主持人（群组专用）\n\n"
        "💡 <b>其他指令：</b>\n"
        "/start - 查看游戏说明\n"
        "/help - 查看此帮助\n\n"
        "⚠️ <b>注意：</b>\n"
        "• 游戏指令只能在群组中使用\n"
        "• 每个指令有10秒冷却时间\n"
        "• 游戏15分钟无操作自动结束\n"
        f"• 最少需要{MIN_PLAYERS}人才能开始游戏\n"
        f"• 点数为{DICE_MIN}-{DICE_MAX}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def create_new_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """创建新游戏"""
    if not await is_group_chat(update):
        await send_reply(update, "❌ 此指令只能在群组中使用！")
        return

    user = update.effective_user
    chat_id = update.effective_chat.id
    thread_id = get_thread_id(update)

    ok, remaining = check_cooldown(user.id)
    if not ok:
        await send_reply(update, f"⏳ 冷却中，请等待 {remaining:.0f} 秒")
        return

    game = get_game(chat_id, thread_id)

    if game.is_active:
        await send_reply(
            update,
            "⚠️ 当前已有进行中的游戏！\n"
            "使用 /stop 结束当前游戏后再创建新游戏。",
        )
        return

    # 初始化新游戏
    game.reset()
    game.is_active = True
    game.host_id = user.id
    game.created_at = time.time()
    game.last_activity = time.time()
    game.players[user.id] = {"name": get_display_name(user), "username": user.username}

    set_cooldown(user.id)

    host_name = get_display_name(user)
    await send_reply(
        update,
        f"🎮 <b>新游戏已创建！</b>\n\n"
        f"👑 主持人：{host_name}\n\n"
        f"📢 使用 /join 加入游戏\n"
        f"🎲 使用 /roll 开始投骰子（至少需要 {MIN_PLAYERS} 人）",
    )


async def join_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """加入游戏"""
    if not await is_group_chat(update):
        await send_reply(update, "❌ 此指令只能在群组中使用！")
        return

    user = update.effective_user
    chat_id = update.effective_chat.id
    thread_id = get_thread_id(update)

    ok, remaining = check_cooldown(user.id)
    if not ok:
        await send_reply(update, f"⏳ 冷却中，请等待 {remaining:.0f} 秒")
        return

    game = get_game(chat_id, thread_id)

    if not game.is_active:
        await send_reply(update, "❌ 当前没有进行中的游戏！\n使用 /createnewgame 创建新游戏。")
        return

    if user.id in game.players:
        await send_reply(update, "⚠️ 你已经在游戏中了！")
        return

    if game.is_rolling:
        await send_reply(update, "⚠️ 游戏已开始投骰子，无法加入！")
        return

    game.players[user.id] = {"name": get_display_name(user), "username": user.username}
    game.last_activity = time.time()
    set_cooldown(user.id)

    player_name = get_display_name(user)
    player_count = len(game.players)
    await send_reply(
        update,
        f"✅ <b>{player_name}</b> 加入了游戏！\n"
        f"👥 当前玩家数：{player_count}",
    )


async def leave_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """离开游戏"""
    if not await is_group_chat(update):
        await send_reply(update, "❌ 此指令只能在群组中使用！")
        return

    user = update.effective_user
    chat_id = update.effective_chat.id
    thread_id = get_thread_id(update)

    ok, remaining = check_cooldown(user.id)
    if not ok:
        await send_reply(update, f"⏳ 冷却中，请等待 {remaining:.0f} 秒")
        return

    game = get_game(chat_id, thread_id)

    if not game.is_active:
        await send_reply(update, "❌ 当前没有进行中的游戏！")
        return

    if user.id not in game.players:
        await send_reply(update, "⚠️ 你不在游戏中！")
        return

    # 主持人离开 -> 结束游戏
    if user.id == game.host_id:
        game.reset()
        set_cooldown(user.id)
        await send_reply(update, "👑 主持人离开了游戏，游戏已结束！")
        return

    del game.players[user.id]
    game.last_activity = time.time()
    set_cooldown(user.id)

    player_name = get_display_name(user)
    player_count = len(game.players)
    await send_reply(
        update,
        f"👋 <b>{player_name}</b> 离开了游戏\n"
        f"👥 当前玩家数：{player_count}",
    )


async def list_players(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看玩家列表"""
    if not await is_group_chat(update):
        await send_reply(update, "❌ 此指令只能在群组中使用！")
        return

    chat_id = update.effective_chat.id
    thread_id = get_thread_id(update)
    game = get_game(chat_id, thread_id)

    if not game.is_active:
        await send_reply(update, "❌ 当前没有进行中的游戏！")
        return

    if not game.players:
        await send_reply(update, "👥 当前没有玩家。")
        return

    lines = [f"👥 <b>玩家列表</b>（共 {len(game.players)} 人）\n"]
    for uid, info in game.players.items():
        tag = "👑" if uid == game.host_id else "•"
        name = info["name"]
        username = info.get("username")
        if username:
            lines.append(f"{tag} <a href='tg://user?id={uid}'>{name}</a>")
        else:
            lines.append(f"{tag} <a href='tg://user?id={uid}'>{name}</a>")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def roll_dice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """投骰子"""
    if not await is_group_chat(update):
        await send_reply(update, "❌ 此指令只能在群组中使用！")
        return

    user = update.effective_user
    chat_id = update.effective_chat.id
    thread_id = get_thread_id(update)

    ok, remaining = check_cooldown(user.id)
    if not ok:
        await send_reply(update, f"⏳ 冷却中，请等待 {remaining:.0f} 秒")
        return

    game = get_game(chat_id, thread_id)

    if not game.is_active:
        await send_reply(update, "❌ 当前没有进行中的游戏！\n使用 /createnewgame 创建新游戏。")
        return

    # 主持人或上一轮获胜者可以发起投骰子
    can_roll = user.id == game.host_id
    if game.roll_results:
        _prev_max = max(game.roll_results.values())
        can_roll = can_roll or any(
            uid == user.id for uid, val in game.roll_results.items() if val == _prev_max
        )
    if not can_roll:
        await send_reply(update, "❌ 只有主持人或上一轮胜利者可以发起投骰子！")
        return

    if len(game.players) < MIN_PLAYERS:
        await send_reply(
            update,
            f"⚠️ 玩家不足！至少需要 {MIN_PLAYERS} 人。\n"
            f"当前玩家数：{len(game.players)}\n"
            f"📢 使用 /join 加入游戏",
        )
        return

    if game.is_rolling:
        await send_reply(update, "⚠️ 投骰子已经在进行中！")
        return

    game.is_rolling = True
    game.last_activity = time.time()
    game.round_number += 1
    game.choice_done = False  # 新轮重置选择状态
    set_cooldown(user.id)

    # 为每个玩家生成骰子结果
    game.roll_results.clear()
    game.has_rolled.clear()

    results = []
    for uid, info in game.players.items():
        dice = random.randint(DICE_MIN, DICE_MAX)
        game.roll_results[uid] = dice

    # 找出最大和最小
    max_val = max(game.roll_results.values())
    min_val = min(game.roll_results.values())

    # 找出胜利者和失败者（可能多人并列）
    winners = [uid for uid, val in game.roll_results.items() if val == max_val]
    losers = [uid for uid, val in game.roll_results.items() if val == min_val]

    # 生成结果文本
    lines = [
        f"🎲 <b>第 {game.round_number} 轮投骰子结果</b>\n",
    ]

    # 按分数排序显示
    sorted_results = sorted(game.roll_results.items(), key=lambda x: x[1], reverse=True)
    for uid, val in sorted_results:
        info = game.players[uid]
        name = info["name"]
        if uid in winners:
            emoji = "🏆"
        elif uid in losers:
            emoji = "💀"
        else:
            emoji = "🎲"
        lines.append(f"  {emoji} {name}：<b>{val}</b>")

    lines.append("")

    # 显示胜利者和失败者
    winner_names = ", ".join(
        f"<a href='tg://user?id={uid}'>{game.players[uid]['name']}</a>"
        for uid in winners
    )
    loser_names = ", ".join(
        f"<a href='tg://user?id={uid}'>{game.players[uid]['name']}</a>"
        for uid in losers
    )

    lines.append(f"🏆 <b>胜利者：</b>{winner_names}")
    lines.append(f"💀 <b>失败者：</b>{loser_names}")
    lines.append("")
    lines.append("🎯 <b>请选择（仅失败者可点击）：</b>")
    lines.append("🔄 下一轮：主持人或胜利者发送 /roll 开始新一轮")

    # 创建内联按钮：真心话 / 大冒险
    keyboard = [
        [
            InlineKeyboardButton("💬 真心话", callback_data=f"truth_{game.round_number}"),
            InlineKeyboardButton("🔥 大冒险", callback_data=f"dare_{game.round_number}"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # 存储当前轮次信息
    game.is_rolling = False

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup,
        disable_web_page_preview=True,
    )


async def handle_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理真心话/大冒险选择"""
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id
    thread_id = query.message.message_thread_id
    user = query.from_user
    game = get_game(chat_id, thread_id)

    if not game.is_active:
        await query.edit_message_text("⚠️ 游戏已结束。")
        return

    data = query.data.split("_")
    choice = data[0]  # "truth" or "dare"
    round_num = int(data[1])

    if round_num != game.round_number:
        await query.answer("⚠️ 这是旧轮次的结果。", show_alert=True)
        return

    # 重新计算胜利者和失败者
    max_val = max(game.roll_results.values())
    min_val = min(game.roll_results.values())
    winners = [uid for uid, val in game.roll_results.items() if val == max_val]
    losers = [uid for uid, val in game.roll_results.items() if val == min_val]

    # 只有失败者可以选择，其他人点击无效
    if user.id not in losers:
        await query.answer("⛔ 只有失败者可以做出选择！", show_alert=True)
        return

    # 本轮已选择后锁定，避免多人并列失败者重复覆盖
    if game.choice_done:
        await query.answer("⚠️ 本轮已做出选择。", show_alert=True)
        return
    game.choice_done = True

    winner_names = ", ".join(
        f"<a href='tg://user?id={uid}'>{game.players[uid]['name']}</a>"
        for uid in winners
    )
    loser_names = ", ".join(
        f"<a href='tg://user?id={uid}'>{game.players[uid]['name']}</a>"
        for uid in losers
    )

    if choice == "truth":
        result_text = (
            f"💬 <b>真心话！</b>\n\n"
            f"🏆 胜利者 {winner_names} 向 💀 失败者 {loser_names} 提问：\n\n"
            f"❓ 失败者必须如实回答！\n"
            f"（如果问题过于私密，失败者有拒绝的权利）"
        )
    else:
        result_text = (
            f"🔥 <b>大冒险！</b>\n\n"
            f"🏆 胜利者 {winner_names} 要求 💀 失败者 {loser_names} 执行：\n\n"
            f"💪 失败者必须完成挑战！\n"
            f"（如果要求过于苛刻，失败者有拒绝的权利）"
        )

    # 移除原消息按钮（保留投骰子结果），另发一条显示选择结果
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(
        result_text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )

    game.last_activity = time.time()


async def stop_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """结束游戏（主持人）"""
    if not await is_group_chat(update):
        await send_reply(update, "❌ 此指令只能在群组中使用！")
        return

    user = update.effective_user
    chat_id = update.effective_chat.id
    thread_id = get_thread_id(update)

    ok, remaining = check_cooldown(user.id)
    if not ok:
        await send_reply(update, f"⏳ 冷却中，请等待 {remaining:.0f} 秒")
        return

    game = get_game(chat_id, thread_id)

    if not game.is_active:
        await send_reply(update, "❌ 当前没有进行中的游戏！")
        return

    if user.id != game.host_id:
        await send_reply(update, "❌ 只有主持人可以结束游戏！\n使用 /votestop 发起投票结束。")
        return

    game.reset()
    set_cooldown(user.id)
    await send_reply(update, "🛑 游戏已结束！\n使用 /createnewgame 开始新游戏。")


async def vote_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """投票结束游戏"""
    if not await is_group_chat(update):
        await send_reply(update, "❌ 此指令只能在群组中使用！")
        return

    user = update.effective_user
    chat_id = update.effective_chat.id
    thread_id = get_thread_id(update)

    ok, remaining = check_cooldown(user.id)
    if not ok:
        await send_reply(update, f"⏳ 冷却中，请等待 {remaining:.0f} 秒")
        return

    game = get_game(chat_id, thread_id)

    if not game.is_active:
        await send_reply(update, "❌ 当前没有进行中的游戏！")
        return

    if user.id not in game.players:
        await send_reply(update, "⚠️ 你不在游戏中！")
        return

    # 计算所需票数（超过半数）
    required_votes = len(game.players) // 2 + 1

    # 使用 bot_data 存储投票
    vote_key = f"votestop_{chat_id}_{thread_id}"
    if vote_key not in context.bot_data:
        context.bot_data[vote_key] = set()

    votes = context.bot_data[vote_key]

    if user.id in votes:
        await send_reply(update, "⚠️ 你已经投过票了！")
        return

    votes.add(user.id)
    set_cooldown(user.id)

    current_votes = len(votes)

    if current_votes >= required_votes:
        # 投票通过，结束游戏
        game.reset()
        del context.bot_data[vote_key]
        await send_reply(
            update,
            f"🗳️ 投票通过（{current_votes}/{required_votes}）\n🛑 游戏已结束！\n"
            f"使用 /createnewgame 开始新游戏。",
        )
    else:
        await send_reply(
            update,
            f"🗳️ {get_display_name(user)} 投票结束游戏\n"
            f"📊 当前票数：{current_votes}/{required_votes}\n"
            f"需要超过半数玩家同意才能结束。",
        )


async def kick_player(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """踢出玩家"""
    if not await is_group_chat(update):
        await send_reply(update, "❌ 此指令只能在群组中使用！")
        return

    user = update.effective_user
    chat_id = update.effective_chat.id
    thread_id = get_thread_id(update)

    ok, remaining = check_cooldown(user.id)
    if not ok:
        await send_reply(update, f"⏳ 冷却中，请等待 {remaining:.0f} 秒")
        return

    game = get_game(chat_id, thread_id)

    if not game.is_active:
        await send_reply(update, "❌ 当前没有进行中的游戏！")
        return

    if user.id != game.host_id:
        await send_reply(update, "❌ 只有主持人可以踢出玩家！")
        return

    # 检查是否回复了某人的消息
    if not update.message.reply_to_message:
        await send_reply(update, "⚠️ 请回复要踢出的玩家的消息来使用此命令。")
        return

    target = update.message.reply_to_message.from_user
    if target.id == user.id:
        await send_reply(update, "❌ 不能踢出自己！")
        return

    if target.id not in game.players:
        await send_reply(update, "⚠️ 该用户不在游戏中！")
        return

    target_name = game.players[target.id]["name"]
    del game.players[target.id]
    game.last_activity = time.time()
    set_cooldown(user.id)

    await send_reply(
        update,
        f"👢 主持人踢出了 <b>{target_name}</b>\n"
        f"👥 当前玩家数：{len(game.players)}",
    )


async def transfer_host(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """转让主持人"""
    if not await is_group_chat(update):
        await send_reply(update, "❌ 此指令只能在群组中使用！")
        return

    user = update.effective_user
    chat_id = update.effective_chat.id
    thread_id = get_thread_id(update)

    ok, remaining = check_cooldown(user.id)
    if not ok:
        await send_reply(update, f"⏳ 冷却中，请等待 {remaining:.0f} 秒")
        return

    game = get_game(chat_id, thread_id)

    if not game.is_active:
        await send_reply(update, "❌ 当前没有进行中的游戏！")
        return

    if user.id != game.host_id:
        await send_reply(update, "❌ 只有主持人可以转让主持人！")
        return

    if not update.message.reply_to_message:
        await send_reply(update, "⚠️ 请回复要转让的玩家的消息来使用此命令。")
        return

    target = update.message.reply_to_message.from_user
    if target.id == user.id:
        await send_reply(update, "❌ 不能转让给自己！")
        return

    if target.id not in game.players:
        await send_reply(update, "⚠️ 该用户不在游戏中！")
        return

    game.host_id = target.id
    game.last_activity = time.time()
    set_cooldown(user.id)

    target_name = game.players[target.id]["name"]
    await send_reply(update, f"👑 主持人已转让给 <b>{target_name}</b>")


async def admin_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """管理员强制结束游戏"""
    if not await is_group_chat(update):
        await send_reply(update, "❌ 此指令只能在群组中使用！")
        return

    user = update.effective_user
    chat_id = update.effective_chat.id
    thread_id = get_thread_id(update)

    # 检查管理员权限
    member = await context.bot.get_chat_member(chat_id, user.id)
    if member.status not in ("administrator", "creator"):
        await send_reply(update, "❌ 此命令仅限群管理员使用！")
        return

    game = get_game(chat_id, thread_id)

    if not game.is_active:
        await send_reply(update, "❌ 当前没有进行中的游戏！")
        return

    game.reset()
    set_cooldown(user.id)
    await send_reply(update, "🛑 管理员强制结束了游戏！\n使用 /createnewgame 开始新游戏。")


async def admin_transfer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """管理员强制转让主持人"""
    if not await is_group_chat(update):
        await send_reply(update, "❌ 此指令只能在群组中使用！")
        return

    user = update.effective_user
    chat_id = update.effective_chat.id
    thread_id = get_thread_id(update)

    # 检查管理员权限
    member = await context.bot.get_chat_member(chat_id, user.id)
    if member.status not in ("administrator", "creator"):
        await send_reply(update, "❌ 此命令仅限群管理员使用！")
        return

    game = get_game(chat_id, thread_id)

    if not game.is_active:
        await send_reply(update, "❌ 当前没有进行中的游戏！")
        return

    if not update.message.reply_to_message:
        await send_reply(update, "⚠️ 请回复要转让的玩家的消息来使用此命令。")
        return

    target = update.message.reply_to_message.from_user
    if target.id not in game.players:
        await send_reply(update, "⚠️ 该用户不在游戏中！")
        return

    game.host_id = target.id
    game.last_activity = time.time()
    set_cooldown(user.id)

    target_name = game.players[target.id]["name"]
    await send_reply(update, f"👑 管理员强制将主持人转让给 <b>{target_name}</b>")


async def vote_transfer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """投票转让主持人"""
    if not await is_group_chat(update):
        await send_reply(update, "❌ 此指令只能在群组中使用！")
        return

    user = update.effective_user
    chat_id = update.effective_chat.id
    thread_id = get_thread_id(update)

    ok, remaining = check_cooldown(user.id)
    if not ok:
        await send_reply(update, f"⏳ 冷却中，请等待 {remaining:.0f} 秒")
        return

    game = get_game(chat_id, thread_id)

    if not game.is_active:
        await send_reply(update, "❌ 当前没有进行中的游戏！")
        return

    if user.id not in game.players:
        await send_reply(update, "⚠️ 你不在游戏中！")
        return

    if not context.args or len(context.args) < 1:
        await send_reply(update, "⚠️ 用法：/votetrans @用户名\n或回复目标用户的消息使用 /votetrans")
        return

    # 查找目标用户
    target = None
    target_name = context.args[0]

    # 先检查是否回复了消息
    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
    else:
        # 按用户名查找
        for uid, info in game.players.items():
            if info.get("username") and f"@{info['username']}" == target_name:
                target_user = type("User", (), {"id": uid, "first_name": info["name"], "username": info["username"]})()
                target = target_user
                break

    if target is None:
        await send_reply(update, "⚠️ 未找到该玩家！请 @用户名 或回复目标用户的消息。")
        return

    if target.id == game.host_id:
        await send_reply(update, "⚠️ 该用户已经是主持人了！")
        return

    if target.id not in game.players:
        await send_reply(update, "⚠️ 该用户不在游戏中！")
        return

    # 计算所需票数
    required_votes = len(game.players) // 2 + 1

    vote_key = f"votetrans_{chat_id}_{thread_id}"
    if vote_key not in context.bot_data:
        context.bot_data[vote_key] = {"target": target.id, "votes": set()}

    vote_data = context.bot_data[vote_key]

    # 如果目标变了，重置投票
    if vote_data["target"] != target.id:
        vote_data = {"target": target.id, "votes": set()}
        context.bot_data[vote_key] = vote_data

    votes = vote_data["votes"]

    if user.id in votes:
        await send_reply(update, "⚠️ 你已经投过票了！")
        return

    votes.add(user.id)
    set_cooldown(user.id)

    current_votes = len(votes)

    if current_votes >= required_votes:
        # 投票通过
        game.host_id = target.id
        game.last_activity = time.time()
        del context.bot_data[vote_key]

        target_display = game.players[target.id]["name"]
        await send_reply(
            update,
            f"🗳️ 投票通过（{current_votes}/{required_votes}）\n"
            f"👑 主持人已转让给 <b>{target_display}</b>",
        )
    else:
        target_display = game.players[target.id]["name"]
        await send_reply(
            update,
            f"🗳️ {get_display_name(user)} 投票转让主持人给 {target_display}\n"
            f"📊 当前票数：{current_votes}/{required_votes}\n"
            f"需要超过半数玩家同意。",
        )


# ============================================================
# 启动时注册命令菜单
# ============================================================

GROUP_COMMANDS = [
    BotCommand("createnewgame", "创建新游戏"),
    BotCommand("join", "加入游戏"),
    BotCommand("leave", "离开游戏"),
    BotCommand("players", "查看玩家列表"),
    BotCommand("roll", "投骰子（主持人）"),
    BotCommand("stop", "结束游戏（主持人）"),
    BotCommand("votestop", "投票结束游戏"),
    BotCommand("kick", "踢出玩家（主持人）"),
    BotCommand("trans", "转让主持人"),
    BotCommand("votetrans", "投票转让主持人"),
    BotCommand("adminstop", "管理员强制结束"),
    BotCommand("admintrans", "管理员强制转让"),
]

PRIVATE_COMMANDS = [
    BotCommand("start", "查看使用说明"),
    BotCommand("help", "查看详细帮助"),
]


async def post_init(application: Application) -> None:
    """启动时向 Telegram 注册群组/私聊命令菜单"""
    try:
        await application.bot.set_my_commands(
            GROUP_COMMANDS, scope=BotCommandScopeAllGroupChats()
        )
        await application.bot.set_my_commands(
            PRIVATE_COMMANDS, scope=BotCommandScopeAllPrivateChats()
        )
        logger.info("命令菜单已注册（群组 %d 条 / 私聊 %d 条）",
                    len(GROUP_COMMANDS), len(PRIVATE_COMMANDS))
    except TelegramError as e:
        logger.warning("命令菜单注册失败（不影响运行）：%s", e)


# ============================================================
# 主函数
# ============================================================

def main():
    """启动 Bot"""
    if not BOT_TOKEN:
        print("=" * 60)
        print("⚠️  未找到 BOT_TOKEN！")
        print("=" * 60)
        print()
        print("1. 从 @BotFather 获取 Bot Token")
        print("2. Render 部署：写入 Upstash 的 tod:config:bot_token（推荐，仓库不含密钥）")
        print("   或设置环境变量 BOT_TOKEN")
        print("3. 本地调试：在项目目录的 .env 文件中填写")
        print('   BOT_TOKEN="123456789:ABCdefGHIjklMNOpqrsTUVwxyz"')
        print()
        print("=" * 60)
        return

    # 创建应用（支持代理访问 Telegram）
    app_builder = Application.builder().token(BOT_TOKEN)
    if PROXY:
        logger.info("使用代理连接 Telegram: %s", PROXY)
        app_builder.request(
            HTTPXRequest(
                proxy=PROXY,
                connect_timeout=20.0,
                read_timeout=20.0,
                write_timeout=20.0,
                pool_timeout=20.0,
            )
        )
    app = app_builder.post_init(post_init).build()

    # 注册命令处理器
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("createnewgame", create_new_game))
    app.add_handler(CommandHandler("join", join_game))
    app.add_handler(CommandHandler("leave", leave_game))
    app.add_handler(CommandHandler("players", list_players))
    app.add_handler(CommandHandler("roll", roll_dice))
    app.add_handler(CommandHandler("stop", stop_game))
    app.add_handler(CommandHandler("votestop", vote_stop))
    app.add_handler(CommandHandler("kick", kick_player))
    app.add_handler(CommandHandler("trans", transfer_host))
    app.add_handler(CommandHandler("adminstop", admin_stop))
    app.add_handler(CommandHandler("admintrans", admin_transfer))
    app.add_handler(CommandHandler("votetrans", vote_transfer))

    # 内联按钮回调
    app.add_handler(CallbackQueryHandler(handle_choice, pattern="^(truth|dare)_"))

    # 定时任务：检查游戏超时
    job_queue = app.job_queue
    if job_queue:
        job_queue.run_repeating(check_game_timeout, interval=60, first=60)

    # 启动
    if MODE == "webhook":
        if not WEBHOOK_URL:
            print("⚠️  WEBHOOK_URL 未配置！Render 会自动注入 RENDER_EXTERNAL_URL，本地调试需在 .env 中设置公网 HTTPS 地址。")
            return
        _base = WEBHOOK_URL.rstrip("/")
        _path = "/" + WEBHOOK_PATH.strip("/")
        WEBHOOK_FULL = _base if _base.endswith(_path) else _base + _path
        print(f"🌐 真心话大冒险 Bot 已启动（Webhook 模式）")
        print(f"   Listen: {WEBHOOK_LISTEN}:{WEBHOOK_PORT}{WEBHOOK_PATH}")
        print(f"   Webhook URL: {WEBHOOK_FULL}")
        print(f"   Token 来源: {'Upstash' if _secrets.get('BOT_TOKEN') else '环境变量'}")

        async def run_server():
            await app.initialize()
            await app.start()

            # 注册 webhook（Render 域名稳定，重试几次即可；drop_pending_updates 避免重启后重放旧更新）
            for attempt in range(1, 6):
                try:
                    ok = await app.bot.set_webhook(
                        WEBHOOK_FULL,
                        allowed_updates=Update.ALL_TYPES,
                        drop_pending_updates=True,
                    )
                    if ok:
                        print("✅ Webhook 注册成功")
                        break
                except Exception as e:
                    print(f"⚠️  setWebhook 失败（第 {attempt} 次）：{e}")
                await asyncio.sleep(3)
            else:
                print("⚠️  setWebhook 多次失败，服务器仍将启动；请检查网络后手动 /setWebhook 或重启")

            # 自定义 aiohttp 服务器：POST /webhook 收更新，GET / 供 Render 健康检查
            server_app = web.Application()

            async def webhook_handler(request):
                try:
                    data = await request.json()
                    update = Update.de_json(data, app.bot)
                    if update and update.update_id:
                        if update.update_id in _seen_update_ids:
                            return web.Response(text="OK")
                        _seen_update_ids.add(update.update_id)
                        if len(_seen_update_ids) > 1000:
                            _seen_update_ids.clear()
                        await app.update_queue.put(update)
                except Exception as e:
                    logger.error("Webhook 处理失败: %s", e)
                return web.Response(text="OK")

            async def health_handler(request):
                return web.Response(text="OK")

            server_app.router.add_post(WEBHOOK_PATH, webhook_handler)
            server_app.router.add_get("/", health_handler)

            runner = web.AppRunner(server_app)
            await runner.setup()
            site = web.TCPSite(runner, WEBHOOK_LISTEN, WEBHOOK_PORT)
            await site.start()
            print("✅ 服务器已启动，等待请求...")
            await asyncio.Event().wait()

        try:
            asyncio.run(run_server())
        except KeyboardInterrupt:
            pass
    else:
        print("🎮 真心话大冒险 Bot 已启动（Polling 模式）！")
        print("按 Ctrl+C 停止")
        app.run_polling(allowed_updates=Update.ALL_TYPES)



if __name__ == "__main__":
    main()
