"""应用配置"""
import os

PORT = int(os.getenv('PORT', 8636))
UPLOAD_FOLDER = os.getenv('UPLOAD_FOLDER', '/app/uploads')
DB_FILE = os.getenv('DB_FILE', '/app/data/app.db')
TOKEN_EXPIRE_SECONDS = int(os.getenv('TOKEN_EXPIRE_SECONDS', 300))
RATE_LIMIT_REQUESTS = int(os.getenv('RATE_LIMIT_REQUESTS', 5))
RATE_LIMIT_WINDOW = int(os.getenv('RATE_LIMIT_WINDOW', 60))
MAX_FILE_SIZE = int(os.getenv('MAX_FILE_SIZE', 52428800))  # 50MB

BLOCKED_EXTENSIONS = {'exe', 'sh', 'bat', 'cmd', 'ps1', 'py', 'php', 'jsp', 'cgi', 'pl'}

SHARE_LINK_EXPIRE_HOURS = int(os.getenv('SHARE_LINK_EXPIRE_HOURS', 24))
SHARE_LINK_MAX_DOWNLOADS = int(os.getenv('SHARE_LINK_MAX_DOWNLOADS', 10))

# 下载票（受控下载凭证）有效期：传输中每次续传会滑动续期
DOWNLOAD_TICKET_TTL_SECONDS = int(os.getenv('DOWNLOAD_TICKET_TTL_SECONDS', 1800))
# 分块传输时落盘检查点的最小间隔（秒）
DOWNLOAD_CHECKPOINT_INTERVAL = float(os.getenv('DOWNLOAD_CHECKPOINT_INTERVAL', '0.5'))

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
