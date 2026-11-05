# -*- coding: utf-8 -*-
"""生成《贸易服务结算与费用追溯》作业流程图（SVG + HTML）"""

W = 1240.0
H = 1690.0

C_TITLE   = "#1F3A5F"
C_SUB     = "#5C7590"
C_NODE_F  = "#EDF4FC"
C_NODE_S  = "#A9C8E8"
C_DIA_F   = "#F8FBFF"
C_LINE    = "#8CA3BA"
C_BADGE_T = "#7C93AB"
C_BADGE_S = "#CBD9E8"
C_LEAD    = "#C3D0DE"

TAGS = {
    "最耗时": ("#FBE0E2", "#B4232A", "#F3C3C8"),
    "耗时":   ("#FCDCC4", "#A85B14", "#F2C6A4"),
    "必须人工": ("#DCF3E3", "#1D7A3F", "#A7D8B6"),
    "等待":   ("#FDF0C4", "#92700F", "#EBD48A"),
}

FS = '"Microsoft YaHei", "PingFang SC", "Hiragino Sans GB", sans-serif'
out = []
add = out.append


def tw(text, fs):
    w = 0.0
    for ch in text:
        w += fs * (0.56 if ord(ch) < 0x2E80 else 1.0)
    return w


def pill(x, y, text, key, h=20, fs=11, fixed_w=None):
    bg, fg, bd = TAGS[key]
    w = fixed_w if fixed_w else tw(text, fs) + 16
    add(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h}" rx="{h/2:.0f}" '
        f'fill="{bg}" stroke="{bd}" stroke-width="0.8"/>')
    add(f'<text x="{x + w/2:.1f}" y="{y + h/2 + 4:.1f}" font-size="{fs}" fill="{fg}" '
        f'text-anchor="middle" font-weight="600">{text}</text>')
    return w


def arrow_down(x, y1, y2, dashed=False):
    style = 'stroke-dasharray="5 5"' if dashed else ''
    add(f'<line x1="{x}" y1="{y1}" x2="{x}" y2="{y2-9}" stroke="{C_LINE}" stroke-width="1.6" {style}/>')
    add(f'<polygon points="{x-5},{y2-10} {x+5},{y2-10} {x},{y2}" fill="{C_LINE}"/>')


def arrow_right(x1, x2, y, dashed=False):
    style = 'stroke-dasharray="5 5"' if dashed else ''
    add(f'<line x1="{x1}" y1="{y}" x2="{x2-9}" y2="{y}" stroke="{C_LINE}" stroke-width="1.6" {style}/>')
    add(f'<polygon points="{x2-10},{y-5} {x2-10},{y+5} {x2},{y}" fill="{C_LINE}"/>')


def leader(y, x1=514, x2=618):
    add(f'<line x1="{x1}" y1="{y}" x2="{x2}" y2="{y}" stroke="{C_LEAD}" stroke-width="1.2" '
        f'stroke-dasharray="2 5"/>')


def node(no, top, title, desc, tags=(), h=72, x=150, w=360):
    cy = top + h / 2
    add(f'<rect x="88" y="{cy-12:.1f}" width="40" height="24" rx="6" fill="#FFFFFF" stroke="{C_BADGE_S}"/>')
    add(f'<text x="108" y="{cy+4.5:.1f}" font-size="13" fill="{C_BADGE_T}" text-anchor="middle" '
        f'font-weight="700">{no}</text>')
    add(f'<rect x="{x}" y="{top}" width="{w}" height="{h}" rx="10" fill="{C_NODE_F}" stroke="{C_NODE_S}"/>')
    add(f'<text x="{x+22}" y="{top+30}" font-size="15" font-weight="700" fill="{C_TITLE}">{title}</text>')
    add(f'<text x="{x+22}" y="{top+52}" font-size="12" fill="{C_SUB}">{desc}</text>')
    rx = x + w - 18
    for t in reversed(tags):
        pw = tw(t, 11) + 16
        rx -= pw
        pill(rx, top + 13, t, t, fixed_w=pw)
        rx -= 6
    return cy


