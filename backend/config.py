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

# 受控下载会话有效期（秒）：会话创建后需在此时间内完成传输与确认
DOWNLOAD_SESSION_EXPIRE_SECONDS = int(os.getenv('DOWNLOAD_SESSION_EXPIRE_SECONDS', 3600))
# 最近下载历史返回条数
DOWNLOAD_HISTORY_LIMIT = int(os.getenv('DOWNLOAD_HISTORY_LIMIT', 20))

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
