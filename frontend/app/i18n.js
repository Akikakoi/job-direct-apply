// 看板多语言词典（§12.6 P5 ⑤）
//
// 取舍：只覆盖看板（/insights）与语言切换，不引 i18n 框架——本项目只有中英两套文案，
// 一个纯字典 + cookie 记忆语言就够；新增语言只需在 DICTS/LANGS 各加一条。
//
// 语言来源优先级：cookie（用户显式选过）> 默认中文。**不用 navigator.language 自动协商**：
// 看板常被分享给英文同事，链接/cookie 能"钉住"语言比隐式协商更可控（与后端 i18n.py 同口径）。
export const LANGS = [
  { code: "zh-CN", label: "中文" },
  { code: "en", label: "English" },
];

export const DEFAULT_LANG = "zh-CN";
export const LANG_COOKIE = "lang";

const ZH = {
  nav_back: "← 返回推荐首页",
  lang_label: "语言",
  page_title: "质量看板",
  page_sub: "投递反馈回灌 → 命中率 / 分数对照 / NDCG@k，并给出匹配权重调参建议（只读，不自动改配置）",
  loading: "加载中…",

  q_title: "反馈质量",
  m_total: "投递总数",
  m_coverage: "反馈覆盖",
  m_hit: "命中率（面试+Offer）",
  m_ndcg: "排序质量 NDCG@{k}",
  m_ndcg_hint: "{n} 份简历",
  sec_outcome: "结果分布",
  empty_outcome: "暂无已回灌的结果反馈",
  sec_score: "分数对照（各结果组的 final_score 均值）",
  empty_score: "无可关联打分的反馈",
  score_caption: "口径：分高 → 结果好 若成立，面试/Offer 组均值应高于拒绝组。",
  sec_weights: "当前生效权重",

  outcome_interview: "面试",
  outcome_offer: "Offer",
  outcome_rejected: "拒绝",
  outcome_no_feedback: "无反馈",
  weight_skill: "技能",
  weight_city: "城市",
  weight_exp: "经验",
  weight_role: "岗位",

  t_title: "权重回归调参建议",
  t_step: "网格粒度",
  t_step_005: "0.05（细，组合多）",
  t_step_01: "0.1（默认）",
  t_step_02: "0.2",
  t_step_05: "0.5（粗）",
  t_run: "运行调参",
  t_running: "计算中…",
  m_resumes: "样本简历",
  m_jobs: "候选职位",
  m_baseline: "基线 NDCG",
  m_combos: "评估组合",
  sec_baseline: "基线（当前配置）",
  sec_suggest: "建议（NDCG {base} → {best}，+{gain}）",
  t_adopt: "采纳方式：人工确认后改 backend/.env 并重算 match_scores（接口不会自动写入）。",
  t_no_suggest: "未给出建议：维持现有权重。",
  sec_candidates: "候选 top{n}",
  t_note: "提示：{n}",
  t_idle: "点击「运行调参」后在权重网格上搜索 NDCG@k 最优组合；需要 ≥3 份有反馈简历才有结论。",

  s_title: "招聘季画像",
  s_sub: "按真实发布日看职位供给的旺淡季；只认 publish_date，不拿采集时间凑数",
  s_region: "区域",
  region_all: "全部",
  region_cn: "国内",
  region_overseas: "海外",
  s_metric_dated: "有发布日期",
  s_metric_years: "覆盖年份",
  s_metric_peak: "旺季月份",
  s_metric_current: "当前季节",
  s_month_dist: "月份分布",
  s_quarter_dist: "季度分布",
  s_peak_marker: "（旺季）",
  s_advice: "建议：{n}",
  s_note: "说明：{n}",
  s_season_unknown: "样本不足",
  s_none: "—",

  ops_title: "运维监控",
  ops_sub: "采集成功率 / 任务积压 / 命中率基线（§14 ④）",
  ops_window: "统计窗口",
  ops_h24: "24 小时",
  ops_h168: "7 天",
  ops_m_attempts: "采集尝试",
  ops_m_success: "采集成功率",
  ops_m_queue: "队列积压",
  ops_m_pending: "待跟进投递",
  ops_m_stale: "超期未更新",
  ops_m_hit: "命中率基线",
  ops_m_ndcg: "NDCG@{k}",
  ops_queue_na: "Redis 不可用",
  ops_over_days: "超 {d} 天",
  ops_sample: "有反馈样本 {n}",
  ops_status_ok: "正常",
  ops_status_warn: "告警",
  ops_status_insufficient: "样本不足",
  ops_alerts: "告警",
  ops_no_alerts: "无告警",
  ops_level_warn: "需处理",
  ops_level_info: "仅告知",
  ops_note: "提示：{n}",

  legal_terms_title: "用户协议",
  legal_privacy_title: "隐私政策",
  legal_link_terms: "用户协议",
  legal_link_privacy: "隐私政策",
  legal_version: "版本 v{v} ｜ 生效日 {d}",
  legal_back_home: "← 返回推荐首页",
};

