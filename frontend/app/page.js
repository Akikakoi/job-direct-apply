"use client";

import { useEffect, useState } from "react";

const EXPLAIN_LABEL = { skill: "技能", city: "城市", exp: "经验", role: "岗位" };

function scoreClass(score) {
  if (score >= 0.7) return "score";
  if (score >= 0.4) return "score";
  return "score";
}

function scoreColor(score) {
  return score >= 0.7 ? "var(--ok)" : score >= 0.4 ? "var(--warn)" : "var(--muted)";
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

export default function Home() {
  const [file, setFile] = useState(null);
  const [resume, setResume] = useState(null);
  const [items, setItems] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [expanded, setExpanded] = useState(() => new Set());

  useEffect(() => {
    const saved = localStorage.getItem("resume_id");
    if (saved) loadRecommend(Number(saved));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function loadRecommend(resumeId) {
    setLoading(true);
    setError("");
    try {
      const resp = await fetch(`/api/recommend?resume_id=${resumeId}&limit=50`);
      if (!resp.ok) {
        if (resp.status === 404) {
          localStorage.removeItem("resume_id");
          setResume(null);
          return;
        }
        throw new Error(`推荐接口 ${resp.status}`);
      }
      const body = await resp.json();
      setResume({ id: resumeId });
      setItems(body.data.items);
      setTotal(body.data.total);
    } catch (err) {
      setError(String(err.message || err));
    } finally {
      setLoading(false);
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
      const resp = await fetch("/api/resumes", { method: "POST", body: form });
      const body = await resp.json();
      if (!resp.ok) throw new Error(body.detail || `上传失败 ${resp.status}`);
      const id = body.data.id;
      localStorage.setItem("resume_id", String(id));
      setResume({ id, profile: body.data.profile, source: body.data.source });
      await loadRecommend(id);
    } catch (err) {
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

  return (
    <div className="container">
      <h1>简历直达</h1>
      <p className="sub">上传简历，获取基于官方 ATS 职位的可解释排序推荐</p>

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
              简历 #{resume.id} ｜ 解析来源：{resume.source === "llm" ? "LLM 抽取 + 规则校验" : "规则解析（未配置 LLM key）"}
              {resume.profile.experience_years != null ? ` ｜ 经验 ${resume.profile.experience_years} 年` : ""}
              {resume.profile.edu_degree ? ` ｜ 学历 ${resume.profile.edu_degree}` : ""}
            </div>
            {resume.profile.skills?.length ? (
              <div className="chips">
                {resume.profile.skills.map((s) => (
                  <span key={s} className="chip">{s}</span>
                ))}
              </div>
            ) : null}
          </div>
        ) : null}
      </div>

      {error ? <div className="error">{error}</div> : null}

      {resume && !loading ? (
        <div className="card">
          <h2>推荐职位（共 {total} 个，按匹配度排序）</h2>
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
                  </div>
                </div>
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
    </div>
  );
}
