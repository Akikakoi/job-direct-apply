from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全局配置：环境变量 / .env 覆盖默认值。"""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # SQLite 开箱即用；生产切 PostgreSQL（需另装 psycopg2-binary）
    database_url: str = "sqlite:///./dev.db"
    # 空 = 不启用 Redis（限流退化为 DB 间隔守卫，别名缓存退化为直查 DB）
    redis_url: str = ""

    # skill_tags 别名映射的 Redis 缓存 TTL（秒）；skill_tags 有维护动作后等 TTL 或手动清 key
    alias_cache_ttl_s: int = 600

    http_timeout_s: int = 30
    ua: str = "job-direct-apply-bot/0.1 (compliant; contact: dev@example.com)"

    fetch_default_interval_min: int = 360   # 海外公开 API 默认抓取间隔
    official_site_interval_min: int = 720   # 官网兜底更保守
    ttl_multiplier: int = 3                 # 连续 N 个采集周期未见更新 → expired

    # §4.3 账号体系（JWT，HS256，标准库实现；见 app/services/auth.py）
    # 生产必须换掉默认值：python -c "from app.services.auth import new_secret; print(new_secret())"
    auth_secret: str = "dev-insecure-secret-change-me"
    auth_access_ttl_min: int = 120          # access 令牌有效期（分钟）
    auth_refresh_ttl_days: int = 14         # refresh 令牌有效期（天）
    # 默认关：表单/查询参数传 user_id 的旧客户端不被拦（鉴权是"可开可关"的一层，
    # 不是硬依赖）；开=用户态接口必须带 Bearer，缺失一律 401。
    auth_required: bool = False

    # §9 后台任务：异步化开关（默认关——开发期无 Redis/worker，走请求内同步）
    resume_parse_async: bool = False        # 上传后异步解析（resume_parse_task）
    match_async: bool = False               # 画像修改后异步重算（run_match_task）
    idle_jobs_ttl_days: int = 14            # 每日全局 TTL 下架阈值（idle_jobs_cleanup）

    workday_max_total: int = 2000           # Workday 单站点单次搜索封顶
    workday_max_pages: int = 100            # 翻页安全上限（20/页 × 100 = 2000）

    # SmartRecruiters 详情补齐（N+1，默认关；开=每次采集最多补 sr_detail_cap 条）
    sr_fetch_details: bool = False
    sr_detail_cap: int = 200

    # §12.7 #7 采集入库抽标签：词典扫描永远开（零成本）；LLM 兜底按成本开关（需 llm_api_key）
    job_tag_llm: bool = False
    job_tag_llm_cap: int = 50               # 每轮采集最多 LLM 抽取的职位数（成本上限）

    # P2 简历解析：LLM 抽取（DeepSeek / OpenAI 兼容接口）。key 为空走规则兜底
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-chat"
    llm_timeout_s: int = 60
    uploads_dir: str = "./uploads"          # 简历原件存盘目录
    # §10 简历原件静态加密（AES-GCM，应用层密钥；见 app/services/crypto.py）
    # 空 = 不加密（旧行为，prodcheck 会 warn「原件明文落盘」）；生成：
    #   python -c "from app.services.crypto import new_key; print(new_key())"
    uploads_key: str = ""

    # P2 规则匹配引擎权重（§7 一期，sum=1；反馈数据回归调参后可改）
    match_w_skill: float = 0.5
    match_w_city: float = 0.2
    match_w_exp: float = 0.15
    match_w_role: float = 0.15

    # P4 语义融合：final = alpha*rule + beta*vec（§7 二期，alpha+beta=1）
    match_alpha: float = 0.75
    match_beta: float = 0.25

    # §12.5 反馈回灌闭环：权重自动采纳（默认关——自动改线上排序不能默认开）
    # 生效权重 = match_weight_versions 最新一行；本组参数只控制"自动采纳"的门槛与节奏
    weights_auto_tune: bool = False          # 开=beat 按周评估并采纳优于基线的权重
    weights_auto_tune_margin: float = 0.02   # NDCG 提升门槛（低于此视为噪声，不换参数）
    weights_auto_tune_min_sample: int = 10   # 参与自动采纳所需的最小有反馈简历数
    weights_auto_tune_step: float = 0.1      # 网格粒度（与 /api/insights/tuning 同义）
    weights_auto_tune_k: int = 10            # 评估用的 NDCG@k

    # P3 投递催进：pending 状态卡超过 T 个自然日进入提醒（§8，T 天默认值挂账销项）
    reminder_after_days: int = 3

    # P3 半自动帮填：用户投递档案（.env 配置；单用户产品形态）
    autofill_name: str = ""
    autofill_email: str = ""
    autofill_phone: str = ""
    autofill_headless: bool = False  # 有头模式：用户人工核对后手动提交

    # §10 可观测性（app/services/obs.py）：日志/指标默认开、JSON 日志与 Sentry 默认关
    log_json: bool = False                  # true = 一行一个 JSON（供 Loki/ES；开发期控制台保持可读）
    metrics_enabled: bool = True            # GET /metrics（Prometheus 文本格式）
    metrics_token: str = ""                 # 非空则 /metrics 必须带同值令牌（Prometheus 通常无凭据，默认不强制）
    metrics_ops_window_hours: int = 24      # 抓取时用 ops 口径算业务指标的窗口
    sentry_dsn: str = ""                    # 非空且装了 sentry-sdk 才初始化，否则静默跳过
    sentry_environment: str = "production"

    # P4 催进邮件通知：smtp_host 空 = 不发邮件（beat 只打日志）
    smtp_host: str = ""                     # 如 smtp.qq.com
    smtp_port: int = 465                    # QQ Mail SSL
    smtp_user: str = ""                     # 发件邮箱
    smtp_password: str = ""                 # SMTP 授权码（非登录密码）
    notify_email: str = ""                  # 收件邮箱（催进提醒接收方）

    # P4 扫描件 PDF OCR：OpenAI 兼容视觉 API（如 DashScope qwen-vl）。key 空 = 不支持扫描件
    ocr_api_key: str = ""
    ocr_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    ocr_model: str = "qwen-vl-plus"
    ocr_max_pages: int = 5                  # 最多渲染页数（成本控制）

    # P4 IM webhook（钉钉/企微群机器人）：type 空 = 不推 IM（与邮件互相独立）
    im_webhook_type: str = ""               # "dingtalk" | "wecom"
    im_webhook_url: str = ""                # 机器人 webhook 地址
    im_webhook_secret: str = ""             # 钉钉加签 secret（企微留空）


settings = Settings()
