# -*- coding: utf-8 -*-
"""切片与分词。

与阶段 1 的 rag_cli.py 是同一套算法，这里抽成模块供 ingest 与服务复用。
（阶段 1 保持单文件可独立运行，所以两边有一份等价的实现，这是刻意的取舍。）

切片策略（三级）：
    ① 按标题切段，保留标题路径作为来源定位的一半
    ② 同一小节内把小段落累积到接近 max_chars 再切
    ③ 跨小节立刻收口 —— 一个切片绝不横跨两个标题，否则 heading 会失真
"""

from __future__ import annotations

import re
from dataclasses import dataclass

try:
    import jieba
    HAS_JIEBA = True
except ImportError:          # 缺 jieba 时降级为单字 + 二元，仍可检索（精度下降）
    HAS_JIEBA = False


@dataclass
class Chunk:
    chunk_id: str
    text: str
    source_file: str
    heading: str
    char_start: int
    char_end: int

    def locator(self) -> str:
        """人类可读的来源定位串，直接进提示词，便于模型按编号引用。"""
        return f"{self.source_file} · {self.heading} · 第 {self.char_start}-{self.char_end} 字"


def tokenize(text: str) -> list[str]:
    """BM25 与 mock 向量共用。中文必须有分词，否则整段被当成一个 token。"""
    if HAS_JIEBA:
        toks = [t.strip() for t in jieba.lcut(text.lower()) if t.strip()]
    else:
        low = re.sub(r"\s+", "", text.lower())
        toks = list(low) + [low[i:i + 2] for i in range(len(low) - 1)]
    # 过滤纯标点，保留数字（金额、编号常含数字）
    return [t for t in toks if re.search(r"[\w\u4e00-\u9fa5]", t)]


def split_markdown(text: str, source: str,
                   max_chars: int = 500, overlap: int = 80) -> list[Chunk]:
    chunks: list[Chunk] = []
    heading_stack: list[tuple[int, str]] = []
    blocks: list[tuple[str, int]] = []      # (段落文本, 起始偏移)
    cur: list[str] = []
    cur_start = 0
    offset = 0

    def flush() -> None:
        nonlocal cur, cur_start
        if cur:
            blocks.append(("\n".join(cur).strip(), cur_start))
            cur = []

    # ---- ① 扫描全文，记录标题路径与每段偏移 ----
    for line in text.splitlines(keepends=True):
        raw = line.rstrip("\n").rstrip("\r")
        m = re.match(r"^(#{1,6})\s+(.*)$", raw)
        if m:
            flush()
            level = len(m.group(1))
            heading_stack = [h for h in heading_stack if h[0] < level]
            heading_stack.append((level, m.group(2).strip()))
            blocks.append((f"【{' > '.join(h[1] for h in heading_stack)}】", offset))
        elif raw.strip():
            if not cur:
                cur_start = offset
            cur.append(raw)
        else:
            flush()
        offset += len(line)
    flush()

    def cur_heading(at: int) -> str:
        path = ""
        for btext, boff in blocks:
            if boff > at:
                break
            if btext.startswith("【") and btext.endswith("】"):
                path = btext[1:-1]
        return path

    buf, buf_start, buf_heading = "", 0, ""

    def emit(txt: str, start: int, heading: str) -> None:
        if not txt.strip():
            return
        chunks.append(Chunk(
            chunk_id=f"c{len(chunks):05d}",
            text=txt.strip(),
            source_file=source,
            heading=heading,
            char_start=start,
            char_end=start + len(txt),
        ))

    # ---- ② 小节内累积；跨小节收口 ----
    for btext, boff in blocks:
        h = cur_heading(boff)
        if buf and h != buf_heading:
            emit(buf, buf_start, buf_heading)
            buf = ""

        piece = btext
        sentences = re.split(r"(?<=[。！？；\n])", piece) if len(piece) > max_chars else [piece]
        for s in sentences:
            if not s:
                continue
            if not buf:
                buf_start = boff if piece == s else boff + piece.find(s)
                buf_heading = h
            if len(buf) + len(s) <= max_chars:
                buf += s
            else:
                if buf:
                    emit(buf, buf_start, buf_heading)
                    tail = buf[-overlap:] if overlap > 0 else ""
                    buf = tail
                    buf_start = buf_start + len(buf) - len(tail)
                buf += s
                while len(buf) > max_chars:          # 极端长句兜底硬切
                    emit(buf[:max_chars], buf_start, buf_heading)
                    buf = buf[max_chars - overlap:]
                    buf_start += max_chars - overlap
    if buf:
        emit(buf, buf_start, buf_heading)

    return chunks
