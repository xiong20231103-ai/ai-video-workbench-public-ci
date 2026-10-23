#!/usr/bin/env python
"""
TikHub Downloader
A GUI application for downloading videos from TikTok, Douyin and other platforms
using the TikHub.io API with synchronous requests.
"""

import os
import platform
import tkinter as tk
import ttkbootstrap as ttk
import logging
import sys

from downloader.app import TikHubDownloaderApp
from downloader.utils.logger import logger_instance
from downloader.config import Config
from downloader.utils.theme_utils import get_theme_name

# 配置日志记录器
logger = logger_instance


def main():
    """Application entry point"""
    # 加载配置
    config = Config()

    # 获取主题设置
    theme_setting = config.get('theme', 'system')
    logger.info(f"Theme setting from config: {theme_setting}")

    # 获取ttkbootstrap主题名
    theme_name = get_theme_name(theme_setting)
    logger.info(f"Using ttkbootstrap theme: {theme_name}")

    # 创建带有指定主题的tkinter根窗口
    root = ttk.Window(themename=theme_name)

    # 设置应用图标
    try:
        resource_root = getattr(sys, "_MEIPASS", os.path.dirname(__file__))
        asset_roots = [
            os.path.join(os.path.dirname(__file__), "downloader", "assets"),
            os.path.join(resource_root, "assets"),
        ]
        asset_root = next((path for path in asset_roots if os.path.isdir(path)), asset_roots[0])
        if platform.system() == "Windows":
            # Windows使用.ico文件
            icon_path = os.path.join(asset_root, "icon.ico")
            try:
                root.iconbitmap(default=icon_path)
            except tk.TclError:
                # Some frozen Tk builds cannot decode ICO at runtime; PNG works reliably.
                png_path = os.path.join(asset_root, "icon.png")
                icon = tk.PhotoImage(file=png_path)
                root.iconphoto(True, icon)
                root._app_icon_reference = icon
        else:
            # Linux/Mac使用.png文件
            icon_path = os.path.join(asset_root, "icon.png")
            icon = tk.PhotoImage(file=icon_path)
            root.iconphoto(True, icon)
    except Exception as e:
        logger.error(f"Error setting application icon: {e}")

    # 初始化并运行应用程序
    app = TikHubDownloaderApp(root)
    app.run()


if __name__ == "__main__":
    main()
