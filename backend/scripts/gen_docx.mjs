// 舆情报告 docx 生成器
// 严格对齐《参考格式.docx》模板规格（已从模板 XML 解析）：
//   标题   22pt 加粗 居中
//   章节   黑体 16pt（不加粗，黑体本身厚重）
//   正文   仿宋 16pt，首行缩进 2 字符(640twips)，两端对齐，固定行距 560/EXACT(28pt)
//   句首   分论点/对策短语加粗 + 后续正文
// 用法: node gen_docx.mjs <report.json> <out.docx>
import { readFileSync, writeFileSync } from "node:fs";
import { Document, Packer, Paragraph, TextRun, AlignmentType, LineRuleType } from "docx";

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
    children.push(
      new Paragraph({
        spacing: { line: 560, lineRule: LineRuleType.EXACT, before: 200 },
        children: [new TextRun({ text: sec.heading, font: HEITI, size: 32 })],
      })
    );
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