def diamond(no, cy, title, desc, tag=None, cx=330, rx=215, ry=58):
    add(f'<rect x="88" y="{cy-12:.1f}" width="40" height="24" rx="6" fill="#FFFFFF" stroke="{C_BADGE_S}"/>')
    add(f'<text x="108" y="{cy+4.5:.1f}" font-size="13" fill="{C_BADGE_T}" text-anchor="middle" '
        f'font-weight="700">{no}</text>')
    add(f'<polygon points="{cx-rx},{cy} {cx},{cy-ry} {cx+rx},{cy} {cx},{cy+ry}" '
        f'fill="{C_DIA_F}" stroke="{C_NODE_S}" stroke-width="1.2"/>')
    fs = 14
    twt = tw(title, fs)
    pw = 76 if tag else 0
    total = pw + (12 if tag else 0) + twt
    sx = cx - total / 2
    if tag:
        pill(sx, cy - 24, tag, tag, fixed_w=76)
        sx += pw + 12
    add(f'<text x="{sx:.1f}" y="{cy-1}" font-size="{fs}" font-weight="700" fill="{C_TITLE}">{title}</text>')
    add(f'<text x="{cx}" y="{cy+21}" font-size="11.5" fill="{C_SUB}" text-anchor="middle">{desc}</text>')


def note(top, title, lines, x=620, w=510, tan=True):
    n = len(lines)
    h = 40 + 22 * n + 8
    fill = "#FBF1DE" if tan else "#F7F9FC"
    stroke = "#E4C89B" if tan else "#D5DFEA"
    tcol = "#8A5B22" if tan else "#33506E"
    lcol = "#7A6A50" if tan else "#5C7590"
    add(f'<rect x="{x}" y="{top}" width="{w}" height="{h}" rx="9" fill="{fill}" stroke="{stroke}"/>')
    add(f'<text x="{x+18}" y="{top+26}" font-size="13" font-weight="700" fill="{tcol}">{title}</text>')
    for i, ln in enumerate(lines):
        add(f'<text x="{x+18}" y="{top+48+22*i}" font-size="12" fill="{lcol}">{ln}</text>')
    return h


# ============================ 画布 ============================
add(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
    f'font-family=\'{FS}\'>')
add(f'<rect width="{W}" height="{H}" fill="#FFFFFF"/>')
add('<g stroke-linecap="round">')

add(f'<text x="150" y="52" font-size="27" font-weight="700" fill="{C_TITLE}">《贸易服务结算与费用追溯》</text>')
add(f'<text x="150" y="80" font-size="12.5" fill="{C_SUB}">'
    f'代理费用结算单 · 详细作业流程 ｜ 试点范围：1 个批次、40 份账单 ｜ 标注：费时费钱环节 · 必须人工介入环节</text>')
add(f'<line x1="150" y1="94" x2="1130" y2="94" stroke="#E7EDF4" stroke-width="1.2"/>')

# 01
cy = node("01", 110, "费用登记（草稿）", "一线专员提交变更 · 模型无审批权", ["必须人工"])
leader(cy)
arrow_down(330, 182, 202)

# 02
diamond("02", 260, "是否在客户截止前？", "截止后补录须登记缺项，不默认通过")
add(f'<text x="342" y="320" font-size="11.5" fill="{C_SUB}">是</text>')
add(f'<text x="556" y="246" font-size="11.5" fill="{C_SUB}">否</text>')
arrow_right(545, 620, 260)
arrow_down(330, 318, 340)

# 03
cy = node("03", 340, "凭证与合同服务项取证", "原币种 · 汇率日期 · 凭证编号 · 三处取数", ["最耗时", "必须人工"])
leader(cy)
arrow_down(330, 412, 432)
# 04
node("04", 432, "服务项映射与缺项清单", "费用名称 ↔ 合同服务项 · 说明反复改写", ["耗时", "必须人工"])
arrow_down(330, 504, 524)
# 05
node("05", 524, "交接登记", "最新版本 · 未确认事项 · 截止时间 · 接收人", ["必须人工"])
arrow_down(330, 596, 616)
# 06
cy = node("06", 616, "双人核对", "当班人员 + 复核人 · 逐字段比对", ["最耗时", "必须人工"])
leader(cy)
arrow_down(330, 688, 708)
# 07
cy = node("07", 708, "关键字段与质量检查", "重复编号 · 空时间 · 版本断链 · 单位差异", ["耗时"])
leader(cy)
arrow_down(330, 780, 800)

# 08
diamond("08", 858, "跨币种重复项可否解释？", "只提示 · 不自动删除 · 保留原行", tag="必须人工")
add(f'<text x="556" y="844" font-size="11.5" fill="{C_SUB}">否</text>')
add(f'<text x="342" y="922" font-size="11.5" fill="{C_SUB}">是</text>')
arrow_right(545, 620, 858)
arrow_down(330, 916, 936)

# 09
diamond("09", 994, "版本可确认？", "版本有效期 + 确认状态双条件", tag="必须人工")
add(f'<text x="556" y="980" font-size="11.5" fill="{C_SUB}">否</text>')
add(f'<text x="342" y="1058" font-size="11.5" fill="{C_SUB}">是</text>')
arrow_right(545, 620, 994)
arrow_down(330, 1052, 1082)

