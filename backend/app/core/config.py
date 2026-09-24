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

    # P2 规则匹配引擎权重（§7 一期，sum=1；反馈数据回归调参后可改）
    match_w_skill: float = 0.5
    match_w_city: float = 0.2
    match_w_exp: float = 0.15
    match_w_role: float = 0.15

    # P4 语义融合：final = alpha*rule + beta*vec（§7 二期，alpha+beta=1）
    match_alpha: float = 0.75
    match_beta: float = 0.25

    # P3 投递催进：pending 状态卡超过 T 个自然日进入提醒（§8，T 天默认值挂账销项）
    reminder_after_days: int = 3

    # P3 半自动帮填：用户投递档案（.env 配置；单用户产品形态）
    autofill_name: str = ""
    autofill_email: str = ""
    autofill_phone: str = ""
    autofill_headless: bool = False  # 有头模式：用户人工核对后手动提交

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
