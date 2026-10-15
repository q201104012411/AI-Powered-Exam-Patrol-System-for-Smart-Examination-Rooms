# -*- coding: utf-8 -*-
r"""
数据分类分级目录（《考生数据保护.md》第一节）

把文档里的表格做成**可执行、可核查**的数据结构：
  · 每条数据类别带敏感级别、典型内容、存储位置、保留期限、必要加密字段
  · 保留期限用 days 表示（None = 沿用既有规定 / 按考务档案要求，不自动销毁）
  · required_encrypted_fields 用于验收时逐字段核对"敏感数据加密率 100%"

重要：本表是**唯一口径**。任何"这条数据要不要加密/多久删"的判断都从这里取，
不允许在业务代码里另写一套（否则验收时口径打架）。
"""

# 敏感级别
LEVEL_HIGH = "高"           # 敏感个人信息
LEVEL_MID_HIGH = "中高"
LEVEL_MID = "中"

# 存储位置
LOC_LOCAL = "考点本地 + 加密库"
LOC_LOCAL_ENC = "本地加密"
LOC_EDGE = "边缘侧本地"
LOC_CLOUD = "云侧平台"
LOC_EXISTING = "既有考务系统"

# 保留期限（天）。None 表示"沿用既有规定，不由本系统自动销毁"。
DAY_30 = 30
DAY_EXAM_PERIOD = 30            # 30 天 ~ 1 个考期，取下限 30 天


class Category(object):
    """一条数据类别（对应文档表格的一行）"""

    __slots__ = ("key", "name", "level", "sample", "location", "retention_days",
                 "retention_text", "irreversible", "required_encrypted_fields",
                 "auto_destroy", "notes")

    def __init__(self, key, name, level, sample, location, retention_days,
                 retention_text, irreversible=False,
                 required_encrypted_fields=(), auto_destroy=True, notes=""):
        self.key = key
        self.name = name
        self.level = level
        self.sample = sample
        self.location = location
        self.retention_days = retention_days
        self.retention_text = retention_text
        self.irreversible = irreversible
        self.required_encrypted_fields = tuple(required_encrypted_fields)
        self.auto_destroy = auto_destroy
        self.notes = notes

    def to_dict(self):
        return {"key": self.key, "name": self.name, "level": self.level,
                "sample": self.sample, "location": self.location,
                "retention_days": self.retention_days,
                "retention_text": self.retention_text,
                "irreversible": self.irreversible,
                "required_encrypted_fields": list(self.required_encrypted_fields),
                "auto_destroy": self.auto_destroy, "notes": self.notes}

    def __repr__(self):
        return "<Category %s %s %s>" % (self.key, self.name, self.level)


# ---------------------------------------------------------------- 目录本体
CATEGORIES = [
    Category(
        key="face_template", name="人脸特征模板", level=LEVEL_HIGH,
        sample="不可逆特征向量",
        location=LOC_LOCAL, retention_days=DAY_30,
        retention_text="考试结束后 30 天",
        irreversible=True,
        required_encrypted_fields=("template_blob",),
        notes="只落特征模板、不落原图；模板本身不可逆，另做静态加密。",
    ),
    Category(
        key="face_image", name="人脸原始图像", level=LEVEL_HIGH,
        sample="核验抓拍图",
        location=LOC_LOCAL_ENC, retention_days=DAY_30,
        retention_text="最短必要（建议 ≤ 30 天）",
        required_encrypted_fields=("file_path",),
        notes="确需留图时单项加密并设最短期限；文件本体加密后落 vault\\。",
    ),
    Category(
        key="exam_video", name="考场视频", level=LEVEL_HIGH,
        sample="全量视频流",
        location=LOC_EDGE, retention_days=DAY_30,
        retention_text="按当地考务规定，建议 30 天起",
        required_encrypted_fields=("video_path",),
        auto_destroy=False,
        notes="边缘侧本地存储并加密；本系统登记其保留期并到期提示，"
              "删除动作由边缘侧执行（本系统只核查登记与提醒，不代删）。",
    ),
    Category(
        key="alert_clip", name="告警片段与元数据", level=LEVEL_MID_HIGH,
        sample="异常片段、结构化记录",
        location=LOC_CLOUD, retention_days=DAY_EXAM_PERIOD,
        retention_text="30 天～1 个考期",
        required_encrypted_fields=("detail", "candidate_ref"),
    ),
    Category(
        key="review_record", name="复核与处置记录", level=LEVEL_MID,
        sample="结论、理由、附件",
        location=LOC_CLOUD, retention_days=DAY_EXAM_PERIOD,
        retention_text="按考务档案要求",
        auto_destroy=False,
        required_encrypted_fields=("reason",),
        notes="属考务档案，按档案规定保管，不由本系统自动销毁。",
    ),
    Category(
        key="enroll_identity", name="身份报名信息", level=LEVEL_HIGH,
        sample="姓名、证件号、准考证",
        location=LOC_EXISTING, retention_days=None,
        retention_text="沿用既有规定",
        auto_destroy=False,
        required_encrypted_fields=("candidate_name", "id_card_no", "ticket_no"),
        notes="主体数据在既有考务系统；本系统只存引用号 + 哈希 + 掩码。",
    ),
]

BY_KEY = {c.key: c for c in CATEGORIES}


def get(key):
    """取一条数据类别；未知 key 直接抛错，避免"以为加密了其实没登记"。"""
    if key not in BY_KEY:
        raise KeyError("未登记的数据类别：%s（请先在 catalog.CATEGORIES 里登记）" % key)
    return BY_KEY[key]


def keys():
    return [c.key for c in CATEGORIES]


def retention_days(key):
    return get(key).retention_days


def required_encrypted_fields(key):
    return get(key).required_encrypted_fields


def auto_destroyable():
    """参与到期自动销毁的类别（retention_days 非空且 auto_destroy 为真）。"""
    return [c for c in CATEGORIES if c.auto_destroy and c.retention_days is not None]


def level_of(key):
    return get(key).level


def table_markdown():
    """把目录渲染成 Markdown 表（直接贴进报告/项目书）"""
    lines = ["| 数据类别 | 敏感级别 | 典型内容 | 存储位置 | 保留期限 | 自动销毁 |",
             "| --- | --- | --- | --- | --- | --- |"]
    for c in CATEGORIES:
        auto = "是" if (c.auto_destroy and c.retention_days is not None) else "否"
        lines.append("| %s | %s | %s | %s | %s | %s |"
                     % (c.name, c.level, c.sample, c.location, c.retention_text, auto))
    return "\n".join(lines)


def validate():
    """
    自检：目录自身必须自洽。
    返回 (ok, 问题列表)。问题包括"敏感级别高但没登记加密字段"这类会导致
    验收指标算不出来的情况。
    """
    problems = []
    if not CATEGORIES:
        problems.append("分类分级目录为空")
    seen = set()
    for c in CATEGORIES:
        if c.key in seen:
            problems.append("类别 key 重复：%s" % c.key)
        seen.add(c.key)
        if c.level in (LEVEL_HIGH, LEVEL_MID_HIGH) and not c.required_encrypted_fields:
            problems.append("%s（%s）未登记必须加密的字段" % (c.name, c.level))
        if c.retention_days is not None and c.retention_days <= 0:
            problems.append("%s 保留期限非法：%s" % (c.name, c.retention_days))
        if c.irreversible and c.key != "face_template":
            problems.append("%s 标记为不可逆但没有对应说明" % c.name)
    return (not problems), problems
