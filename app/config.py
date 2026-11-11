import os
import secrets
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "msghub.db"

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
RATE_LIMIT_PER_MIN = int(os.environ.get("RATE_LIMIT_PER_MIN", "5"))
TOKEN_TTL_SECONDS = int(os.environ.get("TOKEN_TTL_SECONDS", str(7 * 24 * 3600)))

# QQ 官方机器人（不配置则只有网页后台可回复）
QQ_APP_ID = os.environ.get("QQ_APP_ID", "").strip()
QQ_APP_SECRET = os.environ.get("QQ_APP_SECRET", "").strip()
QQ_API_BASE = os.environ.get("QQ_API_BASE", "https://api.bot.qq.com").rstrip("/")
QQ_TOKEN_URL = os.environ.get("QQ_TOKEN_URL", "https://api.bot.qq.com/app/getAppAccessToken")
# 被动回复窗口：官方文档写 msg_id 5 分钟内有效（另一处写 60 分钟），默认按保守值
QQ_PASSIVE_WINDOW = int(os.environ.get("QQ_PASSIVE_WINDOW", "300"))
QQ_PASSIVE_MAX = 4  # 每条用户消息最多被动回复 4 次
QQ_MERGE_DELAY = float(os.environ.get("QQ_MERGE_DELAY", "2"))  # 同一会话几秒内的消息合并转发

# 留言字段长度上限
MAX_NAME = 100
MAX_CONTACT = 200
MAX_CONTENT = 5000


def _load_secret() -> str:
    env = os.environ.get("SECRET_KEY", "").strip()
    if env:
        return env
    f = DATA_DIR / ".secret"
    if f.exists():
        return f.read_text().strip()
    key = secrets.token_hex(32)
    f.write_text(key)
    f.chmod(0o600)
    return key


SECRET_KEY = _load_secret()
