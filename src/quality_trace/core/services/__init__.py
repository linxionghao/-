"""服务层子包（架构 L2）：用例编排、事务边界、降级决策。

约束同 core/__init__.py：不 print、不读环境变量（API Key 由入口层传参）、
不 import argparse。
"""
