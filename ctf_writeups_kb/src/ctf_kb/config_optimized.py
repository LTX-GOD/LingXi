"""
兼容旧导入路径，统一复用主配置定义。
"""
from ctf_kb.config import Config, OptimizedConfig, cfg, optimized_cfg

__all__ = ["Config", "OptimizedConfig", "cfg", "optimized_cfg"]
