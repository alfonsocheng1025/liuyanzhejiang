// Vercel Serverless Function —— 读取 GitHub Actions 生成的 risk.json（社会风险预警）
// 与 /api/data 同机制；失败返回非 200，前端回落到 ./risk.json（部署内置种子）。
export default async function handler(req, res) {
  const owner = process.env.VERCEL_GIT_REPO_OWNER;
  const repo = process.env.VERCEL_GIT_REPO_SLUG;
  const branch = process.env.DATA_BRANCH || "data";
  const url =
    process.env.RISK_URL ||
    (owner && repo
      ? `https://raw.githubusercontent.com/${owner}/${repo}/${branch}/risk.json`
      : null);
  if (!url) return res.status(404).json({ error: "RISK_URL 未配置" });
  try {
    const r = await fetch(`${url}?t=${Date.now()}`, { cache: "no-store" });
    if (!r.ok) return res.status(r.status).json({ error: `upstream ${r.status}` });
    const txt = await r.text();
    res.setHeader("Content-Type", "application/json; charset=utf-8");
    res.setHeader("Cache-Control", "public, s-maxage=120, stale-while-revalidate=600");
    return res.status(200).send(txt);
  } catch (e) {
    return res.status(502).json({ error: String(e) });
  }
}
