"""
Cursor-Caliper API Schemas
定义 API 请求和响应的数据模型
"""
from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional


class BlockSchema(BaseModel):
    """Block 数据模型"""
    id: int = Field(..., description="Block ID")
    type: str = Field(..., description="Block 类型: text, image, table, formula")
    text: Optional[str] = Field(None, description="文本内容")
    image_data: Optional[str] = Field(None, description="图片数据（Base64 或 URL）")
    caption: Optional[str] = Field(None, description="图片或表格标题")
    table_data: Optional[Dict[str, Any]] = Field(None, description="表格数据")
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict, description="额外元数据")
    # 物理特征字段
    is_bold: bool = Field(default=False, description="是否加粗")
    font_size: Optional[float] = Field(None, description="字号")
    alignment: Optional[str] = Field(None, description="对齐方式")
    is_heading_style: bool = Field(default=False, description="是否 Heading 样式")
    heading_level: Optional[int] = Field(None, description="Heading 层级")


class ParseRequest(BaseModel):
    """解析请求"""
    blocks: List[Dict[str, Any]] = Field(..., description="Block 列表")
    title: Optional[str] = Field(None, description="文档标题（可选，覆盖 LLM 识别的结果）")
    authors: Optional[str] = Field(None, description="作者信息（可选，覆盖 LLM 识别的结果）")


class SectionOutput(BaseModel):
    """分节 Markdown 输出"""
    title: str = Field(..., description="章节标题")
    content: str = Field(..., description="Markdown 内容")
    section_type: str = Field(default="section", description="章节类型")
    level: int = Field(default=1, description="层级")


class ParseResponse(BaseModel):
    """通用解析响应"""
    success: bool = Field(..., description="是否成功")
    document_tree: List[Dict[str, Any]] = Field(..., description="文档树结构")
    markdown: str = Field(..., description="完整 Markdown 格式")
    json_output: str = Field(..., description="JSON 格式", alias="json")
    sections: List[Dict[str, Any]] = Field(default_factory=list, description="分节 Markdown 列表")


class PaperParseResponse(BaseModel):
    """文章排版系统专用响应"""
    success: bool = Field(..., description="是否成功")
    paper_data: Dict[str, Any] = Field(..., description="PaperData 格式数据")


class DocxParseResponse(BaseModel):
    """DOCX Block extraction response (Stage 1 only)."""
    success: bool = Field(..., description="是否成功")
    blocks: List[Dict[str, Any]] = Field(..., description="Block 列表")
    filename: str = Field(..., description="原始文件名")
    total_blocks: int = Field(..., description="Block 总数")


class FullParseResponse(BaseModel):
    """全流程解析响应"""
    success: bool = Field(..., description="是否成功")
    doc_title: str = Field(default="", description="文档标题")
    doc_authors: str = Field(default="", description="文档作者")
    sections: List[Dict[str, Any]] = Field(..., description="分节 Markdown 列表")
    full_markdown: str = Field(..., description="完整 Markdown")
    paper_data: Dict[str, Any] = Field(..., description="PaperData 格式")
    stats: Dict[str, Any] = Field(..., description="统计信息")


class HealthResponse(BaseModel):
    """健康检查响应"""
    status: str = Field(..., description="服务状态")
    service: str = Field(..., description="服务名称")
    version: str = Field(..., description="版本号")
