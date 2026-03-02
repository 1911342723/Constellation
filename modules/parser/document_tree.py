"""
文档树 (Document Tree) - 游标卡尺最终输出

封装文档结构，提供多种输出格式：
1. to_json() — 完整的 JSON 树结构
2. to_markdown() — 单个完整的 Markdown 文档
3. to_markdown_sections() — 按章节分割的多个 Markdown 文档（核心需求！）
4. to_paper_data() — 适配文章排版系统的 PaperData 格式

"按章节分割为多个 Markdown 文档" 是最终交付的核心形式：
每个一级章节生成一个独立的 Markdown 文件，便于后续编辑、AI 处理和排版。
"""
from typing import List, Dict, Any, Optional
import json
import uuid
from modules.parser.schemas import DocumentNode
import logging

logger = logging.getLogger(__name__)


class DocumentTree:
    """
    文档树
    
    游标卡尺算法的最终产物，封装了完整的文档层级结构。
    支持多种输出格式，核心输出是 "多个结构化 Markdown 文档"。
    """
    
    def __init__(
        self, 
        nodes: List[DocumentNode],
        doc_title: str = "",
        doc_authors: str = "",
        preamble_content: str = "",
    ):
        """
        初始化文档树
        
        Args:
            nodes: 文档树的顶级节点列表
            doc_title: 文档标题（由 LLM 从骨架中识别）
            doc_authors: 文档作者（由 LLM 从骨架中识别）
            preamble_content: 前置内容（第一个章节之前的元信息）
        """
        self.nodes = nodes
        self.doc_title = doc_title
        self.doc_authors = doc_authors
        self.preamble_content = preamble_content
    
    # ============================================================
    # 输出格式 1: JSON 树结构
    # ============================================================
    
    def to_json(self, indent: int = 2) -> str:
        """
        转换为 JSON 格式
        
        Returns:
            JSON 字符串
        """
        data = {
            "doc_title": self.doc_title,
            "doc_authors": self.doc_authors,
            "sections": [self._node_to_dict(node) for node in self.nodes]
        }
        return json.dumps(data, ensure_ascii=False, indent=indent)
    
    def to_dict(self) -> List[dict]:
        """
        转换为字典列表
        
        Returns:
            字典列表
        """
        return [self._node_to_dict(node) for node in self.nodes]
    
    def _node_to_dict(self, node: DocumentNode) -> dict:
        """递归将节点转为字典"""
        d = {
            "title": node.title,
            "level": node.level,
            "start_block_id": node.start_block_id,
            "end_block_id": node.end_block_id,
            "content": node.content,
            "section_type": node.section_type,
        }
        if node.children:
            d["children"] = [self._node_to_dict(child) for child in node.children]
        else:
            d["children"] = []
        return d
    
    # ============================================================
    # 输出格式 2: 单个完整 Markdown 文档
    # ============================================================
    
    def to_markdown(self) -> str:
        """
        转换为单个完整的 Markdown 文档
        
        Returns:
            完整的 Markdown 文本
        """
        lines = []
        
        # 文档标题
        if self.doc_title:
            lines.append(f"# {self.doc_title}")
            lines.append("")
        
        # 作者信息
        if self.doc_authors:
            lines.append(f"*{self.doc_authors}*")
            lines.append("")
        
        # 前置内容
        if self.preamble_content:
            lines.append(self.preamble_content)
            lines.append("")
        
        # 递归渲染所有节点
        for node in self.nodes:
            self._node_to_markdown(node, lines)
        
        return "\n".join(lines)
    
    def _node_to_markdown(self, node: DocumentNode, lines: List[str]):
        """递归将节点渲染为 Markdown"""
        # 标题（层级 +1 因为文档标题占了 #）
        heading_level = min(node.level + 1, 6)  # Markdown 最多 6 级
        lines.append(f"{'#' * heading_level} {node.title}")
        lines.append("")
        
        # 内容
        if node.content:
            lines.append(node.content)
            lines.append("")
        
        # 子节点
        for child in node.children:
            self._node_to_markdown(child, lines)
    
    # ============================================================
    # 输出格式 3: 按章节分割的多个 Markdown 文档（核心需求！）
    # ============================================================
    
    def to_markdown_sections(self) -> List[Dict[str, str]]:
        """
        按一级章节分割为多个独立的 Markdown 文档
        
        这是游标卡尺算法的核心交付形式！
        每个一级章节（level=1）生成一个独立的 Markdown 文档，
        包含该章节自身的内容和所有子章节。
        
        Returns:
            Markdown 文档列表，每项包含：
            - title: 章节标题
            - content: 完整的 Markdown 内容（含子章节）
            - section_type: 章节类型
            - level: 层级
        """
        sections = []
        
        for node in self.nodes:
            md_lines = []
            
            # 一级标题
            md_lines.append(f"# {node.title}")
            md_lines.append("")
            
            # 该章节自身的内容
            if node.content:
                md_lines.append(node.content)
                md_lines.append("")
            
            # 递归渲染子章节
            for child in node.children:
                self._render_child_section(child, md_lines, base_level=1)
            
            sections.append({
                "title": node.title,
                "content": "\n".join(md_lines),
                "section_type": node.section_type,
                "level": node.level,
            })
        
        logger.info(f"[DocumentTree] 分割为 {len(sections)} 个独立 Markdown 文档")
        for i, sec in enumerate(sections):
            logger.info(f"  [{i}] {sec['title']} ({sec['section_type']}, {len(sec['content'])} chars)")
        
        return sections
    
    def _render_child_section(self, node: DocumentNode, lines: List[str], base_level: int):
        """
        递归渲染子章节
        
        Args:
            node: 子节点
            lines: 输出行列表
            base_level: 父级基准层级（用于计算相对 # 数量）
        """
        # 相对层级（确保子章节从 ## 开始）
        relative_level = node.level - base_level + 1
        heading_level = min(max(relative_level + 1, 2), 6)
        
        lines.append(f"{'#' * heading_level} {node.title}")
        lines.append("")
        
        if node.content:
            lines.append(node.content)
            lines.append("")
        
        for child in node.children:
            self._render_child_section(child, lines, base_level)
    
    # ============================================================
    # 输出格式 4: PaperData 格式（适配文章排版系统）
    # ============================================================
    
    def to_paper_data(self) -> Dict[str, Any]:
        """
        转换为文章排版系统的 PaperData 格式
        
        这个格式直接对接前端的 PaperEditor 组件，
        包含 title、authors 和 sections 三个顶级字段。
        
        Returns:
            PaperData 字典
        """
        sections = []
        
        for node in self.nodes:
            section = self._node_to_paper_section(node)
            sections.append(section)
        
        return {
            "title": self.doc_title,
            "authors": self.doc_authors,
            "sections": sections,
        }
    
    def _node_to_paper_section(self, node: DocumentNode) -> Dict[str, Any]:
        """
        递归将 DocumentNode 转换为 PaperData 的 section 格式
        
        Args:
            node: 文档节点
            
        Returns:
            PaperData section 字典
        """
        section = {
            "id": str(uuid.uuid4()),
            "title": node.title,
            "content": node.content,
            "type": node.section_type,
            "level": node.level,
            "subsections": [],
        }
        
        for child in node.children:
            child_section = self._node_to_paper_section(child)
            section["subsections"].append(child_section)
        
        return section
    
    # ============================================================
    # 统计信息
    # ============================================================
    
    def get_stats(self) -> Dict[str, Any]:
        """
        获取文档树的统计信息
        
        Returns:
            统计字典
        """
        total_sections = 0
        total_content_chars = 0
        max_depth = 0
        
        def count_recursive(nodes: List[DocumentNode], depth: int):
            nonlocal total_sections, total_content_chars, max_depth
            for node in nodes:
                total_sections += 1
                total_content_chars += len(node.content)
                max_depth = max(max_depth, depth)
                count_recursive(node.children, depth + 1)
        
        count_recursive(self.nodes, 1)
        
        return {
            "doc_title": self.doc_title,
            "doc_authors": self.doc_authors,
            "top_level_sections": len(self.nodes),
            "total_sections": total_sections,
            "total_content_chars": total_content_chars,
            "max_depth": max_depth,
        }
