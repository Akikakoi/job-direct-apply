"use client";

import { useCallback, useEffect, useState } from "react";

import { DEFAULT_LANG, readLangCookie, t } from "../i18n";

const WEIGHT_KEYS = ["skill", "city", "exp", "role"];
// 后端状态是 insufficient_sample，词典 key 是 ops_status_insufficient（不对齐会静默显示 key 本身）
const OPS_STATUS_KEYS = {
  ok: "ops_status_ok",
  warn: "ops_status_warn",
  insufficient_sample: "ops_status_insufficient",
};
const OPS_HOURS = [24, 168];
const STEP_OPTIONS = [
  [0.05, "t_step_005"],
  [0.1, "t_step_01"],
  [0.2, "t_step_02"],
  [0.5, "t_step_05"],
];

function pct(v) {
  return v == null ? "—" : `${(v * 100).toFixed(1)}%`;
}

function num(v, digits = 4) {
  return v == null ? "—" : v.toFixed(digits);
}

// 后端 500 返回纯文本时 resp.json() 会抛 "Unexpected token"，统一容错解析
async function toJson(resp) {
  const text = await resp.text();
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function Metric({ label, value, hint }) {
  return (
    <div className="metric">
      <div className="metric-value">{value}</div>
      <div className="metric-label">
        {label}
        {hint ? ` ｜ ${hint}` : ""}
      </div>
    </div>
  );
}

function WeightChips({ weights, tt }) {
  return (
    <div className="chips">
      {WEIGHT_KEYS.map((key) => (
        <span className="chip" key={key}>
          {tt(`weight_${key}`)} {weights[key]}
        </span>
      ))}
      <span className="chip">α {weights.alpha} ｜ β {weights.beta}</span>
    </div>
  );
}

// 月份柱条宽度：以当月最大值为 100%，非零但极小值给 2% 下限（否则细如发丝看不见）
function barWidth(count, max) {
  if (!max || !count) return 0;
  return Math.max(2, Math.round((count / max) * 100));
}

export default function Insights() {
  const [report, setReport] = useState(null);
  const [tuning, setTuning] = useState(null);
  const [season, setSeason] = useState(null);
  const [ops, setOps] = useState(null);
  const [opsHours, setOpsHours] = useState(24);
  const [region, setRegion] = useState("all");
  const [step, setStep] = useState(0.1);
  const [lang, setLang] = useState(DEFAULT_LANG);
  const [loading, setLoading] = useState(false);
  const [opsLoading, setOpsLoading] = useState(false);
  const [tuningLoading, setTuningLoading] = useState(false);
  const [seasonLoading, setSeasonLoading] = useState(false);
  const [error, setError] = useState("");

  // 语言由 cookie 决定（服务端已按同一 cookie 渲染 <html lang>，见 app/layout.js）
  useEffect(() => {
    setLang(readLangCookie());
  }, []);

  const tt = useCallback((key, vars) => t(lang, key, vars), [lang]);
  const opsStatus = useCallback(
    (status) => tt(OPS_STATUS_KEYS[status] || "ops_status_insufficient"),
    [tt]
  );

  useEffect(() => {
    (async () => {
      setLoading(true);
      try {
        const resp = await fetch("/api/insights/quality");
        const body = await toJson(resp);
        if (!resp.ok || !body?.data) throw new Error(body?.detail || `质量接口 ${resp.status}`);
        setReport(body.data);
      } catch (err) {
        setError(String(err.message || err));
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  // 运维监控看板（§14 ④）：窗口可切换；Redis 不可用时后端回 available=false，前端照实显示
  useEffect(() => {
    let alive = true;
    (async () => {
      setOpsLoading(true);
      try {
        const resp = await fetch(`/api/insights/ops?hours=${opsHours}`);
        const body = await toJson(resp);
        if (!resp.ok || !body?.data) throw new Error(body?.detail || `运维接口 ${resp.status}`);
        if (alive) setOps(body.data);
      } catch (err) {
        if (alive) setError(String(err.message || err));
      } finally {
        if (alive) setOpsLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, [opsHours]);

  // 招聘季画像（§12.6 P5 ⑤）：区域 + 语言都作为查询参数，后端回已本地化的标签
  useEffect(() => {
    let alive = true;
    (async () => {
      setSeasonLoading(true);
      try {
        const resp = await fetch(`/api/insights/seasonality?region=${region}&lang=${lang}`);
        const body = await toJson(resp);
        if (!resp.ok || !body?.data) throw new Error(body?.detail || `招聘季接口 ${resp.status}`);
        if (alive) setSeason(body.data);
      } catch (err) {
        if (alive) setError(String(err.message || err));
      } finally {
        if (alive) setSeasonLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, [region, lang]);

  async function runTuning() {
    setTuningLoading(true);
    setError("");
    try {
      const resp = await fetch(`/api/insights/tuning?step=${step}&top=5`);
      const body = await toJson(resp);
      if (!resp.ok || !body?.data) throw new Error(body?.detail || `调参接口 ${resp.status}`);
      setTuning(body.data);
    } catch (err) {
      setError(String(err.message || err));
    } finally {
      setTuningLoading(false);
    }
  }

  const maxMonth = season ? Math.max(...season.by_month.map((r) => r.count)) : 0;

  return (
    <div className="container">
      <h1>{tt("page_title")}</h1>
      <p className="sub">{tt("page_sub")}</p>

      <p style={{ marginBottom: 16 }}>
        <a className="link" href="/">
          {tt("nav_back")}
        </a>
      </p>

      {error ? <div className="error">{error}</div> : null}

      <div className="card">
        <h2>{tt("q_title")}</h2>
        {loading ? <div className="meta">{tt("loading")}</div> : null}
        {report ? (
          <>
            <div className="metrics">
              <Metric label={tt("m_total")} value={report.total_applications} />
              <Metric label={tt("m_coverage")} value={pct(report.feedback_coverage)} />
              <Metric label={tt("m_hit")} value={pct(report.hit_rate)} />
              <Metric
                label={tt("m_ndcg", { k: report.k })}
                value={num(report.ndcg_at_k)}
                hint={tt("m_ndcg_hint", { n: report.ndcg_resumes })}
              />
            </div>

            <div className="section-title">{tt("sec_outcome")}</div>
            {Object.keys(report.outcome_counts).length ? (
              Object.entries(report.outcome_counts).map(([outcome, n]) => (
                <div className="table-row" key={outcome}>
                  <span>{tt(`outcome_${outcome}`)}</span>
                  <span>{n}</span>
                </div>
              ))
            ) : (
              <div className="meta">{tt("empty_outcome")}</div>
            )}

            <div className="section-title">{tt("sec_score")}</div>
            {Object.keys(report.avg_final_score_by_outcome).length ? (
              Object.entries(report.avg_final_score_by_outcome).map(([outcome, score]) => (
                <div className="table-row" key={outcome}>
                  <span>{tt(`outcome_${outcome}`)}</span>
                  <span>{num(score)}</span>
                </div>
              ))
            ) : (
              <div className="meta">{tt("empty_score")}</div>
            )}
            <div className="meta" style={{ marginTop: 8 }}>
              {tt("score_caption")}
            </div>

            <div className="section-title">{tt("sec_weights")}</div>
            <WeightChips weights={report.weights} tt={tt} />
          </>
        ) : null}
      </div>

      <div className="card">
        <h2>{tt("ops_title")}</h2>
        <div className="meta">{tt("ops_sub")}</div>

        <div className="row" style={{ marginTop: 12 }}>
          <span className="meta">{tt("ops_window")}</span>
          <select
            value={opsHours}
            onChange={(e) => setOpsHours(Number(e.target.value))}
            style={{ padding: "4px 8px", borderRadius: 8, border: "1px solid var(--line)" }}
          >
            {OPS_HOURS.map((h) => (
              <option value={h} key={h}>
                {tt(`ops_h${h}`)}
              </option>
            ))}
          </select>
        </div>

        {opsLoading && !ops ? <div className="meta" style={{ marginTop: 12 }}>{tt("loading")}</div> : null}

        {ops ? (
          <>
            <div className="metrics" style={{ marginTop: 14 }}>
              <Metric
                label={tt("ops_m_attempts")}
                value={ops.collection.attempts}
                hint={opsStatus(ops.collection.status)}
              />
              <Metric label={tt("ops_m_success")} value={pct(ops.collection.success_rate)} />
              <Metric
                label={tt("ops_m_queue")}
                value={
                  ops.backlog.task_queue.available
                    ? ops.backlog.task_queue.queue
                    : tt("ops_queue_na")
                }
              />
              <Metric label={tt("ops_m_pending")} value={ops.backlog.applications.pending} />
              <Metric
                label={tt("ops_m_stale")}
                value={ops.backlog.applications.stale}
                hint={tt("ops_over_days", { d: ops.backlog.applications.stale_after_days })}
              />
              <Metric
                label={tt("ops_m_hit")}
                value={pct(ops.quality.hit_rate)}
                hint={tt("ops_sample", { n: ops.quality.responded })}
              />
              <Metric
                label={tt("ops_m_ndcg", { k: ops.quality.k })}
                value={num(ops.quality.ndcg_at_k)}
              />
            </div>

            <div className="section-title">{tt("ops_alerts")}</div>
            {ops.alerts.length ? (
              ops.alerts.map((a) => (
                <div className="table-row" key={a.code}>
                  <span>
                    [{tt(`ops_level_${a.level}`)}] {a.message}
                  </span>
                </div>
              ))
            ) : (
              <div className="meta">{tt("ops_no_alerts")}</div>
            )}

            {ops.notes.map((n, i) => (
              <div className="meta" key={i} style={{ marginTop: 8 }}>
                {tt("ops_note", { n })}
              </div>
            ))}
          </>
        ) : null}
      </div>

      <div className="card">
        <h2>{tt("s_title")}</h2>
        <div className="meta">{tt("s_sub")}</div>

        <div className="row" style={{ marginTop: 12 }}>
          <span className="meta">{tt("s_region")}</span>
          <select
            value={region}
            onChange={(e) => setRegion(e.target.value)}
            style={{ padding: "4px 8px", borderRadius: 8, border: "1px solid var(--line)" }}
          >
            <option value="all">{tt("region_all")}</option>
            <option value="cn">{tt("region_cn")}</option>
            <option value="overseas">{tt("region_overseas")}</option>
          </select>
        </div>

        {seasonLoading && !season ? <div className="meta" style={{ marginTop: 12 }}>{tt("loading")}</div> : null}

        {season ? (
          <>
            <div className="metrics" style={{ marginTop: 14 }}>
              <Metric label={tt("s_metric_dated")} value={season.scope.with_publish_date} />
              <Metric label={tt("s_metric_years")} value={season.scope.years_covered} />
              <Metric
                label={tt("s_metric_peak")}
                value={
                  season.season_summary.peak_months.length
                    ? season.season_summary.peak_months
                        .map((m) => season.by_month[m - 1].month_label)
                        .join(" / ")
                    : tt("s_none")
                }
              />
              <Metric
                label={tt("s_metric_current")}
                value={season.current.season_label || tt("s_season_unknown")}
                hint={season.current.month_label}
              />
            </div>

            <div className="section-title">{tt("s_month_dist")}</div>
            <div className="bars">
              {season.by_month.map((row) => (
                <div className="bar-row" key={row.month}>
                  <span className="bar-label">{row.month_label}</span>
                  <span className="bar-track">
                    <span
                      className={`bar${row.season === "peak" ? " peak" : row.season === "off" ? " off" : ""}`}
                      style={{ width: `${barWidth(row.count, maxMonth)}%` }}
                    />
                  </span>
                  <span className="bar-value">
                    {row.count}
                    {row.season === "peak" ? tt("s_peak_marker") : ""}
                  </span>
                </div>
              ))}
            </div>

            <div className="section-title">{tt("s_quarter_dist")}</div>
            {season.by_quarter.map((q) => (
              <div className="table-row" key={q.quarter}>
                <span>{q.quarter_label}</span>
                <span>{q.count}</span>
              </div>
            ))}

            <div className="meta" style={{ marginTop: 10 }}>
              {tt("s_advice", { n: season.current.advice })}
            </div>
            {season.notes.map((n, i) => (
              <div className="meta" key={i} style={{ marginTop: 6 }}>
                {tt("s_note", { n })}
              </div>
            ))}
          </>
        ) : null}
      </div>

      <div className="card">
        <h2>{tt("t_title")}</h2>
        <div className="row">
          <span className="meta">{tt("t_step")}</span>
          <select
            value={step}
            onChange={(e) => setStep(Number(e.target.value))}
            style={{ padding: "4px 8px", borderRadius: 8, border: "1px solid var(--line)" }}
          >
            {STEP_OPTIONS.map(([value, key]) => (
              <option value={value} key={key}>
                {tt(key)}
              </option>
            ))}
          </select>
          <button className="ghost" onClick={runTuning} disabled={tuningLoading}>
            {tuningLoading ? tt("t_running") : tt("t_run")}
          </button>
        </div>

        {tuning ? (
          <>
            <div className="metrics" style={{ marginTop: 14 }}>
              <Metric label={tt("m_resumes")} value={tuning.samples.resumes} />
              <Metric label={tt("m_jobs")} value={tuning.samples.jobs} />
              <Metric label={tt("m_baseline")} value={num(tuning.baseline.ndcg_at_k)} />
              <Metric label={tt("m_combos")} value={tuning.grid.combos} />
            </div>

            <div className="section-title">{tt("sec_baseline")}</div>
            <WeightChips weights={tuning.baseline.weights} tt={tt} />

            {tuning.best ? (
              <>
                <div className="section-title">
                  {tt("sec_suggest", {
                    base: num(tuning.baseline.ndcg_at_k),
                    best: num(tuning.best.ndcg_at_k),
                    gain: tuning.best.gain,
                  })}
                </div>
                <WeightChips weights={tuning.best.weights} tt={tt} />
                <div className="meta" style={{ marginTop: 8 }}>
                  {tt("t_adopt")}
                </div>
                <pre className="mono" style={{ marginTop: 8 }}>{tuning.best.env_snippet}</pre>
              </>
            ) : (
              <div className="meta" style={{ marginTop: 10 }}>{tt("t_no_suggest")}</div>
            )}

            {tuning.candidates.length ? (
              <>
                <div className="section-title">{tt("sec_candidates", { n: tuning.candidates.length })}</div>
                {tuning.candidates.map((c, i) => (
                  <div className="table-row" key={i}>
                    <span>
                      {WEIGHT_KEYS.map((k) => `${tt(`weight_${k}`)} ${c.weights[k]}`).join(" ｜ ")} ｜ α {c.weights.alpha}
                    </span>
                    <span>{num(c.ndcg_at_k)}</span>
                  </div>
                ))}
              </>
            ) : null}

            {tuning.notes.map((n, i) => (
              <div className="meta" key={i} style={{ marginTop: 8 }}>
                {tt("t_note", { n })}
              </div>
            ))}
          </>
        ) : (
          <div className="meta" style={{ marginTop: 10 }}>{tt("t_idle")}</div>
        )}
      </div>
    </div>
  );
}