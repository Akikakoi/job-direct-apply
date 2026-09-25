"use client";

import { useEffect, useState } from "react";

import { authFetch } from "./auth";

const EXPLAIN_LABEL = { skill: "技能", city: "城市", exp: "经验", role: "岗位" };
const DEGREE_LABEL = { phd: "博士", master: "硕士", bachelor: "本科", associate: "大专" };

function scoreClass(score) {
  if (score >= 0.7) return "score";
  if (score >= 0.4) return "score";
  return "score";
}

function scoreColor(score) {
  return score >= 0.7 ? "var(--ok)" : score >= 0.4 ? "var(--warn)" : "var(--muted)";
}

// 后端 500 返回纯文本 "Internal Server Error"，直接 resp.json() 会抛
// "Unexpected token 'I'" 掩盖真实状态码；统一 text → JSON 容错解析。
async function toJson(resp) {
  const text = await resp.text();
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function ExplainItem({ e }) {
  const label = EXPLAIN_LABEL[e.key] || e.key;
  const pct = Math.round((e.score ?? 0) * 100);
  let detail = "";
  if (e.key === "skill") {
    if (e.hit) {
      detail = e.hit.length ? (
        <>
          命中：<span className="hit">{e.hit.join("、")}</span>
          {e.missing?.length ? (
            <>
              {"  "}缺失：<span className="missing">{e.missing.join("、")}</span>
            </>
          ) : null}
        </>
      ) : (
        "无命中"
      );
    } else {
      detail = "一方技能标签为空，按中性分计";
    }
  } else if (e.key === "city") {
    detail = e.note === "remote_ok"
      ? "职位支持远程"
      : e.note === "city_unknown_neutral"
        ? "职位城市未知"
        : e.job_city
          ? `职位地点：${e.job_city}`
          : "未匹配";
  } else if (e.key === "exp") {
    detail = e.note === "job_no_requirement"
      ? "职位无经验要求"
      : e.note === "resume_no_years"
        ? "简历未识别工作年限"
        : `职位要求 ${e.job_min} 年，简历 ${e.resume_years ?? "?"} 年`;
  } else if (e.key === "role") {
    detail = e.note === "no_target_role" ? "简历未填求职意向" : `目标「${e.target_role}」vs「${e.job_title}」`;
  }
  return (
    <div className="explain-item">
      <span className={`tag ${pct >= 70 ? "ok" : pct < 40 ? "low" : ""}`}>{label} {pct}%</span>
      <span>{detail}</span>
    </div>
  );
}

function Period({ start, end }) {
  const text = [start, end].filter(Boolean).join(" - ");
  return text ? <div className="meta">{text}</div> : null;
}

// 解析详情：教育经历 / 项目经历 / 实习工作经历（字段由 LLM 或规则兜底抽取）
function ResumeDetail({ profile }) {
  const { education, projects, internships } = profile;
  if (!education?.length && !projects?.length && !internships?.length) {
    return <div className="meta">本次解析未识别到院校 / 项目 / 实习经历</div>;
  }
  return (
    <div className="explain" style={{ marginTop: 8 }}>
      {education?.length ? (
        <div className="profile-block">
          <div className="profile-label">教育经历</div>
          {education.map((e, i) => (
            <div className="profile-item" key={i}>
              <div className="profile-title">
                {e.school}
                {e.major ? ` ｜ ${e.major}` : ""}
                {e.degree ? ` ｜ ${DEGREE_LABEL[e.degree] || e.degree}` : ""}
              </div>
              <Period start={e.start} end={e.end} />
              {e.highlights?.length ? <div className="meta">{e.highlights.join(" · ")}</div> : null}
            </div>
          ))}
        </div>
      ) : null}

      {projects?.length ? (
        <div className="profile-block">
          <div className="profile-label">项目经历</div>
          {projects.map((p, i) => (
            <div className="profile-item" key={i}>
              <div className="profile-title">
                {p.name}
                {p.role ? ` ｜ ${p.role}` : ""}
              </div>
              {p.tech?.length ? (
                <div className="chips" style={{ marginTop: 4 }}>
                  {p.tech.map((t) => (
                    <span className="chip" key={t}>{t}</span>
                  ))}
                </div>
              ) : null}
              {p.description ? <div className="meta" style={{ marginTop: 4 }}>{p.description}</div> : null}
              {p.links?.length ? (
                <div style={{ marginTop: 4 }}>
                  {p.links.map((l) => (
                    <a className="link" key={l} href={l} target="_blank" rel="noreferrer">
                      {l}
                    </a>
                  ))}
                </div>
              ) : null}
            </div>
          ))}
        </div>
      ) : null}

      {internships?.length ? (
        <div className="profile-block">
          <div className="profile-label">实习 / 工作经历</div>
          {internships.map((it, i) => (
            <div className="profile-item" key={i}>
              <div className="profile-title">
                {it.company}
                {it.title ? ` ｜ ${it.title}` : ""}
              </div>
              <Period start={it.start} end={it.end} />
              {it.description ? <div className="meta" style={{ marginTop: 4 }}>{it.description}</div> : null}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

// 投递前告知兜底文案（§14 ②）：/api/legal/policies 取不到时也必须有告知，不能静默投递
const FALLBACK_NOTICE = {
  title: "投递前告知",
  points: [
    "本次投递将跳转到用人单位的第三方招聘系统，简历与投递信息由你在该站点确认后提交。",
    "平台不会代填密码、不绕过登录或验证码，也不会替你点击「提交」。",
    "平台仅留存职位信息、简历画像与投递状态等最小必要数据，不对外售卖。",
    "你可以随时删除简历或投递记录以撤回授权。",
  ],
  consent_label: "我已阅读并同意《用户协议》《隐私政策》，并授权平台按上述告知发起本次投递",
};

export default function Home() {
  const [file, setFile] = useState(null);
  const [resume, setResume] = useState(null);
  const [items, setItems] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [expanded, setExpanded] = useState(() => new Set());
  const [applied, setApplied] = useState(() => new Set());
  const [apps, setApps] = useState([]);
  const [reminders, setReminders] = useState([]);
  const [region, setRegion] = useState("all"); // 岗位地区筛选：all=全部 | cn=国内 | overseas=国外
  const [showProfile, setShowProfile] = useState(false); // 简历解析详情（院校/项目/实习）折叠面板
  const [parsing, setParsing] = useState(false); // 后台异步解析中（§9 resume_parse_task）
  // §14 ② 投递授权：notice=后端告知文案与政策版本，pendingJob=等待用户勾选确认的职位
  const [notice, setNotice] = useState(null);
  const [pendingJob, setPendingJob] = useState(null);
  const [agreed, setAgreed] = useState(false);

  useEffect(() => {
    const saved = localStorage.getItem("resume_id");
    if (saved) loadRecommend(Number(saved));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 投递前告知与政策版本从后端取（单一事实源），失败时用内置兜底文案，不阻塞投递
  useEffect(() => {
    (async () => {
      try {
        const resp = await authFetch("/api/legal/policies");
        if (!resp.ok) return;
        const body = await toJson(resp);
        // 告知文案与当前政策版本一并保存：版本要原样回传给 POST /api/applications 留痕
        if (body?.data?.apply_notice) {
          setNotice({ ...body.data.apply_notice, version: body.data.policy_version });
        }
      } catch {
        /* 兜底文案已就位 */
      }
    })();
  }, []);

  async function loadSideData() {
    try {
      const [appsResp, remResp] = await Promise.all([
        authFetch("/api/applications?user_id=1&limit=50"),
        authFetch("/api/reminders?user_id=1"),
      ]);
      if (appsResp.ok) {
        setApps((await appsResp.json()).data.items);
      }
      if (remResp.ok) {
        setReminders((await remResp.json()).data.items);
      }
    } catch {
      /* 侧栏数据失败不阻塞主流程 */
    }
  }

  // 简历解析详情（院校/项目/实习等）单独取一次：/api/recommend 不返回 profile，
  // 页面刷新后靠它恢复展示，失败不阻塞推荐列表。
  async function loadResumeProfile(resumeId) {
    try {
      const resp = await authFetch(`/api/resumes/${resumeId}`);
      if (!resp.ok) return;
      const body = await resp.json();
      const profile = body?.data?.profile;
      if (profile) setResume({ id: resumeId, profile, source: profile.source });
    } catch {
      /* 详情失败不阻塞主流程 */
    }
  }

  // 异步解析（§9 resume_parse_task）：profile.parse_status=pending 时轮询详情，
  // 解析终态（done/failed）或超时后返回 profile，由调用方决定展示。
  async function waitForParse(resumeId, attempts = 20) {
    for (let i = 0; i < attempts; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 1500));
      try {
        const resp = await authFetch(`/api/resumes/${resumeId}`);
        if (!resp.ok) return null;
        const profile = (await resp.json())?.data?.profile;
        if (profile && profile.parse_status !== "pending") return profile;
      } catch {
        return null;
      }
    }
    return null;
  }

  async function loadRecommend(resumeId, regionValue = region) {
    setLoading(true);
    setError("");
    try {
      const resp = await authFetch(`/api/recommend?resume_id=${resumeId}&limit=50&region=${regionValue}`);
      if (!resp.ok) {
        if (resp.status === 404) {
          localStorage.removeItem("resume_id");
          setResume(null);
          return;
        }
        throw new Error(`推荐接口 ${resp.status}`);
      }
      const body = await resp.json();
      setResume((prev) => (prev?.id === resumeId && prev.profile ? prev : { id: resumeId }));
      setItems(body.data.items);
      setTotal(body.data.total);
      loadResumeProfile(resumeId);
      loadSideData();
    } catch (err) {
      setError(String(err.message || err));
    } finally {
      setLoading(false);
    }
  }

  // §14 ② 投递授权：先在站内展示告知并让用户显式勾选（不再"点击即视为授权"），
  // 再把当前政策版本随请求带上，服务端据此留痕 authorized_at / consent_version。
  function requestApply(job) {
    setError("");
    setPendingJob(job);
    setAgreed(false);
  }

  async function confirmApply(job) {
    setError("");
    try {
      const resp = await authFetch("/api/applications", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          user_id: 1,
          resume_id: resume?.id ?? null,
          job_id: job.job_id,
          authorized: agreed,
          consent_version: notice?.version ?? null,
        }),
      });
      const body = await toJson(resp);
      if (!resp.ok) throw new Error(body?.detail || `标记失败 ${resp.status}`);
      setApplied((prev) => new Set(prev).add(job.job_id));
      setPendingJob(null);
      setAgreed(false);
      loadSideData();
    } catch (err) {
      setError(String(err.message || err));
    }
  }

  async function handleUpload() {
    if (!file) return;
    setLoading(true);
    setError("");
    setItems([]);
    try {
      const form = new FormData();
      form.append("file", file);
      const resp = await authFetch("/api/resumes", { method: "POST", body: form });
      const body = await toJson(resp);
      if (!resp.ok) {
        throw new Error(body?.detail || `上传失败（服务端错误 ${resp.status}，请重试或查看后端日志）`);
      }
      if (!body?.data?.id) {
        throw new Error("上传响应异常：缺少简历 id");
      }
      const id = body.data.id;
      localStorage.setItem("resume_id", String(id));
      setResume({ id, profile: body.data.profile, source: body.data.source });
      // 异步解析（§9 resume_parse_task）：返回 parse_status=pending 时轮询到终态再拉推荐
      if (body.data.parse_status === "pending") {
        setParsing(true);
        const profile = await waitForParse(id);
        setParsing(false);
        if (profile) setResume({ id, profile, source: profile.source });
        else setError("解析仍在后台进行，稍后点「刷新推荐」查看结果");
      }
      await loadRecommend(id);
    } catch (err) {
      setParsing(false);
      setError(String(err.message || err));
      setLoading(false);
    }
  }

  function toggleExpand(jobId) {
    setExpanded((prev) => {
      const next = new Set(prev);
      next.has(jobId) ? next.delete(jobId) : next.add(jobId);
      return next;
    });
  }

  // 异步解析状态提示（§9）：pending=解析中、failed=解析失败，均为空时按正常结果展示
  const parseStatus = resume?.profile?.parse_status;
  const parseHint =
    parsing || parseStatus === "pending"
      ? `简历 #${resume.id} ｜ 解析中（后台任务），完成后自动刷新推荐…`
      : parseStatus === "failed"
        ? `简历 #${resume.id} ｜ 解析失败：${resume.profile.parse_error || "未知错误"}，请重新上传或人工修正`
        : null;

  return (
    <div className="container">
      <h1>简历直达</h1>
      <p className="sub" style={{ marginBottom: 6 }}>上传简历，获取基于官方 ATS 职位的可解释排序推荐</p>
      <p className="meta" style={{ marginBottom: 20 }}>
        <a className="link" href="/insights">
          查看质量看板（反馈命中率 / NDCG / 权重调参建议）→
        </a>
      </p>

      <div className="card">
        <h2>{resume ? "更换简历" : "第一步：上传简历"}</h2>
        <div className="row">
          <input
            type="file"
            accept=".txt,.md,.pdf"
            onChange={(e) => setFile(e.target.files?.[0] || null)}
          />
          <button onClick={handleUpload} disabled={!file || loading}>
            {loading ? "解析中…" : "上传并解析"}
          </button>
          {resume && !loading ? (
            <button className="ghost" onClick={() => loadRecommend(resume.id)}>
              刷新推荐
            </button>
          ) : null}
        </div>
        {resume?.profile ? (
          <div style={{ marginTop: 14 }}>
            <div className="meta" style={{ marginBottom: 6 }}>
              {parseHint ? (
                <span>{parseHint}</span>
              ) : (
                <>
                  简历 #{resume.id} ｜ 解析来源：{resume.source === "llm" ? "LLM 抽取 + 规则校验" : "规则解析（未配置 LLM key）"}
                  {resume.profile.experience_years != null ? ` ｜ 经验 ${resume.profile.experience_years} 年` : ""}
                  {resume.profile.edu_degree ? ` ｜ 学历 ${DEGREE_LABEL[resume.profile.edu_degree] || resume.profile.edu_degree}` : ""}
                  {resume.profile.target_role ? ` ｜ 意向 ${resume.profile.target_role}` : ""}
                </>
              )}
            </div>
            {resume.profile.skills?.length ? (
              <div className="chips">
                {resume.profile.skills.map((s) => (
                  <span key={s} className="chip">{s}</span>
                ))}
              </div>
            ) : null}
            <button
              className="ghost"
              style={{ marginTop: 10, padding: "3px 10px", fontSize: 12 }}
              onClick={() => setShowProfile((v) => !v)}
            >
              {showProfile ? "收起解析详情" : "查看解析详情（院校 / 项目 / 实习）"}
            </button>
            {showProfile ? <ResumeDetail profile={resume.profile} /> : null}
          </div>
        ) : null}
      </div>

      {error ? <div className="error">{error}</div> : null}

      {resume && !loading ? (
        <div className="card">
          <h2>
            推荐职位（共 {total} 个，
            {region === "all" ? "国内/国外交错展示，各路内按匹配度排序" : "按匹配度排序"}）
          </h2>
          <div className="row" style={{ marginBottom: 4 }}>
            <span className="meta">地区筛选</span>
            {[
              ["all", "全部"],
              ["cn", "国内"],
              ["overseas", "国外"],
            ].map(([value, label]) => (
              <button
                key={value}
                className={region === value ? undefined : "ghost"}
                onClick={() => {
                  setRegion(value);
                  loadRecommend(resume.id, value);
                }}
              >
                {label}
              </button>
            ))}
          </div>
          {items.length === 0 ? (
            <p className="meta">暂无数据</p>
          ) : (
            items.map((job) => (
              <div className="job" key={job.job_id}>
                <div className="job-head">
                  <div>
                    <div className="job-title">{job.title}</div>
                    <div className="meta">
                      {job.city || "地点未知"} ｜ 来源 {job.source}
                    </div>
                  </div>
                  <div>
                    <span className={scoreClass(job.score)} style={{ color: scoreColor(job.score) }}>
                      {job.score != null ? (job.score * 100).toFixed(0) + " 分" : "-"}
                    </span>
                    {"  "}
                    <a className="link" href={job.apply_url} target="_blank" rel="noreferrer">
                      直达申请 ↗
                    </a>
                    {"  "}
                    {applied.has(job.job_id) || apps.some((a) => a.job_id === job.job_id && a.status !== "closed" && a.status !== "rejected") ? (
                      <span className="meta">已投递</span>
                    ) : (
                      <button className="ghost" style={{ padding: "3px 10px", fontSize: 12 }} onClick={() => requestApply(job)}>
                        标记已投递
                      </button>
                    )}
                  </div>
                </div>
                {pendingJob?.job_id === job.job_id ? (
                  <div className="explain" style={{ marginTop: 8 }}>
                    <div className="profile-label">{(notice || FALLBACK_NOTICE).title}</div>
                    <ul style={{ margin: "6px 0 0 18px", padding: 0 }}>
                      {(notice || FALLBACK_NOTICE).points.map((p, i) => (
                        <li className="meta" key={i} style={{ marginBottom: 4 }}>
                          {p}
                        </li>
                      ))}
                    </ul>
                    <label style={{ display: "flex", gap: 8, alignItems: "flex-start", marginTop: 10 }}>
                      <input
                        type="checkbox"
                        checked={agreed}
                        onChange={(e) => setAgreed(e.target.checked)}
                        style={{ marginTop: 3, flex: "0 0 auto" }}
                      />
                      <span className="meta">{(notice || FALLBACK_NOTICE).consent_label}</span>
                    </label>
                    <div className="row" style={{ marginTop: 10 }}>
                      <button onClick={() => confirmApply(job)} disabled={!agreed}>
                        确认授权并标记已投递
                      </button>
                      <button
                        className="ghost"
                        onClick={() => {
                          setPendingJob(null);
                          setAgreed(false);
                        }}
                      >
                        取消
                      </button>
                      <span className="meta">
                        <a className="link" href="/legal/terms">
                          用户协议
                        </a>
                        {" ｜ "}
                        <a className="link" href="/legal/privacy">
                          隐私政策
                        </a>
                      </span>
                    </div>
                  </div>
                ) : null}
                {job.skills?.length ? (
                  <div className="chips" style={{ marginTop: 6 }}>
                    {job.skills.slice(0, 8).map((s) => (
                      <span key={s} className="chip">{s}</span>
                    ))}
                  </div>
                ) : null}
                <button className="ghost" style={{ marginTop: 8, padding: "3px 10px", fontSize: 12 }} onClick={() => toggleExpand(job.job_id)}>
                  {expanded.has(job.job_id) ? "收起匹配明细" : "匹配在哪 / 差在哪"}
                </button>
                {expanded.has(job.job_id) ? (
                  <div className="explain">
                    {job.explain?.map((e, i) => (
                      <ExplainItem key={i} e={e} />
                    ))}
                  </div>
                ) : null}
              </div>
            ))
          )}
        </div>
      ) : null}

      {apps.length || reminders.length ? (
        <div className="card">
          <h2>我的投递（{apps.length}）</h2>
          {reminders.length ? (
            <div className="error" style={{ marginBottom: 12 }}>
              催进提醒：{reminders.length} 个申请卡住超过 3 天（
              {reminders.map((r) => `#${r.application_id} ${r.job_title.slice(0, 18)} ${r.stuck_days}天`).join("；")}
              ），建议去 ATS 后台查看进度
            </div>
          ) : null}
          {apps.map((a) => (
            <div className="job" key={a.id}>
              <div className="job-head">
                <div>
                  <div className="job-title">{a.job_title}</div>
                  <div className="meta">
                    {a.city || ""} ｜ 投递于 {a.created_at.slice(0, 10)}
                  </div>
                </div>
                <div className="row">
                  <span className="chip">{a.status}</span>
                  <a className="link" href={a.apply_url} target="_blank" rel="noreferrer">
                    打开申请页 ↗
                  </a>
                  {a.status !== "closed" && a.status !== "rejected" ? (
                    <button
                      className="ghost"
                      style={{ padding: "3px 10px", fontSize: 12 }}
                      onClick={async () => {
                        setError("");
                        const resp = await authFetch(`/api/applications/${a.id}/autofill`, { method: "POST" });
                        const body = await resp.json();
                        if (!resp.ok) setError(body.detail || "帮填失败");
                      }}
                    >
                      半自动帮填
                    </button>
                  ) : null}
                </div>
              </div>
            </div>
          ))}
        </div>
      ) : null}

      <p className="meta" style={{ marginTop: 20 }}>
        <a className="link" href="/legal/terms">
          用户协议
        </a>
        {" ｜ "}
        <a className="link" href="/legal/privacy">
          隐私政策
        </a>
      </p>
    </div>
  );
}
