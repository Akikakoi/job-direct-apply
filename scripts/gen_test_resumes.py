# -*- coding: utf-8 -*-
"""生成三份不同专业的测试简历 PDF（纯文本型，可被后端 pypdf 直接抽取）。

用法：
    C:/Users/Yuki/.workbuddy/binaries/python/envs/default/Scripts/python.exe scripts/gen_test_resumes.py

输出：test_resumes/*.pdf
"""
from __future__ import annotations

from pathlib import Path

from fpdf import FPDF

FONT = r"C:\Windows\Fonts\msyh.ttc"
FONT_BOLD = r"C:\Windows\Fonts\msyhbd.ttc"
OUT_DIR = Path(__file__).resolve().parent.parent / "test_resumes"

SECTION_GAP = 3
LINE_GAP = 1.2


class ResumePDF(FPDF):
    def section(self, title: str) -> None:
        self.ln(SECTION_GAP)
        self.set_font("msyhbd", size=12)
        self.set_text_color(30, 60, 120)
        self.cell(0, 8, title, new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(30, 60, 120)
        self.set_line_width(0.4)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(1.5)
        self.set_text_color(0, 0, 0)
        self.set_font("msyh", size=10.5)

    def para(self, text: str) -> None:
        self.multi_cell(0, 6, text, new_x="LMARGIN", new_y="NEXT")
        self.ln(LINE_GAP)

    def bullet(self, text: str) -> None:
        self.set_x(self.l_margin + 2)
        self.multi_cell(self.epw - 2, 6, "· " + text, new_x="LMARGIN", new_y="NEXT")
        self.ln(LINE_GAP)


def build_resume(filename: str, name: str, basic: list[str], education: list[str],
                 experiences: list[tuple[str, list[str]]], skills: list[str],
                 self_eval: str) -> Path:
    pdf = ResumePDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_font("msyh", "", FONT)
    pdf.add_font("msyhbd", "", FONT_BOLD)
    pdf.add_page()
    pdf.set_margins(18, 15, 18)

    # 标题
    pdf.set_font("msyhbd", size=20)
    pdf.cell(0, 12, name, align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("msyh", size=10)
    pdf.set_text_color(90, 90, 90)
    pdf.cell(0, 7, "  |  ".join(basic), align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(2)

    pdf.section("教育背景")
    for line in education:
        pdf.bullet(line)

    pdf.section("实习与项目经历")
    for title, points in experiences:
        pdf.set_font("msyhbd", size=10.5)
        pdf.cell(0, 6, title, new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("msyh", size=10.5)
        for p in points:
            pdf.bullet(p)

    pdf.section("技能清单")
    for s in skills:
        pdf.bullet(s)

    pdf.section("自我评价")
    pdf.para(self_eval)

    OUT_DIR.mkdir(exist_ok=True)
    out = OUT_DIR / filename
    pdf.output(out)
    return out


RESUMES = [
    dict(
        filename="测试简历_林学_森林培育_沈青野.pdf",
        name="沈青野",
        basic=["男", "2002.04", "求职意向：林业技术员/森林资源调查", "136-5577-8899", "shenqingye.test@example.com"],
        education=[
            "南京林业大学 · 林学 · 本科（2021.09 - 2025.06）",
            "主修课程：森林生态学、树木学、森林培育学、森林经理学、测量学、林木育种学、3S 技术导论",
            "GPA 3.3/4.0，获校级奖学金一次；已考取林业有害生物防治员（中级）证书",
        ],
        experiences=[
            ("某省林业调查规划设计院 · 资源调查实习生（实习，2024.06 - 2024.11）", [
                "参与两个县区的森林资源二类调查外业，完成标准地测设与每木检尺约 60 块，记录树种、胸径、树高与郁闭度等因子；",
                "使用罗盘仪与 RTK 进行样地定位，内业用 ArcGIS 整理小班边界并核对属性表，修正错漏图斑 30 余个；",
                "协助编写调查报告的森林蓄积量与树种构成章节，数据经质检组一次复核通过。",
            ]),
            ("亚热带人工林抚育间伐试验 · 课题组成员（项目，2023.03 - 2024.05）", [
                "跟踪杉木人工林间伐试验样地，参与间伐前后生长量测定与土壤取样，累计采集并录入观测数据 2000 余条；",
                "负责样地生物量样本的烘干称重与数据整理，用 R 完成基础统计分析并绘制生长曲线图；",
                "项目成果参与撰写论文一篇（已投稿），负责图表制作与参考文献整理。",
            ]),
        ],
        skills=[
            "专业基础：能识别本区域常见乔木树种 80 种以上，熟悉森林调查常用因子测定方法；",
            "工具软件：熟练使用 ArcGIS 做基本制图与属性管理，会用 ENVI 处理简单遥感影像；",
            "数据处理：会用 Excel 与 R 完成数据整理、基础统计与绘图；",
            "野外能力：能适应长期野外作业，持有 C1 驾照，具备基本无人机操作经验。",
        ],
        self_eval=("做事踏实，能吃苦，实习期间全程跟完两个调查项目的外业内业，对森林资源工作从样地到报表的"
                   "完整流程有实际体感。喜欢户外但更看重数据的准确性，习惯当天数据当天录完当天核对。"
                   "希望在基层林业技术岗位上把调查本领练扎实。"),
    ),
    dict(
        filename="测试简历_护理学_临床护理_苏婉宁.pdf",
        name="苏婉宁",
        basic=["女", "2003.11", "求职意向：临床护士", "159-2244-6655", "suwanning.test@example.com"],
        education=[
            "川北医学院 · 护理学 · 本科（2021.09 - 2025.06）",
            "主修课程：基础护理学、内科护理学、外科护理学、急危重症护理学、健康评估、药理学",
            "已通过护士执业资格考试，CET-4；在校期间获“优秀实习生”称号",
        ],
        experiences=[
            ("某三甲医院 · 内科病区实习护士（实习，2024.06 - 2025.03）", [
                "轮转呼吸内科、心内科病区，在带教老师指导下完成静脉输液、生命体征监测、雾化吸入等基础护理操作约 1500 人次；",
                "参与 8 例危重患者护理查房，协助记录护理文书并完成床旁交接班；",
                "实习期间无护理差错与投诉，获病区护士长书面推荐。",
            ]),
            ("社区健康服务中心 · 健康宣教志愿者（志愿，2023.03 - 2024.05）", [
                "每两周参与一次社区慢病随访与健康宣教，面向老年居民讲解血压、血糖监测要点；",
                "协助建立 60 余份社区居民健康档案，整理随访记录。",
            ]),
        ],
        skills=[
            "操作技能：熟练执行无菌技术、静脉穿刺、心肺复苏等基础与急救操作；",
            "文书能力：熟悉护理病历与交接班记录书写规范；",
            "沟通协作：能耐心与老年患者及家属沟通，适应三班倒工作节奏；",
            "其他：CET-4，熟练使用 Office 办公软件与医院护理信息系统。",
        ],
        self_eval=("性格沉稳细致，动手能力强，能严格执行查对制度与操作流程。实习中体会到临床工作对耐心与"
                   "体力的双重考验，但更有职业成就感。愿意从基础护理做起，在规范的临床环境中成长为独当一面的专科护士。"),
    ),
    dict(
        filename="测试简历_市场营销_数字营销_程一帆.pdf",
        name="程一帆",
        basic=["女", "2002.08", "求职意向：数字营销/新媒体运营", "177-8899-0102", "chengyifan.test@example.com"],
        education=[
            "浙江工商大学 · 市场营销 · 本科（2020.09 - 2024.06）",
            "主修课程：消费者行为学、市场调研、品牌管理、数字营销、数据分析基础、广告学",
            "GPA 3.4/4.0，全国大学生市场调查与分析大赛省赛二等奖",
        ],
        experiences=[
            ("某快消品牌 · 数字营销实习生（实习，2024.03 - 2024.09）", [
                "协助运营品牌官方小红书与抖音账号，独立撰写图文及短视频脚本 40 余篇，其中 3 篇互动量超过账号此前半年均值；",
                "参与两次大促投放，协助整理人群包与素材数据，复盘报告被团队沿用为固定模板；",
                "使用第三方工具监测竞品动态，每周输出竞品内容分析简报。",
            ]),
            ("校园咖啡品牌推广项目 · 负责人（项目，2023.03 - 2023.12）", [
                "带领 5 人小组为校内咖啡店做品牌焕新，完成问卷调研（回收有效问卷 412 份）并重新定位产品卖点；",
                "策划“期末续命咖啡”主题campaign，配套社群运营与优惠券发放，活动期间门店营业额环比提升约 30%；",
                "项目获校级创新创业训练计划立项。",
            ]),
        ],
        skills=[
            "内容运营：熟悉小红书、抖音、微信公众平台的运营规则与内容调性；",
            "数据分析：熟练使用 Excel（透视表/函数），了解 SQL 基础查询与第三方监测工具；",
            "设计工具：会使用 Canva、剪映完成基础图文与短视频制作；",
            "其他：CET-6，具备完整的问卷设计与基础统计分析能力。",
        ],
        self_eval=("对内容与数据的敏感度均衡：既愿意琢磨用户为什么点进来，也习惯用数据验证判断。"
                   "实习与校园项目中积累了从调研、创意到复盘的完整闭环经验，抗压性好，期待在节奏快的营销团队中快速成长。"),
    ),
]


def main() -> None:
    for r in RESUMES:
        out = build_resume(
            filename=r["filename"], name=r["name"], basic=r["basic"],
            education=r["education"], experiences=r["experiences"],
            skills=r["skills"], self_eval=r["self_eval"],
        )
        # 验证：用后端同款 pypdf 确认可抽出有效文本（避免被判为扫描件）
        from pypdf import PdfReader
        reader = PdfReader(str(out))
        text = "\n".join((p.extract_text() or "") for p in reader.pages)
        chars = len(text.strip())
        print(f"{out.name}: {len(reader.pages)} 页, 抽取字符 {chars}, 判定={'文本型 OK' if chars / max(len(reader.pages),1) >= 20 else '疑似扫描件!'}")


if __name__ == "__main__":
    main()
