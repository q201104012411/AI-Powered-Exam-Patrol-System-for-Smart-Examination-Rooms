# -*- coding: utf-8 -*-
"""
特征索引：报名库加载时**一次性**提取特征并缓存，核验时只做一次现场检测 + 一次矩阵运算。

旧实现的问题：1:N 抽查对每个考生都重新读图、重新检测、重新提特征（N 次完整推理），
30 人考场需要 6~15 秒，500 人底库需要 100~250 秒，与"1:N ≤ 3 秒"差 1~2 个数量级。

本实现：
- build() 批量建档（可持久化到加密索引文件，替代明文参考照）；
- identify() 用矩阵点积做向量化检索，N 人底库耗时与 N 基本无关（毫秒级）；
- 索引落盘前经 secure_store 加密（Windows DPAPI），不写明文特征。
"""
import io
import json
import os
import time
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

import config
import secure_store
from face_engine import FaceError
from models import RC_IMAGE_ERROR

INDEX_FORMAT = "face-index-v1"


def _default_extractor(img):
    """默认走真实模型（返回 特征 + 画质/姿态信息）；测试可注入假提取器"""
    import face_engine
    return face_engine.extract(img)


class FaceIndex:
    def __init__(self, embedder: Optional[Callable] = None, path=None, resolve_paths=True,
                 extractor: Optional[Callable] = None):
        if extractor is not None:
            self.extractor = extractor
        elif embedder is not None:
            # 注入式假提取器：只有特征，没有画质信息（测试用）
            self.extractor = lambda img: (np.asarray(embedder(img), dtype=np.float32), {})
        else:
            self.extractor = _default_extractor
        self.embedder = lambda img: self.extractor(img)[0]
        self.path = str(path or config.INDEX_PATH)
        self.resolve_paths = resolve_paths     # 相对路径按工程根目录解析（存量数据兼容）
        self.ids: List[str] = []
        self.embeddings: Optional[np.ndarray] = None   # (N, 512) float32
        self.meta: Dict = {}
        self._twin_matrix = None               # N×N 相似度缓存（撞脸检测用）
        # id -> 行号：核验路径上每次都 list.index() 是 O(N) 线性扫描，
        # 几百人底库叠加高频核验会白白吃掉时间，这里用字典做 O(1) 查找。
        self._pos: Dict[str, int] = {}

    def _reindex(self):
        """重建 id -> 行号 映射（ids 变动后调用）"""
        self._pos = {cid: i for i, cid in enumerate(self.ids)}
        return self._pos

    def _row_of(self, candidate_id) -> Optional[int]:
        """O(1) 取某考生特征的行号；不存在返回 None"""
        if not self._pos or len(self._pos) != len(self.ids):
            self._reindex()
        return self._pos.get(candidate_id)

    # ---------------- 状态 ----------------
    @property
    def size(self):
        return 0 if self.embeddings is None else int(self.embeddings.shape[0])

    def is_empty(self):
        return self.size == 0

    def has(self, candidate_id):
        """O(1) 判断索引中是否有该考生"""
        return self._row_of(candidate_id) is not None

    def embedding_of(self, candidate_id):
        i = self._row_of(candidate_id)
        if i is None:
            return None
        return self.embeddings[i]

    # ---------------- 建档 ----------------
    def build(self, candidates: Dict, force=False, progress=None) -> Dict:
        """
        为缺少特征的考生补建特征。已存在且 force=False 的考生会被跳过。
        返回统计：{"built": n, "skipped": n, "failed": {cid: reason}}
        """
        stats = {"built": 0, "skipped": 0, "failed": {}}
        todo = []
        for cid, c in sorted(candidates.items()):
            if self.has(cid) and not force:
                stats["skipped"] += 1
                continue
            todo.append((cid, c))

        for cid, c in todo:
            photo = c.reference_photo
            if not photo:
                stats["failed"][cid] = "未登记参考照"
                continue
            if self.resolve_paths:
                photo = str(config.resolve_photo(photo))   # 兼容相对路径与数据目录搬迁
            try:
                emb = self.embedder(photo)
            except FaceError as e:
                stats["failed"][cid] = "建档失败(%s)" % e.code
                continue
            except Exception as e:                      # 模型缺失等
                stats["failed"][cid] = "建档异常：%s" % str(e)[:80]
                continue
            self._append(cid, emb)
            stats["built"] += 1
            if progress:
                progress(cid, stats["built"], len(todo))

        self.meta = {
            "format": INDEX_FORMAT,
            "model": "buffalo_l/w600k_r50",
            "dim": int(self.embeddings.shape[1]) if not self.is_empty() else 512,
            "size": self.size,
            "created_at": self.meta.get("created_at") or time.strftime("%Y-%m-%dT%H:%M:%S"),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        return stats
    def _append(self, candidate_id, emb):
        emb = np.asarray(emb, dtype=np.float32).reshape(1, -1)
        norm = float(np.linalg.norm(emb))
        if norm > 0:
            emb = emb / norm                            # 统一归一化，点积即余弦
        self._twin_matrix = None                        # 特征变了，撞脸矩阵失效
        if self.embeddings is None:
            self.embeddings = emb
            self.ids = [candidate_id]
            self._reindex()
            return
        i = self._row_of(candidate_id)
        if i is not None:                               # 覆盖更新
            self.embeddings[i] = emb
            return
        self.embeddings = np.vstack([self.embeddings, emb])
        self.ids.append(candidate_id)
        self._pos[candidate_id] = len(self.ids) - 1

    def remove(self, candidate_id):
        i = self._row_of(candidate_id)
        if i is None:
            return False
        self.embeddings = np.delete(self.embeddings, i, axis=0)
        self.ids.pop(i)
        self._twin_matrix = None
        self._reindex()                                 # 行号整体前移
        if self.is_empty():
            self.embeddings = None
        return True

    # ---------------- 持久化（加密） ----------------
    def to_bytes(self) -> bytes:
        buf = io.BytesIO()
        np.savez(buf,
                 ids=np.array(self.ids, dtype=object),
                 embeddings=self.embeddings if self.embeddings is not None
                 else np.zeros((0, 512), dtype=np.float32),
                 meta=np.array([json.dumps(self.meta, ensure_ascii=False)], dtype=object))
        return buf.getvalue()

    def _stamp_meta(self):
        """任何一次保存都写入格式标记与元信息，避免 load() 无法识别版本"""
        self.meta.update({
            "format": INDEX_FORMAT,
            "model": self.meta.get("model") or "buffalo_l/w600k_r50",
            "dim": int(self.embeddings.shape[1]) if self.embeddings is not None else 512,
            "size": self.size,
            "created_at": self.meta.get("created_at") or time.strftime("%Y-%m-%dT%H:%M:%S"),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
        return self.meta

    def save(self, path=None) -> str:
        self._stamp_meta()
        p = str(path or self.path)
        secure_store.write_encrypted(p, self.to_bytes())
        return p

    def load(self, path=None) -> bool:
        p = str(path or self.path)
        if not os.path.exists(p):
            return False
        raw = secure_store.read_encrypted(p)
        with np.load(io.BytesIO(raw), allow_pickle=True) as z:
            self.ids = [str(x) for x in z["ids"].tolist()]
            emb = np.asarray(z["embeddings"], dtype=np.float32)
            self.embeddings = emb if emb.shape[0] else None
            meta = z["meta"].tolist()
            self.meta = json.loads(meta[0]) if meta else {}
        if self.meta.get("format") != INDEX_FORMAT:
            raise secure_store.SecureStoreUnavailable("索引格式不匹配：%s" % self.meta.get("format"))
        self._twin_matrix = None
        self._reindex()
        return True

    # ---------------- 检索与比对 ----------------
    def score(self, candidate_id, img) -> Tuple[float, Dict]:
        """1:1：返回 (原始余弦相似度, info)。特征缺失抛 FaceError。"""
        s, _info = self.score_with_context(candidate_id, img)
        return s, {"index_size": self.size}

    def score_with_context(self, candidate_id, img) -> Tuple[float, Dict]:
        """
        1:1 比对 + **上下文信息**，供"孪生/撞脸"判断使用：
          info["best_other_id"] / info["best_other_score"] —— 现场图像与"除本人外最像的那个人"的相似度。
        只有本人相似度高、且明显高于其他人时，才允许算法单独放行；
        两者差距过小（疑似双胞胎/撞脸）必须转人工复验。
        """
        from models import RC_NOT_FOUND
        idx = self._row_of(candidate_id)
        if idx is None:
            raise FaceError(RC_NOT_FOUND, "索引中无该考生特征，请先执行 enroll.py 建档")

        emb, info = self.extractor(img)
        info = dict(info or {})
        score = float(np.dot(self.embeddings[idx], emb))

        others = [i for i in range(len(self.ids)) if i != idx]
        if others:
            sims = self.embeddings[others] @ emb
            j = int(np.argmax(sims))
            info["best_other_id"] = self.ids[others[j]]
            info["best_other_score"] = float(sims[j])
        else:
            info["best_other_id"] = ""
            info["best_other_score"] = None
        info["index_size"] = self.size
        return score, info

    # ---------------- 多帧融合（提升准确率的关键） ----------------
    @staticmethod
    def frame_weight(info) -> float:
        """按该帧的画质/姿态给融合权重。

        现场连拍的多帧里，有的清晰、有的糊、有的侧脸。等权平均会把糊帧的
        噪声一起揉进模板；这里让"清晰 + 正脸 + 大脸 + 高置信度"的帧说话更响。
        """
        info = info or {}
        rep = info.get("quality")
        w = 1.0
        try:
            w *= max(0.25, min(1.0, float(getattr(rep, "sharpness", 0.0))
                               / max(1.0, float(config.QUALITY_MIN_SHARPNESS) * 4)))
        except (TypeError, ValueError):
            pass
        try:
            det = float(info.get("det_score", 1.0))
            w *= max(0.3, min(1.0, det))
        except (TypeError, ValueError):
            pass
        if rep is not None:
            # 侧脸/歪头/严重画质问题直接降权，而不是丢弃（丢了可能就没有可用帧了）
            sev = getattr(rep, "severity", "ok")
            if sev == "severe":
                w *= 0.35
            elif sev == "warn":
                w *= 0.7
            try:
                yaw = abs(float(getattr(rep, "yaw", 0.0)))
                if yaw > config.POSE_MAX_YAW:
                    w *= 0.6
            except (TypeError, ValueError):
                pass
        if info.get("enhanced"):
            w *= 0.85                      # 增强帧特征可信度略低
        return float(max(0.05, w))

    @staticmethod
    def fuse(embeddings, weights=None):
        """把多帧归一化特征加权平均后重新归一化（模板融合）。

        这是降低"本人被判不通过"最有效的一步：单帧可能因为瞬间模糊/闭眼
        偏离中心，多帧平均后回到真实身份附近，同时**不会**让不同的人更像。
        """
        vecs = [np.asarray(e, dtype=np.float32).ravel() for e in embeddings
                if e is not None]
        if not vecs:
            return None
        if len(vecs) == 1:
            v = vecs[0]
            n = float(np.linalg.norm(v))
            return v / n if n > 0 else v
        m = np.vstack(vecs)
        if weights is None:
            w = np.ones(len(vecs), dtype=np.float32)
        else:
            w = np.asarray(weights, dtype=np.float32).ravel()[:len(vecs)]
            if w.size < len(vecs):
                w = np.concatenate([w, np.ones(len(vecs) - w.size, dtype=np.float32)])
        w = np.maximum(w, 1e-6)
        avg = (m * w[:, None]).sum(axis=0) / float(w.sum())
        n = float(np.linalg.norm(avg))
        return (avg / n) if n > 0 else avg

    def extract_many(self, images, max_frames=5):
        """对多帧提特征，返回 [(emb, info)]，失败帧跳过（不中断整批）"""
        out = []
        for img in list(images or [])[:max_frames]:
            if img is None:
                continue
            try:
                emb, info = self.extractor(img)
            except Exception:
                continue
            if emb is None:
                continue
            out.append((np.asarray(emb, dtype=np.float32), dict(info or {})))
        return out

    def score_multi(self, candidate_id, images, max_frames=5):
        """1:1 多帧融合比对，返回 (score, info)。

        流程：逐帧提特征 -> 按画质加权融合成模板 -> 与库中该考生比对；
        info 里保留参与融合的帧数、单帧最高分、以及"最像的其他人"分数，
        这样核验记录既能给出融合分，也能解释"是整体像还是某一帧像"。
        """
        from models import RC_NOT_FOUND
        idx = self._row_of(candidate_id)
        if idx is None:
            raise FaceError(RC_NOT_FOUND, "索引中无该考生特征，请先执行 enroll.py 建档")

        pairs = self.extract_many(images, max_frames=max_frames)
        if not pairs:
            raise FaceError(RC_IMAGE_ERROR, "多帧融合失败：没有可用于提特征的图像")

        embs = [e for e, _ in pairs]
        weights = [self.frame_weight(i) for _, i in pairs]
        fused = self.fuse(embs, weights)
        score = float(np.dot(self.embeddings[idx], fused))

        single_scores = [float(np.dot(self.embeddings[idx], e)) for e in embs]
        info = dict(pairs[0][1])              # 以第一帧的画质信息作代表
        info["fused_frames"] = len(embs)
        info["fused_weights"] = [round(w, 3) for w in weights]
        info["single_scores"] = [round(s, 4) for s in single_scores]
        info["single_best"] = round(max(single_scores), 4)
        info["single_worst"] = round(min(single_scores), 4)

        others = [i for i in range(len(self.ids)) if i != idx]
        if others:
            sims = self.embeddings[others] @ fused
            j = int(np.argmax(sims))
            info["best_other_id"] = self.ids[others[j]]
            info["best_other_score"] = float(sims[j])
        else:
            info["best_other_id"] = ""
            info["best_other_score"] = None
        info["index_size"] = self.size
        return score, info

    def identify(self, img, topk=3) -> List[Tuple[str, float]]:
        """1:N：一次现场提特征 + 一次矩阵点积，返回 top-k [(candidate_id, 相似度)]"""
        hits, _info = self.identify_with_context(img, topk=topk)
        return hits

    def identify_with_context(self, img, topk=3):
        """1:N 检索 + 画质/姿态信息；返回 (hits, info)"""
        if self.is_empty():
            return [], {}
        emb, info = self.extractor(img)
        info = dict(info or {})
        scores = self.embeddings @ emb                    # 向量化，毫秒级
        k = min(topk, len(self.ids))
        idx = np.argpartition(-scores, k - 1)[:k]
        idx = idx[np.argsort(-scores[idx])]
        hits = [(self.ids[i], float(scores[i])) for i in idx]
        info["index_size"] = self.size
        if len(hits) > 1:
            info["best_other_id"] = hits[1][0]
            info["best_other_score"] = hits[1][1]
        else:
            info["best_other_id"] = ""
            info["best_other_score"] = None
        return hits, info

    def identify_batch(self, images, topk=1) -> List[List[Tuple[str, float]]]:
        """
        边缘多路并发用：一次传入多帧（或多路摄像机同一时刻的帧），
        逐帧提特征 + **共用同一份索引矩阵**做向量化检索。
        说明：检测与对齐仍是逐帧进行的（insightface 的检测器不接受批量输入），
        这里省掉的是"每帧重复加载模型与重复构建索引矩阵"的开销；
        若边缘设备支持批推理，可在此处替换为 recognition.get_feat 的批量调用。
        """
        out = []
        for img in images:
            try:
                hits, _info = self.identify_with_context(img, topk=topk)
            except Exception:
                hits = []
            out.append(hits)
        return out

    def extract_batch(self, images) -> List[Optional[np.ndarray]]:
        """批量提特征（共用已加载模型），失败的位置返回 None 而不是中断整批"""
        feats = []
        for img in images:
            try:
                emb, _info = self.extractor(img)
                feats.append(np.asarray(emb, dtype=np.float32))
            except Exception:
                feats.append(None)
        return feats

    # ---------------- 撞脸 / 双胞胎检测 ----------------
    def similarity_matrix(self, refresh=False):
        """库内两两相似度（N×N），结果缓存"""
        if self.is_empty():
            return None
        if self._twin_matrix is None or refresh:
            self._twin_matrix = self.embeddings @ self.embeddings.T
        return self._twin_matrix

    def twin_pairs(self, threshold=None, topn=50):
        """
        找出库内互相"撞脸"的考生对（相似度 >= threshold）。
        双胞胎、长相极似的同学必须提前识别出来，核验时改用人工复验流程。
        返回 [{"a": id, "b": id, "score": s}]，按相似度降序。
        """
        threshold = config.TWIN_ENROLL_WARN if threshold is None else float(threshold)
        m = self.similarity_matrix()
        if m is None:
            return []
        n = len(self.ids)
        if n < 2:
            return []
        # 向量化取上三角：原来 N×N 的 Python 双层循环在千人底库下要跑几十万次
        iu, ju = np.triu_indices(n, k=1)
        vals = m[iu, ju]
        hit = np.nonzero(vals >= threshold)[0]
        if hit.size == 0:
            return []
        order = hit[np.argsort(-vals[hit])][:topn]
        return [{"a": self.ids[int(iu[k])], "b": self.ids[int(ju[k])],
                 "score": round(float(vals[k]), 4)} for k in order]

    def nearest_other(self, candidate_id):
        """库内与某考生最像的另一人：返回 (id, score)，没有则 ("", None)"""
        i = self._row_of(candidate_id)
        if i is None:
            return "", None
        m = self.similarity_matrix()
        if m is None or len(self.ids) < 2:
            return "", None
        row = m[i].copy()
        row[i] = -1.0
        j = int(np.argmax(row))
        return self.ids[j], float(row[j])
