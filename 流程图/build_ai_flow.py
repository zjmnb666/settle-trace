# -*- coding: utf-8 -*-
"""《贸易服务结算与费用追溯》AI 数智化作业流程图 -> SVG/HTML"""

W = 1240.0
H = 1690.0

C_TITLE = "#1F3A5F"
C_SUB   = "#5C7590"
C_LINE  = "#8CA3BA"
C_BADGE_T = "#7C93AB"
C_BADGE_S = "#CBD9E8"
C_LEAD  = "#C3D0DE"

# 角色：底色 / 描边 / 强调条 / 标签
R = {
    "auto":  ("#EAF2FB", "#B9D2EC", "#4A90D9", "#DCEAF9", "#1F5C99", "#B9D2EC"),
    "ai":    ("#F3F0FD", "#CFC5F0", "#7B61C9", "#E8E3FA", "#57429F", "#C9BEF0"),
    "human": ("#EAF8EF", "#B6E0C6", "#2E9E63", "#DCF3E3", "#1D7A3F", "#A7D8B6"),
    "gate":  ("#DFF2E7", "#93CDA9", "#1D7A3F", "#CBEBD6", "#14663A", "#93CDA9"),
}
NAME = {"auto": "系统自动", "ai": "AI 辅助", "human": "人裁定", "gate": "人工闸口"}

FS = '"Microsoft YaHei", "PingFang SC", "Hiragino Sans GB", sans-serif'
out = []
add = out.append


def tw(text, fs):
    w = 0.0
    for ch in text:
        w += fs * (0.56 if ord(ch) < 0x2E80 else 1.0)
    return w


def role_pill(x, y, role, h=20, fs=11, fixed_w=None, text=None):
    bg, fg, bd = R[role][3], R[role][4], R[role][5]
    t = text or NAME[role]
    w = fixed_w if fixed_w else tw(t, fs) + 16
    add(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h}" rx="{h/2:.0f}" fill="{bg}" '
        f'stroke="{bd}" stroke-width="0.8"/>')
    add(f'<text x="{x+w/2:.1f}" y="{y+h/2+4:.1f}" font-size="{fs}" fill="{fg}" text-anchor="middle" '
        f'font-weight="600">{t}</text>')
    return w


def arrow_down(x, y1, y2):
    add(f'<line x1="{x}" y1="{y1}" x2="{x}" y2="{y2-9}" stroke="{C_LINE}" stroke-width="1.6"/>')
    add(f'<polygon points="{x-5},{y2-10} {x+5},{y2-10} {x},{y2}" fill="{C_LINE}"/>')


def arrow_right(x1, x2, y):
    add(f'<line x1="{x1}" y1="{y}" x2="{x2-9}" y2="{y}" stroke="{C_LINE}" stroke-width="1.6"/>')
    add(f'<polygon points="{x2-10},{y-5} {x2-10},{y+5} {x2},{y}" fill="{C_LINE}"/>')


def leader(y, x1=514, x2=618):
    add(f'<line x1="{x1}" y1="{y}" x2="{x2}" y2="{y}" stroke="{C_LEAD}" stroke-width="1.2" '
        f'stroke-dasharray="2 5"/>')


def node(no, top, title, desc, role, h=72, x=150, w=360):
    cy = top + h / 2
    fill, stroke, accent, _, _, _ = R[role]
    add(f'<rect x="88" y="{cy-12:.1f}" width="40" height="24" rx="6" fill="#FFFFFF" stroke="{C_BADGE_S}"/>')
    add(f'<text x="108" y="{cy+4.5:.1f}" font-size="13" fill="{C_BADGE_T}" text-anchor="middle" '
        f'font-weight="700">{no}</text>')
    add(f'<rect x="{x}" y="{top}" width="{w}" height="{h}" rx="10" fill="{fill}" stroke="{stroke}"/>')
    add(f'<rect x="{x+1}" y="{top+10}" width="4" height="{h-20}" rx="2" fill="{accent}"/>')
    add(f'<text x="{x+22}" y="{top+30}" font-size="15" font-weight="700" fill="{C_TITLE}">{title}</text>')
    add(f'<text x="{x+22}" y="{top+52}" font-size="12" fill="{C_SUB}">{desc}</text>')
    pw = tw(NAME[role], 11) + 16
    role_pill(x + w - 18 - pw, top + 13, role, fixed_w=pw)
    return cy


def diamond(no, cy, title, desc, role, cx=330, rx=215, ry=58):
    fill, stroke, accent = R[role][0], R[role][1], R[role][2]
    add(f'<rect x="88" y="{cy-12:.1f}" width="40" height="24" rx="6" fill="#FFFFFF" stroke="{C_BADGE_S}"/>')
    add(f'<text x="108" y="{cy+4.5:.1f}" font-size="13" fill="{C_BADGE_T}" text-anchor="middle" '
        f'font-weight="700">{no}</text>')
    add(f'<polygon points="{cx-rx},{cy} {cx},{cy-ry} {cx+rx},{cy} {cx},{cy+ry}" fill="{fill}" '
        f'stroke="{accent}" stroke-width="1.2" opacity="0.96"/>')
    fs = 14
    twt = tw(title, fs)
    pw = 80
    total = pw + 12 + twt
    sx = cx - total / 2
    role_pill(sx, cy - 24, role, fixed_w=pw)
    sx += pw + 12
    add(f'<text x="{sx:.1f}" y="{cy-1}" font-size="{fs}" font-weight="700" fill="{C_TITLE}">{title}</text>')
    add(f'<text x="{cx}" y="{cy+21}" font-size="11.5" fill="{C_SUB}" text-anchor="middle">{desc}</text>')


