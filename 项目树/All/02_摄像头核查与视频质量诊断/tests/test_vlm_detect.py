# -*- coding: utf-8 -*-
"""
多模态（VLM）检测引擎单元测试

完全离线：所有 HTTP 调用都用假的 urlopen 顶替，不发真实请求。
覆盖：
  1. 模型返回的 JSON 容错（```json 围栏 / 前后废话 / 裸数组 / 垃圾文本）
  2. 坐标归一（0~1000 相对 / 0~1 小数 / 绝对像素 / {x,y,w,h}）+ 非法框丢弃
  3. 行为与物品标签归一（模型用词 → 本项目词表）
  4. 配置解析（provider 矩阵、环境变量覆盖、不可用原因）
  5. 两个后端的请求体（Ollama images / OpenAI image_url data URI）
  6. Ollama 可用性探测（能识别"装了但不是视觉模型"）
  7. Tracker：provider=off 与基类等价；不可用时回落 YOLO；
     结果新鲜时用多模态的框与行为；物品关联到人；物品框绘制不崩
"""
import base64
import json
import os
import unittest
from unittest import mock

import numpy as np

import person_detect as pd
import vlm_detect as vlm


# ---------------------------------------------------------------------
#  假的 HTTP 响应 / urlopen
# ---------------------------------------------------------------------
class FakeResp(object):
    def __init__(self, payload):
        self._data = payload if isinstance(payload, bytes) \
            else json.dumps(payload).encode("utf-8")

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class UrlopenRecorder(object):
    """记录每次请求（url/headers/body），按 url 关键字返回预置响应"""

    def __init__(self, routes):
        self.routes = routes          # [(url 关键字, payload), ...]
        self.calls = []

    def __call__(self, req, timeout=None):
        url = getattr(req, "full_url", req)
        body = None
        data = getattr(req, "data", None)
        if data:
            body = json.loads(data.decode("utf-8"))
        self.calls.append({"url": url, "body": body,
                           "headers": dict(getattr(req, "headers", {}) or {}),
                           "timeout": timeout})
        for key, payload in self.routes:
            if key in url:
                return FakeResp(payload)
        raise AssertionError("测试没有为这个地址准备响应：%s" % url)


def _clean_env(case):
    """去掉可能干扰的 EXAM_VLM_* 环境变量"""
    for key in list(vlm.ENV_KEYS.values()):
        if key in os.environ:
            case.addCleanup(os.environ.pop, key)


class _Base(unittest.TestCase):
    def setUp(self):
        _clean_env(self)


# ---------------------------------------------------------------------
#  1) JSON 容错
# ---------------------------------------------------------------------
class TestExtractJson(_Base):
    def test_fenced_json(self):
        text = "好的，结果如下：\n```json\n{\"persons\": [], \"objects\": []}\n```\n以上。"
        data, err = vlm.extract_json(text)
        self.assertEqual(err, "")
        self.assertEqual(data, {"persons": [], "objects": []})

    def test_prose_wrapped_object(self):
        text = '判读结果 {"persons":[{"box":[1,2,3,4]}], "summary":"x"} 完毕'
        data, err = vlm.extract_json(text)
        self.assertEqual(err, "")
        self.assertEqual(len(data["persons"]), 1)

    def test_bare_list_becomes_persons(self):
        text = '[{"box": [10, 20, 30, 40], "action": "起立"}]'
        data, err = vlm.extract_json(text)
        self.assertEqual(err, "")
        self.assertEqual(len(data["persons"]), 1)

    def test_garbage_reports_error_and_no_guess(self):
        data, err = vlm.extract_json("我看不出这张图有什么问题")
        self.assertIsNone(data)
        self.assertIn("JSON", err)

    def test_empty(self):
        data, err = vlm.extract_json("")
        self.assertIsNone(data)
        self.assertTrue(err)


