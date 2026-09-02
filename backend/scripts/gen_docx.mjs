// 舆情报告 docx 生成器
// 严格对齐《参考格式.docx》模板规格（已从模板 XML 解析）：
//   标题   22pt 加粗 居中
//   章节   黑体 16pt（不加粗，黑体本身厚重）
//   正文   仿宋 16pt，首行缩进 2 字符(640twips)，两端对齐，固定行距 560/EXACT(28pt)
//   句首   分论点/对策短语加粗 + 后续正文
// 用法: node gen_docx.mjs <report.json> <out.docx>
import { readFileSync, writeFileSync } from "node:fs";
import { Document, Packer, Paragraph, TextRun, ImageRun, AlignmentType, LineRuleType } from "docx";

const [, , inJson, outDocx] = process.argv;
if (!inJson || !outDocx) {
  console.error("用法: node gen_docx.mjs <report.json> <out.docx>");
  process.exit(1);
}

const FANGSONG = { ascii: "仿宋", eastAsia: "仿宋", hAnsi: "仿宋" };
const HEITI = { ascii: "黑体", eastAsia: "黑体", hAnsi: "黑体" };

// 正文段落：首行缩进 2 字符（16pt x 2 = 640 twips），两端对齐，固定行距 28pt
function bodyParagraph(runs, opts = {}) {
  return new Paragraph({
    alignment: AlignmentType.BOTH,
    indent: { firstLine: 640 },
    spacing: { line: 560, lineRule: LineRuleType.EXACT },
    children: runs,
    ...opts,
  });
}

function bodyRun(text, { bold = false, font = FANGSONG, size = 32 } = {}) {
  return new TextRun({ text, bold, font, size });
}

const stanceCN = (s) => (s === "positive" ? "正面" : s === "negative" ? "负面" : "中立");

function sectionHeading(text) {
  return new Paragraph({
    spacing: { line: 560, lineRule: LineRuleType.EXACT, before: 200 },
    children: [new TextRun({ text, font: HEITI, size: 32 })],
  });
}

/* 观点摘录（B2）：媒体观点 + 网友观点，与网页预览对齐 */
function addQuotes(report, children) {
  const vp = (report.overview || {}).viewpoints || {};
  const media = vp.media || [];
  const netizen = vp.netizen || [];
  const generic = (report.overview || {}).quotes || [];
  if (!media.length && !netizen.length && !generic.length) return;
  children.push(sectionHeading("观点摘录"));
  if (media.length) {
    children.push(bodyParagraph([bodyRun("媒体观点（机构立场）：", { bold: true })]));
    for (const q of media.slice(0, 3)) {
      const t = `【${stanceCN(q.stance)}】${q.media || ""}《${q.title || ""}》：${q.core_view || ""}`;
      children.push(bodyParagraph([bodyRun(t)]));
    }
  }
  if (netizen.length) {
    children.push(bodyParagraph([bodyRun("网友观点（大众原声样本，不作事实依据）：", { bold: true })]));
    for (const q of netizen.slice(0, 3)) {
      const t = `【${stanceCN(q.stance)}】“${q.text || ""}”（${q.platform || q.source || ""}）`;
      children.push(bodyParagraph([bodyRun(t)]));
    }
  }
  if (!media.length && !netizen.length && generic.length) {
    for (const q of generic.slice(0, 4)) {
      children.push(bodyParagraph([bodyRun(`【${stanceCN(q.stance)}】“${q.text || ""}”`)]));
    }
  }
}

/* 数据附录（D1 + 采集统计）：与网页预览对齐 */
function addAppendix(report, children) {
  const st = report.stats || {};
  const rows = [
    ["搜索原始结果", `${st.total_raw ?? "-"} 条`],
    ["去重后保留", `${st.total_after_dedupe ?? "-"} 条`],
    ["抓取到正文", `${st.body_fetched ?? "-"} 条`],
    ["可信度分布", `高 ${st.credibility_dist?.high ?? 0} / 中 ${st.credibility_dist?.mid ?? 0} / 低 ${st.credibility_dist?.low ?? 0}`],
    ["分析耗时", `${report.elapsed_sec ?? "-"} s`],
  ];
  const sc = report.source_check;
  if (sc && sc.summary) {
    const s = sc.summary;
    rows.push(["来源链接状态", `可达 ${s.ok ?? 0} · 失效 ${s.gone ?? 0} · 无法核实 ${s.unreachable ?? 0}（共 ${s.total ?? 0}）`]);
  }
  children.push(sectionHeading("数据附录"));
  for (const [k, v] of rows) {
    children.push(bodyParagraph([bodyRun(k + "：", { bold: true }), bodyRun(v)]));
  }
  if (sc && sc.summary && (sc.summary.gone || sc.summary.unreachable)) {
    const bad = Object.entries(sc.detail || {}).filter(([, v]) => v !== "ok").slice(0, 5);
    if (bad.length) {
      children.push(bodyParagraph([bodyRun("失效/待核链接：", { bold: true }), bodyRun(bad.map(([u]) => u).join(" · "))]));
    }
  }
}

function buildDoc(report) {
  const children = [];

  // 标题：22pt(44) 加粗 居中
  children.push(
    new Paragraph({
      alignment: AlignmentType.CENTER,
      spacing: { line: 560, lineRule: LineRuleType.EXACT },
      children: [new TextRun({ text: report.title, bold: true, size: 44, font: HEITI })],
    })
  );
  children.push(new Paragraph({ spacing: { after: 200 } }));

  // 引言
  if (report.intro) {
    children.push(bodyParagraph([bodyRun(report.intro)]));
  }

  // 章节
  for (const sec of report.sections || []) {
    children.push(sectionHeading(sec.heading));
    for (const para of sec.paragraphs || []) {
      if (typeof para === "string") {
        children.push(bodyParagraph([bodyRun(para)]));
      } else if (para && typeof para === "object") {
        // { lead: "（一）xxx。", body: "详细论述..." } 句首加粗
        const runs = [];
        if (para.lead) runs.push(bodyRun(para.lead, { bold: true }));
        if (para.body) runs.push(bodyRun(para.body));
        children.push(bodyParagraph(runs));
      }
    }
  }

  addQuotes(report, children);
  addAppendix(report, children);

  // 图表（P1-c）：报告 JSON 附 _chart_images=[{kind,path}] 时嵌入 PNG
  const chartImgs = report._chart_images || [];
  if (chartImgs.length) {
    children.push(sectionHeading("图表"));
    for (const img of chartImgs) {
      try {
        children.push(
          new Paragraph({
            alignment: AlignmentType.CENTER,
            children: [
              new ImageRun({
                type: "png",
                data: readFileSync(img.path),
                transformation: { width: 560, height: 240 },
              }),
            ],
          })
        );
      } catch (e) {
        console.warn("[gen_docx] 跳过无法读取的图表:", img.path, String(e).slice(0, 80));
      }
    }
  }

  return new Document({
    styles: { default: { document: { run: { font: FANGSONG, size: 32 } } } },
    sections: [{ children, properties: { page: { margin: { top: 1440, bottom: 1440, left: 1800, right: 1800 } } } }],
  });
}

const report = JSON.parse(readFileSync(inJson, "utf-8"));
Packer.toBuffer(buildDoc(report)).then((buf) => {
  writeFileSync(outDocx, buf);
  console.log(`OK 已生成: ${outDocx}`);
});