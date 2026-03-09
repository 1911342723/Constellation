from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    应用配置类
    所有配置必须通过此类访问，禁止使用 os.getenv
    """
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )
    
    # LLM 配置
    llm_model: str = "deepseek-chat"
    llm_api_key: str
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_temperature: float = 0.1
    llm_max_tokens: int = 4096
    
    # 骨架压缩配置
    skeleton_head_chars: int = 40          # I帧/P帧截断：头部保留字符数
    skeleton_tail_chars: int = 30          # I帧/P帧截断：尾部保留字符数
    skeleton_enable_rle: bool = True       # 是否启用游程编码折叠
    skeleton_rle_threshold: int = 3        # 连续多少个 P帧触发折叠
    skeleton_max_rle_group: int = 10       # 单次折叠最大合并数量
    
    # v2: 滑动窗口配置（超长文档分片）
    sliding_window_threshold: int = 500    # 超过多少个 Block 启用滑动窗口
    window_size: int = 300                 # 每个窗口包含的 Block 数量
    window_overlap: int = 50              # 窗口间重叠的 Block 数量
    
    # v2: 模糊锚定配置
    fuzzy_anchor_radius: int = 5           # 模糊锚定滑轨搜索半径 [-N, +N]
    fuzzy_min_similarity: float = 0.4      # 模糊匹配最低相似度阈值
    
    # 日志配置
    log_level: str = "INFO"
    
    # IP 限流配置
    rate_limit_max_requests: int = 10        # 每个 IP 每小时最大请求数
    rate_limit_window_seconds: int = 3600    # 限流窗口（秒）
    
    # 应用配置
    app_name: str = "Constellation"
    app_version: str = "0.2.0"


settings = Settings()