# ---------------------------------------------------------------------
#  2) 坐标归一
# ---------------------------------------------------------------------
class TestParseResult(_Base):
    def test_relative_1000(self):
        payload = {"persons": [{"box": [250, 500, 750, 1000], "action": "正常姿态",
                                "confidence": 0.9}]}
        persons, objects = vlm.parse_result(payload, 640, 480)
        self.assertEqual(len(persons), 1)
        self.assertEqual(persons[0]["rect"], (160, 240, 320, 240))
        self.assertEqual(persons[0]["action"], "正常姿态")
        self.assertEqual(objects, [])

    def test_fraction_and_relative_and_pixel(self):
        """坐标三套写法：0~1 小数 / 0~1000 相对（默认契约）/ >1000 绝对像素"""
        payload = {"persons": [
            {"box": [0.1, 0.1, 0.6, 0.6]},                       # 0~1 小数
            {"box": [100, 100, 500, 500]},                        # 0~1000 相对
            {"box": [100, 50, 200, 300]},                         # 仍按相对解释
            {"box": [100, 50, 1500, 900]},                        # 有值 >1000 → 绝对像素
            {"box": {"x": 100, "y": 50, "w": 200, "h": 300}},     # xywh
        ]}
        persons, _ = vlm.parse_result(payload, 640, 480)
        self.assertEqual(len(persons), 5)
        self.assertEqual(persons[0]["rect"], (64, 48, 320, 240))
        self.assertEqual(persons[1]["rect"], (64, 48, 256, 192))
        # 0~1000 相对：x=100/1000*640=64, y=50/1000*480=24, x2=200/1000*640=128,
        # y2=300/1000*480=144 -> (64, 24, 64, 120)
        self.assertEqual(persons[2]["rect"], (64, 24, 64, 120))
        self.assertEqual(persons[3]["rect"], (100, 50, 540, 430))
        self.assertEqual(persons[4]["rect"], (100, 50, 200, 300))

    def test_drops_invalid_boxes(self):
        payload = {"persons": [
            {"box": [0, 0, 0, 0]},            # 退化
            {"box": [5, 5]},                  # 缺坐标
            {"box": "手机"},                   # 类型不对
            {"no_box": True},                 # 没有框
            {"box": [200, 200, 100, 100]},    # x2<x1
        ]}
        persons, _ = vlm.parse_result(payload, 640, 480)
        self.assertEqual(persons, [])

    def test_clamped_to_frame(self):
        payload = {"persons": [{"box": [-50, -50, 1500, 1500]}]}
        persons, _ = vlm.parse_result(payload, 640, 480)
        self.assertEqual(persons[0]["rect"], (0, 0, 640, 480))

    def test_objects_and_labels(self):
        payload = {"objects": [
            {"box": [100, 100, 200, 200], "label": "cell phone", "confidence": 0.7},
            {"box": [300, 300, 400, 400], "label": "小纸条", "confidence": 0.4},
            {"box": [500, 100, 600, 200], "label": "耳机", "confidence": 0.6},
        ]}
        _, objects = vlm.parse_result(payload, 640, 480)
        self.assertEqual([o["label"] for o in objects], ["手机", "小抄", "耳机"])
        self.assertTrue(objects[0]["phone"])

    def test_confidence_filter(self):
        payload = {"persons": [{"box": [1, 1, 100, 100], "confidence": 0.1},
                               {"box": [200, 200, 300, 300], "confidence": 0.9}]}
        persons, _ = vlm.parse_result(payload, 640, 480, min_conf=0.5)
        self.assertEqual(len(persons), 1)

    def test_action_and_object_alias(self):
        self.assertEqual(vlm._norm_action("站立"), "起立/站立")
        self.assertEqual(vlm._norm_action("玩手机"), "使用手机")
        self.assertEqual(vlm._norm_action("turn head"), "转脸偏头")
        self.assertEqual(vlm._norm_action("啃铅笔"), "啃铅笔")     # 不认识的保留原文
        self.assertEqual(vlm._norm_object("earbuds"), "耳机")
        self.assertEqual(vlm._norm_object(""), "可疑物品")


