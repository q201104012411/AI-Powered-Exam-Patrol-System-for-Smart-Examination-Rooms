# -*- coding: utf-8 -*-
"""
08_试卷流转与保密室智能监控 测试包

**必须在任何一个测试模块 import config 之前**把数据目录指向同一个临时目录：
config.DATA_ROOT 是 import 期一次性解析的；如果各测试模块各用自己的 mkdtemp，
先被导入的那个决定全局 DATA_ROOT，它一 tearDownModule 删掉目录，
后跑的模块再写附件/报告就会 "No such file or directory"（合并跑时踩到过）。

所以：整个测试会话共用一个临时目录，只在本文件注册 atexit 清理。
（与 02/03 同一做法）
"""
import atexit
import os
import shutil
import tempfile

_SHARED_TMP = tempfile.mkdtemp(prefix="secure_test_")
os.environ["EXAM_SECURE_HOME"] = _SHARED_TMP


def shared_tmp():
    """本会话所有测试模块共用的临时数据目录"""
    return _SHARED_TMP


@atexit.register
def _cleanup():
    shutil.rmtree(_SHARED_TMP, ignore_errors=True)