const EN = {
  nav_back: "← Back to recommendations",
  lang_label: "Language",
  page_title: "Quality dashboard",
  page_sub: "Application feedback looped back → hit rate / score comparison / NDCG@k, plus matching-weight suggestions (read-only, never auto-applied)",
  loading: "Loading…",

  q_title: "Feedback quality",
  m_total: "Applications",
  m_coverage: "Feedback coverage",
  m_hit: "Hit rate (interview + offer)",
  m_ndcg: "Ranking quality NDCG@{k}",
  m_ndcg_hint: "{n} resumes",
  sec_outcome: "Outcome distribution",
  empty_outcome: "No outcome feedback recorded yet",
  sec_score: "Score comparison (mean final_score per outcome)",
  empty_score: "No scored feedback to correlate",
  score_caption: "Basis: if higher scores lead to better outcomes, interview/offer means should exceed the rejected mean.",
  sec_weights: "Active weights",

  outcome_interview: "Interview",
  outcome_offer: "Offer",
  outcome_rejected: "Rejected",
  outcome_no_feedback: "No feedback",
  weight_skill: "Skill",
  weight_city: "City",
  weight_exp: "Experience",
  weight_role: "Role",

  t_title: "Weight tuning suggestion",
  t_step: "Grid step",
  t_step_005: "0.05 (fine, many combos)",
  t_step_01: "0.1 (default)",
  t_step_02: "0.2",
  t_step_05: "0.5 (coarse)",
  t_run: "Run tuning",
  t_running: "Computing…",
  m_resumes: "Sample resumes",
  m_jobs: "Candidate jobs",
  m_baseline: "Baseline NDCG",
  m_combos: "Combos evaluated",
  sec_baseline: "Baseline (current config)",
  sec_suggest: "Suggestion (NDCG {base} → {best}, +{gain})",
  t_adopt: "How to adopt: confirm manually, edit backend/.env and recompute match_scores (the API never writes config).",
  t_no_suggest: "No suggestion: keep the current weights.",
  sec_candidates: "Top {n} candidates",
  t_note: "Note: {n}",
  t_idle: "Click “Run tuning” to search the weight grid for the best NDCG@k; needs ≥3 resumes with feedback.",

  s_title: "Hiring season profile",
  s_sub: "Peak/off-season job supply by real posting date; publish_date only, never crawl time",
  s_region: "Region",
  region_all: "All",
  region_cn: "China",
  region_overseas: "Overseas",
  s_metric_dated: "With publish date",
  s_metric_years: "Years covered",
  s_metric_peak: "Peak months",
  s_metric_current: "Current season",
  s_month_dist: "Monthly distribution",
  s_quarter_dist: "Quarterly distribution",
  s_peak_marker: " (peak)",
  s_advice: "Advice: {n}",
  s_note: "Note: {n}",
  s_season_unknown: "insufficient sample",
  s_none: "—",

  ops_title: "Ops monitoring",
  ops_sub: "Fetch success rate / task backlog / hit-rate baseline (§14 ④)",
  ops_window: "Window",
  ops_h24: "24 hours",
  ops_h168: "7 days",
  ops_m_attempts: "Fetch attempts",
  ops_m_success: "Fetch success rate",
  ops_m_queue: "Queue backlog",
  ops_m_pending: "Pending applications",
  ops_m_stale: "Stale (not updated)",
  ops_m_hit: "Hit-rate baseline",
  ops_m_ndcg: "NDCG@{k}",
  ops_queue_na: "Redis unavailable",
  ops_over_days: "> {d} days",
  ops_sample: "Feedback sample {n}",
  ops_status_ok: "OK",
  ops_status_warn: "Alert",
  ops_status_insufficient: "insufficient sample",
  ops_alerts: "Alerts",
  ops_no_alerts: "No alerts",
  ops_level_warn: "action needed",
  ops_level_info: "informational",
  ops_note: "Note: {n}",

  legal_terms_title: "Terms of Service",
  legal_privacy_title: "Privacy Policy",
  legal_link_terms: "Terms of Service",
  legal_link_privacy: "Privacy Policy",
  legal_version: "Version v{v} | Effective {d}",
  legal_back_home: "← Back to recommendations",
};

export const DICTS = { "zh-CN": ZH, en: EN };

export function normalizeLang(value) {
  return Object.prototype.hasOwnProperty.call(DICTS, value) ? value : DEFAULT_LANG;
}

// 取词：未知 key 回退显示 key 本身（漏词可见但不炸页面）；vars 做 {name} 替换
export function t(lang, key, vars) {
  const dict = DICTS[normalizeLang(lang)];
  const raw = dict[key] ?? DICTS[DEFAULT_LANG][key] ?? key;
  if (!vars) return raw;
  return raw.replace(/\{(\w+)\}/g, (match, name) =>
    vars[name] === undefined || vars[name] === null ? match : String(vars[name])
  );
}

// 客户端读语言 cookie（服务端渲染用 next/headers，见 app/layout.js）
export function readLangCookie() {
  if (typeof document === "undefined") return DEFAULT_LANG;
  const hit = document.cookie
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith(`${LANG_COOKIE}=`));
  return normalizeLang(hit ? decodeURIComponent(hit.slice(LANG_COOKIE.length + 1)) : DEFAULT_LANG);
}

// 服务端可读的 cookie 串同样格式，便于 <html lang> 与页面文案保持一致
export function langCookieValue(value) {
  return `${LANG_COOKIE}=${normalizeLang(value)}; path=/; max-age=31536000; samesite=lax`;
}