# ---------------------------------------------------------------------
#  3) 配置
# ---------------------------------------------------------------------
class TestConfig(_Base):
    def test_off_by_default(self):
        cfg = vlm.VlmConfig()
        self.assertEqual(cfg.provider, vlm.PROVIDER_OFF)
        self.assertFalse(cfg.enabled)
        self.assertIn("关闭", cfg.missing_reason())

    def test_ollama_needs_model(self):
        cfg = vlm.VlmConfig(provider="ollama")
        self.assertFalse(cfg.enabled)
        self.assertIn("模型名", cfg.missing_reason())
        cfg = vlm.VlmConfig(provider="ollama", model="qwen2.5vl:3b")
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.base_url, vlm.DEFAULT_OLLAMA_URL)

    def test_api_needs_key(self):
        cfg = vlm.VlmConfig(provider="api", model="qwen-vl-max",
                            base_url="https://x/v1")
        self.assertFalse(cfg.enabled)
        self.assertIn("API Key", cfg.missing_reason())
        cfg = vlm.VlmConfig(provider="api", model="qwen-vl-max",
                            base_url="https://x/v1", api_key="sk-test")
        self.assertTrue(cfg.enabled)

    def test_env_overrides_settings(self):
        with mock.patch.dict(os.environ, {"EXAM_VLM_PROVIDER": "ollama",
                                          "EXAM_VLM_MODEL": "minicpm-v",
                                          "EXAM_VLM_INTERVAL": "7.5"}):
            cfg = vlm.VlmConfig(provider="off")
            self.assertEqual(cfg.provider, vlm.PROVIDER_OLLAMA)
            self.assertEqual(cfg.model, "minicpm-v")
            self.assertAlmostEqual(cfg.interval, 7.5)

    def test_settings_dict_roundtrip(self):
        cfg = vlm.VlmConfig.from_settings({"vlm_provider": "ollama",
                                           "vlm_model": "llava:7b",
                                           "vlm_max_dim": 1280})
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.model, "llava:7b")
        self.assertEqual(cfg.max_dim, 1280)

    def test_local_openai_compatible_needs_no_key(self):
        """本机 OpenAI 兼容服务（Ollama / vLLM / LM Studio）本来没 Key，不该逼用户编一个"""
        for url in ("http://127.0.0.1:11434/v1", "http://localhost:8000/v1",
                    "http://0.0.0.0:1234/v1"):
            cfg = vlm.VlmConfig(provider="api", model="qwen2.5vl:3b", base_url=url)
            self.assertTrue(cfg.enabled, url)
            self.assertEqual(cfg.missing_reason(), "")

    def test_remote_api_still_requires_key(self):
        cfg = vlm.VlmConfig(provider="api", model="qwen-vl-max",
                            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")
        self.assertFalse(cfg.enabled)
        self.assertIn("API Key", cfg.missing_reason())

    def test_api_omits_auth_header_when_no_key(self):
        """没填 Key 时不要发空的 Authorization 头（本机服务会因此报错）"""
        rec = UrlopenRecorder([("/chat/completions", {"choices": [
            {"message": {"content": '{"persons": [], "objects": []}'}}]})])
        cfg = vlm.VlmConfig(provider="api", model="m",
                            base_url="http://127.0.0.1:11434/v1")
        client = vlm.OpenAIClient(cfg)
        with mock.patch.object(vlm.urllib.request, "urlopen", rec):
            client.analyze_parsed(np.zeros((64, 64, 3), np.uint8))
        self.assertNotIn("Authorization", rec.calls[0]["headers"])
        self.assertEqual(cfg.api_key, "")

    def test_interval_clamped(self):
        cfg = vlm.VlmConfig(provider="ollama", model="m", interval=0.001)
        self.assertGreaterEqual(cfg.interval, 0.5)


# ---------------------------------------------------------------------
#  4) 后端请求体
# ---------------------------------------------------------------------
class TestClients(_Base):
    def _frame(self):
        return np.full((360, 640, 3), 90, dtype=np.uint8)

    def test_ollama_payload_has_images_and_json_format(self):
        rec = UrlopenRecorder([("/api/chat", {"message": {"content":
                                     '{"persons": [], "objects": []}'}})])
        cfg = vlm.VlmConfig(provider="ollama", model="qwen2.5vl:3b")
        client = vlm.OllamaClient(cfg)
        with mock.patch.object(vlm.urllib.request, "urlopen", rec):
            persons, objects, text, dt = client.analyze_parsed(self._frame())
        self.assertEqual(persons, [])
        body = rec.calls[0]["body"]
        self.assertEqual(body["model"], "qwen2.5vl:3b")
        self.assertFalse(body["stream"])
        self.assertEqual(body["format"], "json")
        images = body["messages"][1]["images"]
        self.assertEqual(len(images), 1)
        raw = base64.b64decode(images[0])
        self.assertTrue(raw[:2] == b"\xff\xd8", "应是 JPEG 字节流")

    def test_ollama_downscales_large_frame(self):
        rec = UrlopenRecorder([("/api/chat", {"message": {"content": "{}"}})])
        cfg = vlm.VlmConfig(provider="ollama", model="m", max_dim=224)
        client = vlm.OllamaClient(cfg)
        big = np.full((1080, 1920, 3), 30, dtype=np.uint8)
        with mock.patch.object(vlm.urllib.request, "urlopen", rec):
            client.analyze_parsed(big)
        import cv2
        img = cv2.imdecode(np.frombuffer(
            base64.b64decode(rec.calls[0]["body"]["messages"][1]["images"][0]),
            dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertLessEqual(max(img.shape[:2]), 224)

    def test_api_payload_uses_data_uri_and_bearer(self):
        rec = UrlopenRecorder([("/chat/completions", {"choices": [
            {"message": {"content": '{"persons": [], "objects": []}'}}]})])
        cfg = vlm.VlmConfig(provider="api", model="qwen-vl-max",
                            base_url="https://example.com/compatible-mode/v1",
                            api_key="sk-abc")
        client = vlm.OpenAIClient(cfg)
        with mock.patch.object(vlm.urllib.request, "urlopen", rec):
            client.analyze_parsed(self._frame())
        call = rec.calls[0]
        self.assertTrue(call["url"].endswith("/v1/chat/completions"))
        self.assertEqual(call["headers"].get("Authorization"), "Bearer sk-abc")
        parts = call["body"]["messages"][1]["content"]
        self.assertEqual(parts[0]["type"], "text")
        self.assertTrue(parts[1]["image_url"]["url"].startswith(
            "data:image/jpeg;base64,"))

    def test_api_no_key_never_calls_out(self):
        cfg = vlm.VlmConfig(provider="api", model="m", base_url="https://x/v1")
        engine = vlm.VlmEngine(cfg)
        ok, msg = engine.probe()
        self.assertFalse(ok)
        self.assertIn("API Key", msg)

    def test_probe_ollama_reports_non_vision_model(self):
        rec = UrlopenRecorder([
            ("/api/tags", {"models": [{"name": "qwen3:4b"}, {"name": "llava:7b"}]}),
            ("/api/show", {"capabilities": ["completion"]}),
        ])
        cfg = vlm.VlmConfig(provider="ollama", model="qwen3:4b")
        engine = vlm.VlmEngine(cfg)
        with mock.patch.object(vlm.urllib.request, "urlopen", rec):
            ok, msg = engine.probe(force=True)
        self.assertFalse(ok, msg)
        self.assertIn("不能看图", msg)

    def test_probe_ollama_ok_when_vision_capable(self):
        rec = UrlopenRecorder([
            ("/api/tags", {"models": [{"name": "qwen2.5vl:3b"}]}),
            ("/api/show", {"capabilities": ["completion", "vision"]}),
        ])
        cfg = vlm.VlmConfig(provider="ollama", model="qwen2.5vl")
        engine = vlm.VlmEngine(cfg)
        with mock.patch.object(vlm.urllib.request, "urlopen", rec):
            ok, msg = engine.probe(force=True)
        self.assertTrue(ok, msg)

    def test_probe_ollama_missing_model(self):
        rec = UrlopenRecorder([("/api/tags", {"models": [{"name": "qwen3:4b"}]})])
        cfg = vlm.VlmConfig(provider="ollama", model="qwen2.5vl:3b")
        engine = vlm.VlmEngine(cfg)
        with mock.patch.object(vlm.urllib.request, "urlopen", rec):
            ok, msg = engine.probe(force=True)
        self.assertFalse(ok)
        self.assertIn("没有模型", msg)

    def test_model_matching_tolerates_latest_and_namespace(self):
        names = ["modelscope.cn/Qwen/Qwen3-4B-GGUF:latest"]
        self.assertEqual(vlm._match_model(names, "Qwen3-4B-GGUF"),
                         "modelscope.cn/Qwen/Qwen3-4B-GGUF:latest")
        self.assertEqual(vlm._match_model(names, "不存在的模型"), "")


# ---------------------------------------------------------------------
#  5) Tracker 行为
# ---------------------------------------------------------------------
class TestTracker(_Base):
    def _frame(self):
        img = np.full((480, 640, 3), 90, dtype=np.uint8)
        for cx in (140, 320, 500):
            import cv2
            cv2.ellipse(img, (cx, 300), (26, 70), 0, 0, 360, (150, 140, 130), -1)
            cv2.circle(img, (cx, 210), 20, (200, 190, 180), -1)
        return img

    def test_class_selection(self):
        self.assertIsNone(vlm.tracker_class_for("off"))
        self.assertIsNone(vlm.tracker_class_for(""))
        self.assertIs(vlm.tracker_class_for("ollama"), vlm.VlmPersonTracker)
        self.assertIs(vlm.tracker_class_for("api"), vlm.VlmPersonTracker)

    def test_accepted_params_include_base_and_vlm_keys(self):
        names = vlm.accepted_params(pd.PersonTracker, "ollama")
        for key in ("dense_mode", "min_hits", "boundary_ratio",
                    "vlm_provider", "vlm_model", "vlm_interval"):
            self.assertIn(key, names)
        base_only = vlm.accepted_params(pd.PersonTracker, "off")
        self.assertNotIn("vlm_model", base_only)

    def test_provider_off_behaves_like_base(self):
        """vlm_provider=off 时必须与原来的 PersonTracker 完全一致（同样的框）"""
        frame = self._frame()
        base = pd.PersonTracker(max_input_dim=1280, min_hits=1)
        vlm_tr = vlm.VlmPersonTracker(max_input_dim=1280, min_hits=1,
                                      vlm_provider="off")
        b_small, b_scale = base._scale_frame(frame)
        v_small, v_scale = vlm_tr._scale_frame(frame)
        self.assertEqual(base._detect(b_small), vlm_tr._detect(v_small))
        self.assertEqual(vlm_tr.vlm.cfg.enabled, False)

    def test_falls_back_to_yolo_when_vlm_unavailable(self):
        """配了多模态但连不上：_detect 仍要返回 YOLO 的框，且不阻塞"""
        frame = self._frame()
        tr = vlm.VlmPersonTracker(max_input_dim=1280, min_hits=1,
                                  vlm_provider="ollama", vlm_model="不存在的模型",
                                  vlm_interval=0.5, vlm_timeout=0.2)
        base = pd.PersonTracker(max_input_dim=1280, min_hits=1)
        small, _ = tr._scale_frame(frame)
        b_small, _ = base._scale_frame(frame)
        self.assertGreaterEqual(len(tr._detect(small)), 1)
        self.assertEqual(tr._vlm_last_source, "yolo")
        self.assertIn("多模态", tr.vlm_status())

    def test_uses_vlm_result_when_fresh(self):
        """多模态结果新鲜时：框来自模型，行为标签也被模型覆盖"""
        tr = vlm.VlmPersonTracker(max_input_dim=1280, min_hits=1, vlm_provider="off")
        # 直接注入"引擎刚返回过结果"的状态
        tr.vlm.cfg = vlm.VlmConfig(provider="ollama", model="fake")
        tr.vlm._persons = [{"rect": (100, 100, 200, 300), "action": "起立/站立",
                            "raw_action": "起立", "confidence": 0.9,
                            "phone": False, "desc": ""}]
        tr.vlm._objects = []
        tr.vlm._last_ok_at = vlm.time.time()
        persons, viol, line_y = tr.analyze_only(self._frame(), boundary_ratio=0.92)
        self.assertTrue(persons, "应至少有模型给的那一个人")
        rects = [p["rect"] for p in persons]
        self.assertIn((100, 100, 200, 300), rects)
        hit = [p for p in persons if p["rect"] == (100, 100, 200, 300)][0]
        self.assertEqual(hit["action"], "起立/站立")
        self.assertIn("起立/站立", hit["behaviors"])
        self.assertEqual(hit["source"], "vlm")
        self.assertGreaterEqual(viol, 1, "起立属于异常行为，应计入越界/异常数")

    def test_phone_object_attached_to_person(self):
        tr = vlm.VlmPersonTracker(max_input_dim=1280, min_hits=1, vlm_provider="off")
        tr.vlm.cfg = vlm.VlmConfig(provider="ollama", model="fake")
        tr.vlm._persons = [{"rect": (100, 100, 200, 300), "action": "正常姿态",
                            "raw_action": "正常姿态", "confidence": 0.9,
                            "phone": False, "desc": ""}]
        tr.vlm._objects = [{"rect": (150, 200, 80, 90), "label": "手机",
                            "confidence": 0.8, "phone": True}]
        tr.vlm._last_ok_at = vlm.time.time()
        persons, _, _ = tr.analyze_only(self._frame(), boundary_ratio=0.92)
        hit = [p for p in persons if p["rect"] == (100, 100, 200, 300)][0]
        self.assertIn("使用手机", hit["behaviors"])
        self.assertTrue(hit.get("has_phone"))
        self.assertEqual(hit["vlm_objects"][0]["label"], "手机")

    def test_draw_boxes_includes_objects_without_crash(self):
        import cv2
        tr = vlm.VlmPersonTracker(max_input_dim=1280, min_hits=1, vlm_provider="off")
        tr._vlm_objects = [{"rect": (50, 60, 100, 80), "label": "小抄",
                            "confidence": 0.5, "phone": False},
                           {"rect": (300, 100, 90, 120), "label": "手机",
                            "confidence": 0.7, "phone": True}]
        frame = self._frame()
        buf = frame.copy()
        tr.draw_boxes(buf, [], line_y=430, scale=1.0, font_size=12)
        self.assertEqual(buf.shape, frame.shape)
        self.assertGreater(int(np.abs(buf.astype(int) - frame.astype(int)).sum()), 0,
                           "物品框应该真的画上去了")

    def test_status_text_is_honest_when_not_configured(self):
        tr = vlm.VlmPersonTracker(max_input_dim=640, min_hits=1, vlm_provider="")
        self.assertIn("未启用", tr.vlm_status())
        d = tr.vlm_status_dict()
        self.assertFalse(d["enabled"])

    def test_settings_defaults_cover_all_keys(self):
        d = vlm.settings_defaults()
        for key in vlm.SETTING_KEYS:
            self.assertIn(key, d)


if __name__ == "__main__":
    unittest.main()
