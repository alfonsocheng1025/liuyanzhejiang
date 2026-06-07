// Vercel Serverless Function —— 大屏的数据读取接口
// 优先读取 GitHub Actions 持续更新的 data 分支；失败则返回非 200，
// 前端会自动回落到部署内置的 ./data.json（种子底图）。
export default async function handler(req, res) {
  const owner = process.env.VERCEL_GIT_REPO_OWNER;
  const repo = process.env.VERCEL_GIT_REPO_SLUG;
  const branch = process.env.DATA_BRANCH || "data";
  // 可在 Vercel 环境变量里设 DATA_URL 直接指定，否则按当前仓库自动推断
  const url =
    process.env.DATA_URL ||
    (owner && repo
      ? `https://raw.githubusercontent.com/${owner}/${repo}/${branch}/data.json`
      : null);

  if (!url) return res.status(404).json({ error: "DATA_URL 未配置" });

  try {
    const r = await fetch(`${url}?t=${Date.now()}`, { cache: "no-store" });
    if (!r.ok) return res.status(r.status).json({ error: `upstream ${r.status}` });
    const txt = await r.text();
    res.setHeader("Content-Type", "application/json; charset=utf-8");
    // 边缘缓存 2 分钟，过期后台刷新；既减压上游又保持新鲜
    res.setHeader("Cache-Control", "public, s-maxage=120, stale-while-revalidate=600");
    return res.status(200).send(txt);
  } catch (e) {
    return res.status(502).json({ error: String(e) });
  }
}
