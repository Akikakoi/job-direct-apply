"""法务最小实现（§14 ① 用户协议/隐私政策、② 投递告知）：政策版本单一事实源。

分工（刻意如此拆分）：
- **政策正文**属法务文本，放前端静态资产 `frontend/app/legal/legal-text.json`（中英逐条对照，
  `/legal/terms`、`/legal/privacy` 渲染同一份字符串，`scripts/export_legal_text.py` 出送审稿），
  随代码评审、可 diff；
- **后端只管版本与生效日 + 投递前告知要点**——它们是"用户同意了哪一版"的可核验凭据：
  `applications.consent_version` 引用本模块的 POLICY_VERSION，日后改政策只需递增版本号，
  历史投递记录仍能指回当时那一版（版本漂移会让同意记录失效，故前端也硬编码同一常量，
  由 `tests/test_legal.py` 做跨端契约断言）。

投递告知文案与产品 PRD（§4 关键鉴权/合规钩子、§8 三档模式）对齐的四点：
① 不代填密码、不自动提交，最终提交动作在第三方 ATS 站内由用户本人完成；
② 跳转的是**第三方**站点，简历与投递信息由用户在该站点确认后提交，适用该站点的隐私政策；
③ 平台只留存职位/简历画像/投递状态等**最小必要**信息，不做二次售卖；
④ 授权可随时撤回（删除简历/投递记录即撤回）。
"""

from __future__ import annotations

POLICY_VERSION = "1.0"
POLICY_EFFECTIVE_DATE = "2026-09-24"

# 政策清单（documents）：path 供前端页脚/告知弹层跳转，version 供同意留痕比对
POLICIES: list[dict] = [
    {
        "key": "terms",
        "title": "用户协议",
        "path": "/legal/terms",
        "version": POLICY_VERSION,
        "effective_date": POLICY_EFFECTIVE_DATE,
    },
    {
        "key": "privacy",
        "title": "隐私政策",
        "path": "/legal/privacy",
        "version": POLICY_VERSION,
        "effective_date": POLICY_EFFECTIVE_DATE,
    },
]

# 政策强制披露的三节（§14 ① 括号内要求：个人信息授权、删除、最小必要）。
# REQUIRED_SECTIONS 是给人看的中文节名（需求原文口径）；
# REQUIRED_SECTION_IDS 是**机器可读**的小节 id，对应前端 frontend/app/legal/legal-text.json
# 各小节的 `id` 字段——中英两套正文都必须含这几节，少一节就是"政策不完整"。
# 两处（tests/test_legal.py 与 scripts/export_legal_text.py）都按这份清单断言，不靠人记。
REQUIRED_SECTIONS: list[str] = ["授权范围", "个人信息删除", "最小必要"]
REQUIRED_SECTION_IDS: list[str] = ["authorization", "minimal", "deletion"]

# 投递前告知（前端「标记已投递」二次确认弹层展示）+ 授权勾选文案
APPLY_NOTICE: dict = {
    "title": "投递前告知",
    "points": [
        "本次投递将跳转到用人单位的第三方招聘系统，简历与投递信息由你在该站点确认后提交。",
        "平台不会代填密码、不绕过登录或验证码，也不会替你点击「提交」——最终提交由你本人完成。",
        "平台仅留存职位信息、简历画像与投递状态等最小必要数据，用于推荐与进度跟进，不对外售卖。",
        "你可以随时撤回授权：删除简历或投递记录后，平台不再基于其发起任何投递。",
    ],
    "consent_label": "我已阅读并同意《用户协议》《隐私政策》，并授权平台按上述告知发起本次投递",
    "withdraw_hint": "撤回方式：在「我的投递」删除记录，或在隐私政策页按指引申请删除个人信息。",
}


def consent_version_ok(version: str | None) -> bool:
    """同意版本校验：未带（老客户端/服务端调用）视为当前版本，带了则必须与当前一致。

    版本不一致说明前端停留旧政策页，继续投递会让留痕的同意版本对不上实际展示的文本，
    故按 400 拒绝并要求用户重新确认（而非静默改写为当前版本）。
    """
    return version is None or version == POLICY_VERSION


def policies_payload() -> dict:
    """`GET /api/legal/policies` 的响应体：版本 + 政策清单 + 投递告知 + 强制节清单。"""
    return {
        "policy_version": POLICY_VERSION,
        "effective_date": POLICY_EFFECTIVE_DATE,
        "documents": POLICIES,
        "required_sections": REQUIRED_SECTIONS,
        "required_section_ids": REQUIRED_SECTION_IDS,
        "apply_notice": APPLY_NOTICE,
    }