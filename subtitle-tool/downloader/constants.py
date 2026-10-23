"""
Constants for the TikHub Downloader application
"""

# Application metadata
APP_TITLE = "TikHub AI Subtitle Downloader"
APP_VERSION = "2.0.0-ai"
APP_UPDATE_DATE = "2026-07-10"
APP_COPYRIGHT = "Copyright (c) 2025 TikHub.io Downloader"

# Window settings
DEFAULT_WINDOW_SIZE = "1600x1400"

# About information with emoji and better formatting
ABOUT_TEXT_EN = f"""📱 **TikHub Downloader**
A multi-functional downloader based on TikHub.io API, supporting video downloads from various social media platforms.

ℹ️ **Version Information**
• Version: {APP_VERSION}
• Last updated: {APP_UPDATE_DATE}

🔗 **Official Links**
• Website: https://www.tikhub.io
• API Documentation: https://docs.tikhub.io

✅ **Supported Platforms**
• TikTok
• Douyin
• More coming soon!

© {APP_COPYRIGHT}
"""

ABOUT_TEXT_CN = f"""📱 **TikHub 下载器**
基于 TikHub.io API 的多功能下载工具，支持多个社交媒体平台的视频下载。

ℹ️ **版本信息**
• 版本号: {APP_VERSION}
• 最后更新: {APP_UPDATE_DATE}

🔗 **官方链接**
• 网站: https://www.tikhub.io
• API文档: https://docs.tikhub.io

✅ **支持平台**
• 抖音
• TikTok
• 更多平台即将支持！

© {APP_COPYRIGHT}
"""

# Default to English for backward compatibility
ABOUT_TEXT = ABOUT_TEXT_EN

# HTTP request settings
HTTP_CLIENT_USER_AGENT = f"TikHub Downloader App/{APP_VERSION}-{APP_UPDATE_DATE}"

# HTTP request headers
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0"
DEFAULT_VIDEO_HEADERS = {
    'User-Agent': DEFAULT_USER_AGENT,
    'Accept': "video/webm,video/ogg,video/*;q=0.9,application/ogg;q=0.7,audio/*;q=0.6,*/*;q=0.5",
    'Accept-Encoding': "gzip, deflate, br, zstd, identity",
    'Accept-Language': "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2",
    'Sec-Fetch-Dest': "video",
    'Sec-Fetch-Mode': "cors",
    'Sec-Fetch-Site': "cross-site",
    'Range': "bytes=0-",
    'Priority': "u=4",
    'Pragma': "no-cache",
    'Cache-Control': "no-cache",
    'TE': "trailers"
}