def note(top, title, lines, x=620, w=510):
    n = len(lines)
    h = 40 + 22 * n + 8
    add(f'<rect x="{x}" y="{top}" width="{w}" height="{h}" rx="9" fill="#FFFFFF" stroke="#D5DFEA"/>')
    add(f'<rect x="{x}" y="{top}" width="4" height="{h}" rx="2" fill="#C6D6E8"/>')
    add(f'<text x="{x+18}" y="{top+26}" font-size="13" font-weight="700" fill="#33506E">{title}</text>')
    for i, ln in enumerate(lines):
        add(f'<text x="{x+18}" y="{top+48+22*i}" font-size="12" fill="#5C7590">{ln}</text>')
    return h


# ============================ 画布 ============================
add(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
    f'font-family=\'{FS}\'>')
add(f'<rect width="{W}" height="{H}" fill="#FFFFFF"/>')
add('<g stroke-linecap="round">')

add(f'<text x="150" y="52" font-size="26" font-weight="700" fill="{C_TITLE}">'
    f'《贸易服务结算与费用追溯》· AI 数智化作业流程</text>')
add(f'<text x="150" y="78" font-size="12.5" fill="{C_SUB}">'
    f'代理费用结算单 ｜ 试点：1 个批次、40 份账单 ｜ 蓝色=系统自动 · 紫色=AI 辅助 · 绿色=人工介入</text>')

# AI 能力底座
add(f'<text x="150" y="112" font-size="12" fill="#8A9AAA">AI 能力底座</text>')
cx = 232
for t in ["多模态文档抽取", "语义映射匹配", "规则 + 查重模型", "自动生成与留痕"]:
    w = tw(t, 12.5) + 36
    add(f'<rect x="{cx}" y="94" width="{w:.1f}" height="26" rx="13" fill="#F3F0FD" stroke="#CFC5F0"/>')
    add(f'<circle cx="{cx+13}" cy="107" r="3" fill="#7B61C9"/>')
    add(f'<text x="{cx+22}" y="111.5" font-size="12.5" fill="#57429F">{t}</text>')
    cx += w + 10
add(f'<text x="{cx+8}" y="111.5" font-size="11.5" fill="#9AABB9">平台统一提供，业务侧不训练模型</text>')
add(f'<line x1="150" y1="140" x2="1130" y2="140" stroke="#E7EDF4" stroke-width="1.2"/>')

# 01
cy = node("01", 164, "智能建单登记", "邮件 / 附件 / 台账自动解析建单 · 模型无审批权", "auto")
leader(cy)
arrow_down(330, 236, 256)

# 02
diamond("02", 314, "是否在客户截止前？", "系统按客户日历自动判定", "auto")
add(f'<text x="342" y="374" font-size="11.5" fill="{C_SUB}">是</text>')
add(f'<text x="556" y="300" font-size="11.5" fill="{C_SUB}">否</text>')
arrow_right(545, 620, 314)
arrow_down(330, 372, 392)

# 03
cy = node("03", 392, "凭证与合同取证", "多模态抽取 4 个关键字段 · 三源交叉校验", "ai")
leader(cy)
arrow_down(330, 464, 484)
# 04
cy = node("04", 484, "服务项智能映射", "语义匹配合同服务项 · 低置信转人工", "ai")
leader(cy)
arrow_down(330, 556, 576)
# 05
node("05", 576, "交接登记与快照", "版本号 · 未结项挂账 · 接收人电子签", "auto")
arrow_down(330, 648, 668)
# 06
cy = node("06", 668, "机器预核对 + 人工复核差异", "机器全字段预核 · 人工只看差异项", "ai")
leader(cy)
arrow_down(330, 740, 760)
# 07
node("07", 760, "质量检查与智能查重", "规则引擎 + 历史查重 · 只提示不删除", "auto")
arrow_down(330, 832, 852)

# 08
diamond("08", 910, "疑点可否解释？", "模型给置信度与依据链", "human")
add(f'<text x="556" y="896" font-size="11.5" fill="{C_SUB}">否</text>')
add(f'<text x="342" y="970" font-size="11.5" fill="{C_SUB}">是</text>')
arrow_right(545, 620, 910)
arrow_down(330, 968, 988)

# 09
diamond("09", 1046, "版本 / 字段可确认？", "有效期 + 确认状态自动判定", "auto")
add(f'<text x="556" y="1032" font-size="11.5" fill="{C_SUB}">否</text>')
add(f'<text x="342" y="1106" font-size="11.5" fill="{C_SUB}">是</text>')
arrow_right(545, 620, 1046)
arrow_down(330, 1104, 1124)

