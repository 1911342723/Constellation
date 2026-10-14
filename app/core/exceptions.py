class CaliperBaseException(Exception):
    """Base exception for all Constellation errors.

    每个异常携带一个稳定的机器可读 ``code``。R05：对外只能返回「稍后重试」式的模糊文案
    （交付层的全局处理器有意不外泄 ``message``，防止内部栈 / 依赖细节泄漏），但调用方
    需要能**编程判断**这次失败是哪一类、值不值得重试——靠匹配中文文案是脆的。``code`` 就是
    这个契约：文案可以改、可以本地化，``code`` 不变；它进响应体，``message`` 永远只进日志。
    """

    #: 稳定错误类型（子类覆盖）；进 HTTP 响应体供调用方分支，绝不随文案变化。
    code: str = "internal_error"

    def __init__(self, message: str):
        self.message = message
        super().__init__(self.message)


class ProviderError(CaliperBaseException):
    """Document provider error."""
    code = "provider_error"


class UnsupportedFormatError(ProviderError):
    """No provider is registered for the requested file format/suffix.

    A :class:`ProviderError` subtype so existing handlers (FastAPI's global
    ``ProviderError`` handler → HTTP 400) catch it transparently, while each
    delivery layer can still single it out for a cleaner ``400 / ToolError``.
    """
    code = "unsupported_format"


class CompressorError(CaliperBaseException):
    """Skeleton compression error."""
    code = "compressor_error"


class LLMRouterError(CaliperBaseException):
    """LLM routing error."""
    code = "llm_router_error"


class AssemblerError(CaliperBaseException):
    """Document assembly error."""
    code = "assembler_error"


class ParserError(CaliperBaseException):
    """Parser pipeline error."""
    code = "parser_error"


class ParseQueueFullError(CaliperBaseException):
    """解析排队队列已满（并发准入快速拒绝，映射 HTTP 503）。"""
    code = "queue_full"


class UploadTooLargeError(ProviderError):
    """上传体积 / 资源预算超限（R05：在把整份 payload 读进内存前就拦下）。

    是 :class:`ProviderError` 子类，因此现有全局处理器把它映射为 HTTP 400；但它带自己的
    ``code=upload_too_large``，调用方据此知道「不是文件坏了、是太大了」，无需解析文案。

    R05 的核心：UI 宣称「约 50MB」却从不真正强制——``await file.read()`` 先把整份读进内存
    才准入，排队请求各自持有整份 bytes。改为流式计数写受控临时文件，越过硬上限的瞬间就抛本
    异常中止，既真正强制上限、又把排队内存钉在上限以内。``message`` 面向用户可直接展示（说清
    是哪个预算、上限多少），不含内部细节，故与其它领域异常不同：它的 message 可以出网关。
    """
    code = "upload_too_large"
