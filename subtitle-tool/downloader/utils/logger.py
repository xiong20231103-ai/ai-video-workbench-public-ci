import logging
import sys
from logging.handlers import RotatingFileHandler


class Logger:
    """日志封装类，支持控制台 & 文件日志"""

    def __init__(self, log_file="app.log", log_level=logging.INFO, max_size=5 * 1024 * 1024, backup_count=3):
        """
        初始化日志配置
        :param log_file: 日志文件路径
        :param log_level: 日志级别 (默认 INFO)
        :param max_size: 单个日志文件最大大小（默认 5MB）
        :param backup_count: 备份日志数量
        """
        self.logger = logging.getLogger("TikHubLogger")
        self.logger.setLevel(log_level)  # 设置日志级别

        # ✅ 避免重复添加 `Handler`
        if not self.logger.handlers:
            self._setup_handlers(log_file, max_size, backup_count)

    def _setup_handlers(self, log_file, max_size, backup_count):
        """设置日志输出到控制台 & 滚动文件"""
        formatter = logging.Formatter(
            "%(asctime)s - [%(levelname)s] - (%(filename)s:%(lineno)d) - %(message)s"
        )

        # ✅ 1️⃣ 控制台日志
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        self.logger.addHandler(console_handler)

        # ✅ 2️⃣ 文件日志（支持日志滚动）
        file_handler = RotatingFileHandler(log_file, maxBytes=max_size, backupCount=backup_count, encoding="utf-8")
        file_handler.setFormatter(formatter)
        self.logger.addHandler(file_handler)

    def get_logger(self):
        """获取日志记录器"""
        return self.logger


# ✅ 创建全局日志实例
logger_instance = Logger().get_logger()