# 10
cy = node("10", 1124, "正式确认（电子签）", "授权负责人签字 · 系统校验但不代签", "gate")
leader(cy)
arrow_down(330, 1196, 1216)
# 11
node("11", 1216, "客户口径确认", "自动生成差异解释草稿 · 人工定稿", "ai")
arrow_down(330, 1288, 1308)
# 12
cy = node("12", 1308, "审批放行", "规则前置校验 · 仅异常 / 跨企业上会", "gate")
leader(cy)
arrow_down(330, 1380, 1400)
# 13
node("13", 1400, "归档与审计", "全链路留痕 · 一键追溯谁改了什么", "auto")

# 08「否」回流
add(f'<path d="M620,946 L585,946 L585,1102 L470,1102 L470,1115" fill="none" stroke="{C_LINE}" '
    f'stroke-width="1.4" stroke-dasharray="5 5"/>')
add(f'<polygon points="465,1115 475,1115 470,1124" fill="{C_LINE}"/>')

# ============================ 右侧批注 ============================
note(164, "智能建单", ["邮件 / 群消息 / 附件自动解析，字段预填并留原文件", "只填登记字段，不产出任何结算结论"])
note(256, "自动挂起", ["超截止自动挂起 + 生成缺项清单与责任人", "补齐后进下一批次，不倒签发生时间"])
note(392, "多模态抽取", ["OCR + LLM 从发票 / 合同 / 台账抽 4 个关键字段", "三源交叉校验，冲突自动标红并附原文页"])
note(484, "语义映射", ["费用名称 ↔ 合同服务项向量匹配 + 同义词库", "置信度低于阈值转人工，映射记录可回溯"])
note(576, "人机边界（设计原则）", ["机器做：找得到 · 算得准 · 留得住 · 可复核", "人来做：判得清 · 签得下 · 担得起 · 可追溯"])
note(668, "机器预核对", ["全字段自动比对，只把差异项推给人工复核", "异常对账 55 分钟 / 份 → 约 8 分钟 / 份（目标）"])
note(852, "查重模型", ["币种换算 + 历史账单比对，输出重复概率与依据", "只提示不删除，保留原行与原始版本",
                       "人工裁定责任后留痕，返回「正式确认」继续"])
note(988, "自动判定", ["版本有效期 + 确认状态双条件自动校验", "不可确认即自动派单，进人工处理队列"])
note(1124, "电子签", ["权限、完整性、金额阈值自动校验后送签", "授权名单待业务负责人确认，系统不代签"])
note(1308, "审批规则前置", ["规则先跑，只有异常 / 跨企业访问才上会", "审批是否单设节点尚未核实，按假设设计"])

# ============================ 人机分工对比 ============================
add(f'<line x1="150" y1="1504" x2="1130" y2="1504" stroke="#E7EDF4" stroke-width="1.2"/>')
cards = [
    ("访谈基线（As-Is）", "13 步全人工", "10 处人工 · 异常对账 55 分钟/份", "#B4232A", "#FDF1F2", "#F3C3C8"),
    ("数智化后（To-Be）", "13 步人机协同", "人工仅 3 处 · 约 15 分钟/份（目标）", "#1D7A3F", "#EFF9F2", "#B6E0C6"),
    ("变化（演练假设）", "人工核对 ↓70%", "留痕 100% · 结论仍由授权人签字", "#57429F", "#F5F2FE", "#CFC5F0"),
]
cw, gap, x0 = 316, 26, 150
for i, (t1, t2, t3, col, bg, bd) in enumerate(cards):
    x = x0 + i * (cw + gap)
    add(f'<rect x="{x}" y="1522" width="{cw}" height="88" rx="10" fill="{bg}" stroke="{bd}"/>')
    add(f'<text x="{x+18}" y="1544" font-size="11.5" fill="#7C93AB">{t1}</text>')
    add(f'<text x="{x+18}" y="1570" font-size="17" font-weight="700" fill="{col}">{t2}</text>')
    add(f'<text x="{x+18}" y="1594" font-size="11.5" fill="#5C7590">{t3}</text>')

add(f'<text x="150" y="1648" font-size="11.5" fill="#B4232A">'
    f'三条红线：模型不决策 · 不自动付款 · 不修改已确认账单 · 疑似重复项不自动删除</text>')
add(f'<text x="150" y="1670" font-size="11" fill="#93A6B8">'
    f'依据 discovery-report.md 访谈整理；效率数字为演练目标值，需实测校准，不代表企业真实经营数据</text>')

add('</g></svg>')

svg = "\n".join(out)
with open("ai_flow.svg", "w", encoding="utf-8") as f:
    f.write(svg)

html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>《贸易服务结算与费用追溯》AI 数智化作业流程</title>
<style>
  html,body{{margin:0;padding:0;background:#eef2f7;}}
  .wrap{{width:{W}px;margin:0 auto;background:#fff;}}
  svg{{display:block;}}
</style>
</head>
<body><div class="wrap">
{svg}
</div></body></html>
"""
with open("ai_flow.html", "w", encoding="utf-8") as f:
    f.write(html)
print("ok", len(svg))