# 10
cy = node("10", 1082, "正式确认", "授权业务负责人签字 · 提交 ≠ 确认", ["等待", "必须人工"])
leader(cy)
arrow_down(330, 1154, 1180)
# 11
cy = node("11", 1180, "客户口径确认", "结果用途由客户方确认 · 不推测意图", ["必须人工"])
leader(cy)
arrow_down(330, 1252, 1278)
# 12
cy = node("12", 1278, "审批放行", "异常放行 · 跨企业资料访问授权", ["等待", "必须人工"])
leader(cy)
arrow_down(330, 1350, 1376)
# 13
node("13", 1376, "归档与审计", "谁查过 · 哪个版本 · 什么建议 · 谁确认")

# 08「否」回流：记录留痕后回到「正式确认」
add(f'<path d="M620,906 L585,906 L585,1060 L470,1060 L470,1073" fill="none" stroke="{C_LINE}" '
    f'stroke-width="1.4" stroke-dasharray="5 5"/>')
add(f'<polygon points="465,1073 475,1073 470,1082" fill="{C_LINE}"/>')

# ============================ 右侧批注 ============================
note(104, "读取失败时", ["保留上次快照并标记时间", "提示人工核查，不编造最新状态、不静默重试"], tan=False)
note(204, "挂起 · 截止后补录", ["缺项登记 + 未结项挂账，不默认通过", "补齐后进入下一批次，不倒签发生时间"])
note(336, "取证来源分散", ["内部服务台 · 业务台账 · 文件库三处取数", "尚无统一关联编号，只能靠字段组合匹配"], tan=False)
note(600, "人工核对队列（不可自动）", ["被阻断 · 禁止出结果；缺字段 → 截止后补发", "人工决策后方可继续，模型不给结论"])
note(700, "缺项清单", ["缺什么 + 来源位置，一页呈现", "补录后须签字，减少重复复核"])
note(806, "疑似重复 · 不自动删除", ["保留原行与原始版本，由人工判断责任", "币种换算口径未核实，提示不等于结论",
                                    "→ 记录留痕后回到「正式确认」继续"], tan=True)
note(938, "无法确认 · 挂起", ["版本 / 关键字段对不上 → 进人工处理队列", "必要时暂停试点，不能默认通过"])
note(1064, "授权人名单待确认", ["名单由业务负责人定 · 质量负责人核权限边界", "协调员落责任清单，工程师确认能否留痕"], tan=False)
note(1156, "对外口径", ["结果用途由客户协同代表确认后方可对外", "投诉先登记原始问题 + 版本，不推测客户意图"], tan=False)
note(1248, "三类人工审批点", ["正式结论确认 · 异常放行 · 跨企业资料访问", "异常放行是否单设审批点尚未核实"])

# ============================ 图例 ============================
add(f'<line x1="150" y1="1478" x2="1130" y2="1478" stroke="#E7EDF4" stroke-width="1.2"/>')
ly = 1498
lang = 11.5
items = [
    ("最耗时", "费时又费钱：取证与双人核对，异常对账约 55 分钟 / 份，占主要工时（月模拟 160 份为虚构基线）"),
    ("必须人工", "10 处关键动作不可自动化：登记 · 取证 · 映射 · 交接 · 核对 · 重复判断 · 版本判断 · 正式确认 · 审批放行 · 客户确认"),
    ("耗时", "质量检查执行、结果出具、归档留痕：单步耗时低，可交系统自动完成"),
    ("等待", "等待授权人签字 / 审批：属外部等待，不压缩、不代签、不并行跳过"),
]
for i, (k, txt) in enumerate(items):
    y = ly + i * 30
    pill(150, y - 14, k, k, fixed_w=76)
    add(f'<text x="240" y="{y}" font-size="{lang}" fill="{C_SUB}">{txt}</text>')

add(f'<text x="150" y="{ly+122}" font-size="{lang}" fill="#B4232A">'
    f'红线：不自动付款 · 不修改已确认账单 · 疑似重复项不自动删除 · 跨企业资料只按明确授权范围访问</text>')
add(f'<text x="150" y="{ly+150}" font-size="11" fill="#93A6B8">'
    f'依据 discovery-report.md 访谈整理：全部为演练假设与合成样本，需人工确认后方可进入试点</text>')

add('</g></svg>')

svg = "\n".join(out)
with open("flow.svg", "w", encoding="utf-8") as f:
    f.write(svg)

html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>《贸易服务结算与费用追溯》作业流程图</title>
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
with open("flow.html", "w", encoding="utf-8") as f:
    f.write(html)
print("ok", len(svg))
