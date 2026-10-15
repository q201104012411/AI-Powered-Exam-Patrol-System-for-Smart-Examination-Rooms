# -*- coding: utf-8 -*-
"""
演示标记：演示数据生成器使用的常量。

单独成文件的原因：demo_data.py 顶部 import 它，
便于其它模块判断"当前数据目录是否是演示环境"，也便于一键清理演示数据时不误删真实数据。
"""

DEMO = True
DEMO_TAG = "demo"
DEMO_NOTE = "演示数据：由 demo_data.py 生成，不代表真实考试数据"


def is_demo_record(extra):
    """判断一条记录（extra 字典）是否属于演示数据。"""
    return bool(isinstance(extra, dict) and extra.get(DEMO_TAG))
