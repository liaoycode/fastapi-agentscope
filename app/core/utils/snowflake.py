"""
雪花ID生成器（Snowflake ID Generator）

ID结构（64位）：
- 1位：符号位，始终为0
- 41位：时间戳（毫秒级），可使用约69年
- 10位：机器ID（数据中心ID 5位 + 工作机器ID 5位）
- 12位：序列号（同一毫秒内的递增序列）

特点：
- 趋势递增：基于时间戳生成，整体趋势递增
- 分布式友好：不同机器生成的ID不会重复
- 高性能：不依赖数据库，本地生成
- 无序列化：直接使用整数类型
"""

import threading
import time
from typing import Optional


class SnowflakeIDGenerator:
    """雪花ID生成器（线程安全）"""
    
    # 起始时间戳（2024-01-01 00:00:00）可以根据项目启动时间调整
    EPOCH = 1609459200000  # 2021-01-01
    
    # 各部分位数
    WORKER_ID_BITS = 5      # 工作机器ID位数
    DATACENTER_ID_BITS = 5  # 数据中心ID位数
    SEQUENCE_BITS = 12      # 序列号位数
    
    # 最大值
    MAX_WORKER_ID = -1 ^ (-1 << WORKER_ID_BITS)          # 31
    MAX_DATACENTER_ID = -1 ^ (-1 << DATACENTER_ID_BITS)  # 31
    MAX_SEQUENCE = -1 ^ (-1 << SEQUENCE_BITS)            # 4095
    
    # 位移量
    WORKER_ID_SHIFT = SEQUENCE_BITS                                      # 12
    DATACENTER_ID_SHIFT = SEQUENCE_BITS + WORKER_ID_BITS                # 17
    TIMESTAMP_SHIFT = SEQUENCE_BITS + WORKER_ID_BITS + DATACENTER_ID_BITS  # 22
    
    def __init__(self, datacenter_id: int = 1, worker_id: int = 1):
        """
        初始化雪花ID生成器
        
        Args:
            datacenter_id: 数据中心ID (0-31)
            worker_id: 工作机器ID (0-31)
        """
        if datacenter_id > self.MAX_DATACENTER_ID or datacenter_id < 0:
            raise ValueError(f"datacenter_id必须在0到{self.MAX_DATACENTER_ID}之间")
        
        if worker_id > self.MAX_WORKER_ID or worker_id < 0:
            raise ValueError(f"worker_id必须在0到{self.MAX_WORKER_ID}之间")
        
        self.datacenter_id = datacenter_id
        self.worker_id = worker_id
        self.sequence = 0
        self.last_timestamp = -1
        self.lock = threading.Lock()
    
    def _current_millis(self) -> int:
        """获取当前时间戳（毫秒）"""
        return int(time.time() * 1000)
    
    def _wait_next_millis(self, last_timestamp: int) -> int:
        """等待到下一毫秒"""
        timestamp = self._current_millis()
        while timestamp <= last_timestamp:
            timestamp = self._current_millis()
        return timestamp
    
    def generate_id(self) -> int:
        """
        生成雪花ID
        
        Returns:
            int: 64位整数ID
        """
        with self.lock:
            timestamp = self._current_millis()
            
            # 时钟回拨检测
            if timestamp < self.last_timestamp:
                raise Exception(
                    f"时钟回拨检测: 拒绝生成ID，时间差 {self.last_timestamp - timestamp} 毫秒"
                )
            
            # 同一毫秒内
            if timestamp == self.last_timestamp:
                self.sequence = (self.sequence + 1) & self.MAX_SEQUENCE
                if self.sequence == 0:
                    # 序列号溢出，等待下一毫秒
                    timestamp = self._wait_next_millis(self.last_timestamp)
            else:
                # 新的毫秒，序列号重置为0
                self.sequence = 0
            
            self.last_timestamp = timestamp
            
            # 组装ID
            snowflake_id = (
                ((timestamp - self.EPOCH) << self.TIMESTAMP_SHIFT) |
                (self.datacenter_id << self.DATACENTER_ID_SHIFT) |
                (self.worker_id << self.WORKER_ID_SHIFT) |
                self.sequence
            )
            
            return snowflake_id
    
    def parse_id(self, snowflake_id: int) -> dict:
        """
        解析雪花ID
        
        Args:
            snowflake_id: 雪花ID
            
        Returns:
            dict: 包含时间戳、数据中心ID、工作机器ID、序列号的字典
        """
        timestamp = (snowflake_id >> self.TIMESTAMP_SHIFT) + self.EPOCH
        datacenter_id = (snowflake_id >> self.DATACENTER_ID_SHIFT) & self.MAX_DATACENTER_ID
        worker_id = (snowflake_id >> self.WORKER_ID_SHIFT) & self.MAX_WORKER_ID
        sequence = snowflake_id & self.MAX_SEQUENCE
        
        return {
            "timestamp": timestamp,
            "datetime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp / 1000)),
            "datacenter_id": datacenter_id,
            "worker_id": worker_id,
            "sequence": sequence,
        }


# 全局单例
_snowflake_generator: Optional[SnowflakeIDGenerator] = None


def get_snowflake_generator(datacenter_id: int = 1, worker_id: int = 1) -> SnowflakeIDGenerator:
    """
    获取雪花ID生成器单例
    
    Args:
        datacenter_id: 数据中心ID (0-31)，首次调用时设置
        worker_id: 工作机器ID (0-31)，首次调用时设置
        
    Returns:
        SnowflakeIDGenerator: 雪花ID生成器实例
    """
    global _snowflake_generator
    if _snowflake_generator is None:
        _snowflake_generator = SnowflakeIDGenerator(datacenter_id, worker_id)
    return _snowflake_generator


def generate_snowflake_id() -> int:
    """
    快捷方法：生成雪花ID
    
    Returns:
        int: 64位整数ID
    """
    generator = get_snowflake_generator()
    return generator.generate_id()
