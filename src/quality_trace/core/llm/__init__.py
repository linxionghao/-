"""LLM 适配子包：供应商无关的模型调用与向量化。

阶段 1 从 examples/phase1_rag_demo.py 迁入，对应 docs/architecture.md
第 5 节 LLM 适配层的同步雏形。约束同 core/__init__.py：
API Key 一律由调用方从环境变量读出后作为参数传入，本包不读 os.environ。
"""
