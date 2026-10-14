"""内联 data URI 图片的识别与外置（共享层）。

这组东西是纯函数 + 纯数据（data URI 正则、MIME→扩展名表、外置改写），导出图片和
把内联 base64 抽成内容寻址 blob 的调用方都从这里取，因此放在共享层，保证一张表只有
一处维护。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import re
from typing import Any, Dict, List, Tuple

from infrastructure.assets.store import mime_to_ext

# payload 部分放宽到「右括号 / 引号 / 空白之前的一切」：合法性交给 base64 严格
# 解码判定，保证任何形似 data URI 的片段都会被改写（合法→资产，非法→占位符），
# 绝不把 base64 原样留在文本里。
DATA_URI_RE = re.compile(r"data:(image/[A-Za-z0-9.+-]+);base64,([^)\s<>\"']*)")

#: 无法解码的 data URI 改写成这个占位符。留一个死引用比留一段 base64 更糟，
#: 但两者都比「原样留着让下游自己踩」好——下游拿到它至少知道这里本来有张图。
INVALID_IMAGE_REF = "invalid-image"

#: 落盘文件名里保留的哈希位数。**身份仍是完整 sha256**（manifest 的 ``sha256``
#: 字段、``asset://<id>`` 引用、AssetStore 的键都不截断），这里截断的只是文件名。
#:
#: 为什么必须截断：完整 64 位十六进制会让叶子名达到 78 字符
#: （``img_0001_<64>.png``）。叠上 ``<输出根>/<owner 目录>/.staging/<uuid>/images/``
#: 之后，在 Windows 未开启 LongPathsEnabled 的机器上整条路径会越过 MAX_PATH(260)，
#: 表现为写第一张图就 ``FileNotFoundError``——目录明明已经建好。已实测复现。
#:
#: 为什么 32 位够：128 bit 前缀的生日界约 2^64 张图，落盘去重（按叶子名判重用）
#: 不存在实际碰撞风险；而历史内联图片用的是 8 位短哈希，位数不同也不会撞上。
#: 不要再往下截——32 bit 的旧命名确实能把两张不同的图静默去重成一张。
ASSET_NAME_DIGEST_CHARS = 32


def asset_file_name(ordinal: int, digest: str, mime: str) -> str:
    """生成落盘/打包用的图片相对路径。``digest`` 传完整 sha256，函数内部截断。"""
    short = digest[:ASSET_NAME_DIGEST_CHARS]
    return f"images/img_{ordinal:04d}_{short}.{mime_to_ext(mime)}"


def externalize_images(
    payload: Any,
) -> Tuple[Any, Dict[str, bytes], List[Dict[str, Any]]]:
    """递归改写 payload 中的全部图片 data URI 为相对路径引用。

    Returns:
        (改写后的 payload, {相对路径: 图片字节}, 清单)；清单项为
        ``{name, mime, size, sha256}``，顺序与首次出现顺序一致。
    """
    assets: Dict[str, bytes] = {}
    manifest: List[Dict[str, Any]] = []
    name_by_digest: Dict[str, str] = {}

    def _replace(match: re.Match) -> str:
        mime = match.group(1).lower()
        try:
            blob = base64.b64decode(match.group(2), validate=True)
        except (binascii.Error, ValueError):
            return INVALID_IMAGE_REF
        if not blob:
            return INVALID_IMAGE_REF
        digest = hashlib.sha256(blob).hexdigest()
        name = name_by_digest.get(digest)
        if name is None:
            # 身份用完整 digest（manifest.sha256），文件名按 asset_file_name 截断——
            # 完整 64 位会在 Windows 上撞 MAX_PATH，见该函数注释。
            name = asset_file_name(len(name_by_digest) + 1, digest, mime)
            name_by_digest[digest] = name
            assets[name] = blob
            manifest.append({"name": name, "mime": mime, "size": len(blob), "sha256": digest})
        return name

    def _walk(node: Any) -> Any:
        if isinstance(node, str):
            return DATA_URI_RE.sub(_replace, node)
        if isinstance(node, list):
            return [_walk(item) for item in node]
        if isinstance(node, dict):
            return {key: _walk(value) for key, value in node.items()}
        return node

    return _walk(payload), assets, manifest


def inline_image_stats(text: str) -> Tuple[int, int]:
    """统计文本中内联 data URI 图片的 ``(个数, 总字符数)``（观测告警用）。"""
    count = 0
    total_chars = 0
    for match in DATA_URI_RE.finditer(str(text or "")):
        count += 1
        total_chars += len(match.group(0))
    return count, total_chars


_ASSET_URI_RE = re.compile(r"asset://([0-9a-f]{64})")


def resolve_asset_refs(
    payload: Any, asset_store: Any, image_mode: str
) -> Tuple[Any, Dict[str, bytes], List[Dict[str, Any]]]:
    """把 payload 里的 ``asset://<id>`` 引用按 ``image_mode`` 解析成最终形态。

    Returns:
        (改写后的 payload, {相对路径: 字节}, 清单)。``inline`` 模式下不产生
        资产文件（字节编回 data URI），故后两项为空。

    未知的 asset_id（store 里查不到）改写为 ``invalid-image`` 占位，绝不把引用
    原样留给调用方——那会让下游拿到一个既非图片也非路径的死字符串。
    """
    if asset_store is None:
        return payload, {}, []

    assets: Dict[str, bytes] = {}
    manifest: List[Dict[str, Any]] = []
    name_by_id: Dict[str, str] = {}

    def _replace(match: re.Match) -> str:
        asset_id = match.group(1)
        record = asset_store.get(asset_id)
        if record is None:
            return INVALID_IMAGE_REF
        if image_mode == "inline":
            # 只在真正需要内联时才编一次 base64，且同一张图在一次响应里
            # 只编一次（name_by_id 兼作已编码缓存的键）。
            encoded = name_by_id.get(asset_id)
            if encoded is None:
                encoded = f"data:{record.mime};base64,{base64.b64encode(record.content).decode('ascii')}"
                name_by_id[asset_id] = encoded
            return encoded
        name = name_by_id.get(asset_id)
        if name is None:
            name = asset_file_name(len(name_by_id) + 1, asset_id, record.mime)
            name_by_id[asset_id] = name
            assets[name] = record.content
            manifest.append({
                "name": name,
                "mime": record.mime,
                "size": len(record.content),
                "sha256": asset_id,
            })
        return name

    def _walk(node: Any) -> Any:
        if isinstance(node, str):
            return _ASSET_URI_RE.sub(_replace, node)
        if isinstance(node, list):
            return [_walk(item) for item in node]
        if isinstance(node, dict):
            return {key: _walk(value) for key, value in node.items()}
        return node

    return _walk(payload), assets, manifest
