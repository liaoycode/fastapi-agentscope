import logging
import sys
from pathlib import Path
from logging.handlers import TimedRotatingFileHandler

# 创建 logs 目录（如果不存在）
log_dir = Path("logs")
log_dir.mkdir(exist_ok=True)

# 设置日志格式
log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
date_format = "%Y-%m-%d %H:%M:%S"


def getLogger():
    """获取当前模块的 logger"""
    import inspect
    frame = inspect.currentframe().f_back
    module_name = frame.f_globals.get('__name__', 'unknown')
    return logging.getLogger(module_name)


def setup_logger(
        name: str = "app",
        log_level: str = "INFO",
        log_to_file: bool = True,
        log_to_console: bool = True,
        backup_count: int = 30  # 保留最近 30 个文件，超出自动删除
):
    """
    简单的日志设置，按天切割日志文件

    handlers 挂在 root logger 上，__main__、uvicorn、第三方库等所有 logger
    通过 propagation 都能输出到 console / 文件。

    Args:
        name: 保留参数以兼容旧调用，无实际作用
        log_level: 日志级别
        log_to_file: 是否输出到文件
        log_to_console: 是否输出到控制台
        backup_count: 保留最近几天的日志文件，默认 30 天
    """
    root_logger = logging.getLogger()

    if root_logger.handlers:
        return root_logger

    root_logger.setLevel(log_level)

    formatter = logging.Formatter(log_format, date_format)

    if log_to_console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(log_level)
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)

    if log_to_file:
        # 应用日志：每天午夜切割，保留 backup_count 天
        app_log_file = log_dir / "app.log"
        file_handler = TimedRotatingFileHandler(
            app_log_file,
            when="midnight",       # 每天午夜切割
            interval=1,            # 每 1 天切割一次
            backupCount=backup_count,
            encoding="utf-8",
            utc=False              # 使用本地时间
        )
        # 切割后的文件名后缀：app.log.2025-01-01
        file_handler.suffix = "%Y-%m-%d"
        file_handler.setLevel(log_level)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

        # 错误日志：同样按天切割
        error_log_file = log_dir / "error.log"
        error_handler = TimedRotatingFileHandler(
            error_log_file,
            when="midnight",
            interval=1,
            backupCount=backup_count,
            encoding="utf-8",
            utc=False
        )
        error_handler.suffix = "%Y-%m-%d"
        error_handler.setLevel(logging.ERROR)
        error_handler.setFormatter(formatter)
        root_logger.addHandler(error_handler)

    return root_logger