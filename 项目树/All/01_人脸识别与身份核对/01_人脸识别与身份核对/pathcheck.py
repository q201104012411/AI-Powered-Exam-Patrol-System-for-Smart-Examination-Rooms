# -*- coding: utf-8 -*-
"""
运行环境路径体检。

为什么需要它：
  本项目的路径是中文（"智能考场AI巡考系统\\项目树\\...\\人脸识别与身份核对"），
  而 Python 生态里不少库对非 ASCII 路径处理不好。已经踩过的坑：
    - cv2.imread() 在中文路径下**静默返回 None**（已用 np.fromfile + cv2.imdecode 规避）；
    - 部分 C/C++ 扩展、临时目录、打包工具对中文/空格路径同样敏感。
  因此把"路径健康度"做成可自检项，而不是等出错再猜。

用法：
    python pathcheck.py            # 打印体检报告
    python enroll.py check-paths   # 同上（统一入口）
"""
import os
import sys
import tempfile

import config


def _is_ascii(s):
    try:
        s.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def analyze(path) -> dict:
    """分析单个路径的健康度"""
    p = os.path.abspath(str(path))
    parts = [x for x in p.split(os.sep) if x]
    longest = max((len(x) for x in parts), default=0)
    issues, advice = [], []

    if not _is_ascii(p):
        issues.append("包含非 ASCII 字符（中文等）")
        advice.append("涉及 C/C++ 扩展的功能可能异常；影像读写请统一用 face_engine.imread_unicode")
    if " " in p:
        issues.append("包含空格")
        advice.append("命令行脚本调用时需正确加引号")
    if len(p) >= 200:
        issues.append("路径长度 %d 字符，接近 Windows 260 限制" % len(p))
        advice.append("建议迁移到更短的根目录，如 C:\\AIExam")
    if len(p) >= 260:
        issues.append("路径长度 %d 字符，已超过传统上限（需开启长路径支持）" % len(p))
    if longest > 40:
        issues.append("存在超长目录名（%d 字符）" % longest)
        advice.append("过长目录名容易在打包/压缩时出问题")

    return {
        "路径": p,
        "长度": len(p),
        "非ASCII": not _is_ascii(p),
        "含空格": " " in p,
        "最长目录名": longest,
        "问题": issues,
        "建议": advice,
    }


def check_runtime_paths() -> dict:
    """检查项目内所有关键路径 + 临时目录"""
    targets = {
        "工程根目录": config.BASE_DIR,
        "数据目录": config.DATA_DIR,
        "日志目录": config.LOG_DIR,
        "参考照目录": config.PHOTO_DIR,
        "抓拍留档目录": config.CAPTURE_DIR,
        "报名库(SQLite)": config.DB_PATH,
        "特征索引": config.INDEX_PATH,
        "审计日志": config.AUDIT_LOG_PATH,
    }
    report = {"检查项": {}, "临时目录": None, "总体": "正常"}
    all_issues = []
    for name, path in targets.items():
        info = analyze(path)
        report["检查项"][name] = info
        all_issues.extend(info["问题"])

    tmp = tempfile.gettempdir()
    report["临时目录"] = {"路径": tmp, "非ASCII": not _is_ascii(tmp),
                          "含空格": " " in tmp}

    report["总体"] = "存在需要关注的问题" if all_issues else "正常"
    return report


def print_report():
    r = check_runtime_paths()
    print("=" * 66)
    print("运行路径体检")
    print("=" * 66)
    for name, info in r["检查项"].items():
        flag = "OK " if not info["问题"] else "!! "
        print("%s%-12s 长度 %3d  %s" % (flag, name, info["长度"], info["路径"]))
        for i in info["问题"]:
            print("      - %s" % i)
    t = r["临时目录"]
    print("%s%-12s %s" % ("OK " if not (t["非ASCII"] or t["含空格"]) else "!! ",
                          "临时目录", t["路径"]))
    suggest = sorted({a for info in r["检查项"].values() for a in info["建议"]})
    if suggest:
        print("-" * 66)
        print("建议：")
        for s in suggest:
            print("  * %s" % s)
    print("-" * 66)
    print("结论：%s" % r["总体"])
    print("=" * 66)
    return r


def ascii_root_suggestion():
    """给出一个可选的短路径方案（供人工决定是否迁移）"""
    return {
        "推荐根目录": "C:\\AIExam",
        "迁移后结构": {
            "代码与文档": "C:\\AIExam\\faceid",
            "数据（含生物特征）": "C:\\AIExam\\data",
            "日志": "C:\\AIExam\\logs",
        },
        "迁移要点": [
            "先停掉网页后端与 GUI，避免 SQLite 句柄占用",
            "整体拷贝（不要剪切），校验后再删除原目录",
            "数据库与加密索引随目录一起搬（DPAPI 密文与机器绑定，换机器需重新建档）",
            "迁移后执行：python enroll.py check-paths && python -m unittest discover -s tests -t .",
        ],
    }


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print_report()
    if len(sys.argv) > 1 and sys.argv[1] == "--suggest":
        s = ascii_root_suggestion()
        print("\n短路径方案（可选）：")
        print("  推荐根目录：%s" % s["推荐根目录"])
        for k, v in s["迁移后结构"].items():
            print("    %-10s %s" % (k, v))
        print("  迁移要点：")
        for x in s["迁移要点"]:
            print("    - %s" % x)
