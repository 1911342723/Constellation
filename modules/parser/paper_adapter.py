"""
PaperEditor 适配器 - 纯粹的格式转换层

这个文件的唯一职责：
把 Cursor-Caliper 的 DocumentTree（LLM 已经识别完毕的结构）
转换为前端 PaperEditor 需要的 PaperData 格式。

它 **不做** 任何结构识别、样式检测、特征嗅探！
所有的"哪里是标题、哪里是正文"的判断，全部交给 LLM。
这就是游标卡尺算法的核心原则：
  - LLM 只负责定位边界（输出 block_id）
  - Python 代码只负责无损搬运和格式转换
"""
import uuid
from typing import List, Dict, Any


class PaperEditorAdapter:
    """
    适配器：将 CaliperParser 的输出转换为 PaperData 格式
    
    这是一个纯粹的格式转换层，不包含任何智能逻辑。
    所有结构识别由 CaliperParser（LLM 游标漫游）完成。
    """
    
    def __init__(self):
        pass

    def from_paper_data(self, paper_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        直接透传 paper_data（已经是正确格式）
        
        当调用方已经通过 DocumentTree.to_paper_data() 获取了正确格式时，
        直接返回即可。
        
        Args:
            paper_data: DocumentTree.to_paper_data() 的输出
            
        Returns:
            PaperData 字典（原样返回）
        """
        # 确保每个 section 都有 id
        self._ensure_section_ids(paper_data.get("sections", []))
        return paper_data
    
    def from_sections_list(
        self, 
        sections: List[Dict[str, Any]], 
        title: str = "", 
        authors: str = ""
    ) -> Dict[str, Any]:
        """
        从 DocumentTree.to_markdown_sections() 的输出构建 PaperData
        
        Args:
            sections: [{title, content, section_type, level}, ...] 列表
            title: 文档标题
            authors: 文档作者
            
        Returns:
            PaperData 字典
        """
        paper_sections = []
        
        for sec in sections:
            paper_sections.append({
                "id": str(uuid.uuid4()),
                "title": sec.get("title", "未命名章节"),
                "content": sec.get("content", ""),
                "type": sec.get("section_type", "section"),
                "level": sec.get("level", 1),
                "subsections": [],
            })
        
        return {
            "title": title,
            "authors": authors,
            "sections": paper_sections,
        }
    
    def from_caliper_result(
        self, 
        blocks: List[Dict], 
        chapters: List[Dict],
        doc_title: str = "",
        doc_authors: str = "",
        filename: str = "",
    ) -> Dict[str, Any]:
        """
        从 CaliperAlgorithm 的原始结果构建 PaperData
        
        这是 backend caliper.py 会用到的入口：
        接收原始 blocks + LLM 返回的 chapters 锚点列表，
        进行强制闭合切割 + 无损内容组装 + 树状构建。
        
        Args:
            blocks: 原始 Block 字典列表
            chapters: LLM 标注的章节锚点 [{block_id, title, level}, ...]
            doc_title: LLM 识别的文档标题
            doc_authors: LLM 识别的文档作者
            filename: 文件名（兜底标题）
            
        Returns:
            PaperData 字典
        """
        if not chapters:
            # LLM 未识别到任何章节 → 全文作为一个 section
            content = self._assemble_all_content(blocks)
            fallback_title = doc_title or filename.replace('.docx', '').replace('.doc', '') or "文档正文"
            return {
                "title": fallback_title,
                "authors": doc_authors or "未知作者",
                "sections": [{
                    "id": str(uuid.uuid4()),
                    "title": fallback_title,
                    "content": content,
                    "type": "section",
                    "level": 1,
                    "subsections": [],
                }]
            }
        
        # 按 block_id 排序
        chapters = sorted(chapters, key=lambda x: x.get("block_id", 0))
        
        # 构建 block_id → block 映射
        block_map = {b.get("id"): b for b in blocks}
        max_block_id = max(b.get("id", 0) for b in blocks) if blocks else 0
        
        # ===== 强制闭合切割 + 无损组装 =====
        flat_sections = []
        
        for i, chapter in enumerate(chapters):
            start_id = chapter.get("block_id", 0)
            title = chapter.get("title", "未命名章节")
            level = chapter.get("level", 1)
            
            # 强制闭合
            end_id = chapters[i + 1].get("block_id", 0) - 1 if i + 1 < len(chapters) else max_block_id
            if end_id < start_id:
                end_id = start_id
            
            # 提取内容（跳过标题行）
            content_parts = []
            for bid in range(start_id, end_id + 1):
                if bid not in block_map:
                    continue
                block = block_map[bid]
                if bid == start_id and block.get("type") == "text":
                    continue  # 跳过标题行
                md = self._block_to_markdown(block)
                if md:
                    content_parts.append(md)
            
            flat_sections.append({
                "id": str(uuid.uuid4()),
                "title": title,
                "content": "\n\n".join(content_parts),
                "type": self._infer_type(title),
                "level": level,
                "subsections": [],
            })
        
        # ===== 栈式树状构建 =====
        root_sections = []
        stack = []  # [(level, section)]
        
        for section in flat_sections:
            level = section["level"]
            while stack and stack[-1][0] >= level:
                stack.pop()
            if not stack:
                root_sections.append(section)
            else:
                stack[-1][1]["subsections"].append(section)
            stack.append((level, section))
        
        final_title = doc_title or filename.replace('.docx', '').replace('.doc', '')
        
        return {
            "title": final_title,
            "authors": doc_authors or "未知作者",
            "sections": root_sections,
        }
    
    # ===== 内部工具方法 =====
    
    def _block_to_markdown(self, block: Dict) -> str:
        """把单个 block 字典无损转为 Markdown"""
        btype = block.get("type")
        if btype == "text":
            return block.get("text", "") or ""
        elif btype == "table":
            return block.get("text", "") or ""
        elif btype == "image":
            caption = block.get("caption", "图片")
            data = block.get("image_data", "image")
            return f"![{caption}]({data})"
        elif btype == "formula":
            text = block.get("text", "")
            return f"$$ {text} $$" if text else ""
        return ""
    
    def _assemble_all_content(self, blocks: List[Dict]) -> str:
        """将所有 blocks 组装为一段 Markdown"""
        parts = [self._block_to_markdown(b) for b in blocks]
        return "\n\n".join(p for p in parts if p)
    
    def _infer_type(self, title: str) -> str:
        """推断章节类型"""
        lower = (title or "").lower().strip()
        if any(kw in lower for kw in ["abstract", "摘要"]):
            return "abstract"
        elif any(kw in lower for kw in ["reference", "参考文献"]):
            return "reference"
        elif any(kw in lower for kw in ["appendix", "附录"]):
            return "appendix"
        elif any(kw in lower for kw in ["acknowledgment", "致谢"]):
            return "acknowledgment"
        return "section"
    
    def _ensure_section_ids(self, sections: List[Dict]):
        """递归确保每个 section 都有 id"""
        for sec in sections:
            if "id" not in sec:
                sec["id"] = str(uuid.uuid4())
            if "subsections" in sec:
                self._ensure_section_ids(sec["subsections"])
