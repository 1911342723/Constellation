class CaliperBaseException(Exception):
    """Caliper 基础异常类"""
    def __init__(self, message: str):
        self.message = message
        super().__init__(self.message)


class ProviderError(CaliperBaseException):
    """文档提供者异常"""
    pass


class CompressorError(CaliperBaseException):
    """骨架压缩异常"""
    pass


class LLMRouterError(CaliperBaseException):
    """LLM 路由异常"""
    pass


class AssemblerError(CaliperBaseException):
    """文档组装异常"""
    pass


class ParserError(CaliperBaseException):
    """解析器异常"""
    pass
