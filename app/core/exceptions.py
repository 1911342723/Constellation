class CaliperBaseException(Exception):
    """Base exception for all Constellation errors."""
    def __init__(self, message: str):
        self.message = message
        super().__init__(self.message)


class ProviderError(CaliperBaseException):
    """Document provider error."""
    pass


class UnsupportedFormatError(ProviderError):
    """No provider is registered for the requested file format/suffix.

    A :class:`ProviderError` subtype so existing handlers (FastAPI's global
    ``ProviderError`` handler → HTTP 400) catch it transparently, while each
    delivery layer can still single it out for a cleaner ``400 / ToolError``.
    """
    pass


class CompressorError(CaliperBaseException):
    """Skeleton compression error."""
    pass


class LLMRouterError(CaliperBaseException):
    """LLM routing error."""
    pass


class AssemblerError(CaliperBaseException):
    """Document assembly error."""
    pass


class ParserError(CaliperBaseException):
    """Parser pipeline error."""
    pass


class ParseQueueFullError(CaliperBaseException):
    """解析排队队列已满（并发准入快速拒绝，映射 HTTP 503）。"""
    pass
