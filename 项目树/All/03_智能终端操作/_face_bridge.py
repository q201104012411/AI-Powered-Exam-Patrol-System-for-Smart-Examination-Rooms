# -*- coding: utf-8 -*-
"""
人脸核验桥接脚本（在独立子进程里运行）

为什么单独一个脚本：
    01_人脸识别与身份核对 与本端都有 config.py / models.py，
    同一进程里 import 必然撞名。放到子进程后两边命名空间互不干扰，
    本端只通过 stdout 的最后一行 JSON 拿结果。

用法（一般由 bridge.FaceBridge 调用，不手工执行）：
    python _face_bridge.py --face-dir <01目录> --image <图片> \
        [--candidate C001] [--mode 1:1|1:N] [--operator checker]
输出：最后一行一个 JSON；失败时 ok=false 并给出 reason。
"""
import argparse
import json
import os
import sys
import traceback
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--face-dir", required=True, help="01 模块所在目录")
    ap.add_argument("--image", required=True)
    ap.add_argument("--candidate", default="")
    ap.add_argument("--mode", default="1:1", choices=["1:1", "1:N"])
    ap.add_argument("--operator", default="checker")
    ap.add_argument("--device", default="HANDHELD-01")
    args = ap.parse_args()

    face_dir = str(Path(args.face_dir).resolve())
    if not os.path.isfile(args.image):
        return emit({"ok": False, "reason": "图片不存在：%s" % args.image})
    if not os.path.isdir(face_dir):
        return emit({"ok": False, "reason": "01 模块目录不存在：%s" % face_dir})

    # 只把 01 目录放进 sys.path 首位；本脚本自身不 import 本端任何模块，避免撞名
    sys.path.insert(0, face_dir)
    cwd = os.getcwd()
    try:
        os.chdir(face_dir)                      # 01 的部分模块依赖相对路径
        import config as face_config            # noqa: E402
        import liveness                         # noqa: E402
        from audit import AuditLogger           # noqa: E402
        from face_engine import imread_unicode  # noqa: E402
        from face_index import FaceIndex        # noqa: E402
        from store import CandidateStore        # noqa: E402
        from verifier import FaceVerifier       # noqa: E402

        img = imread_unicode(str(face_config.resolve_photo(args.image)))
        if img is None:
            return emit({"ok": False, "reason": "图片无法解码：%s" % args.image})

        cands = CandidateStore().load()
        idx = FaceIndex()
        try:
            idx.load()
        except Exception as e:                  # 索引不存在/机器变更
            return emit({"ok": False, "reason": "特征索引不可用（需先建档）：%s"
                                             % str(e)[:120]})
        logger = AuditLogger()
        v = FaceVerifier(cands, idx, device_id=args.device, logger=logger,
                         operator=args.operator)
        token = liveness.make_token(args.device, source="file")

        if args.mode == "1:1":
            if args.candidate not in cands:
                return emit({"ok": False,
                             "reason": "考生不存在：%s（现有：%s）"
                                       % (args.candidate, "、".join(sorted(cands))) })
            rec = v.verify_face_first(args.candidate, live_images=[img],
                                      capture_token=token)
        else:
            rec = v.verify_1toN(img, capture_token=token)

        return emit({
            "ok": True,
            "result": rec.result,
            "reason_code": rec.reason_code,
            "reason": rec.reason,
            "score": rec.score,
            "threshold": rec.threshold,
            "candidate_id": rec.candidate_id,
            "candidate_name": (cands.get(rec.candidate_id).name
                               if rec.candidate_id in cands else ""),
            "doc_info": getattr(rec, "id_card_display", "") or "",
            "liveness": rec.liveness,
            "quality_severity": getattr(rec, "quality_severity", ""),
            "source": "01_人脸识别与身份核对",
        })
    except Exception as e:                      # 任何异常都如实回传，不伪装成功
        return emit({"ok": False, "reason": "%s: %s" % (type(e).__name__, str(e)[:200]),
                     "trace": traceback.format_exc()[-800:]})
    finally:
        try:
            os.chdir(cwd)
        except OSError:
            pass


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
