/** V1.1 术语回归测试：用户可见 UI 文案不得再出现开发阶段术语。

策略：**精确匹配 Audit §6 记录的泄漏文案**（含 Stage 4/5/6A 的完整用户句子），
而不是禁止内部状态值 token——`current_phase === 'waiting_manual'` 这类内部
比较值可以且应该继续存在（code 层），只有渲染给用户的文案必须产品化。

匹配方式：读取每个 .tsx 组件源码，剥离注释后，在**字符串字面量与 JSX 文本**
中查找泄漏短语（词序精确）。内部比较值（如 'stage4'）不在禁止列表。

实现不依赖 node:fs（避免引入 @types/node）：用 Vite `import.meta.glob` 惰性
加载全部 .tsx 源码。
 */

import { describe, expect, it } from 'vitest';

/** Audit §6 记录的用户可见泄漏文案（精确短语，匹配即失败）。 */
const FORBIDDEN_PHRASES: string[] = [
  // Stage 阶段号泄漏（完整用户句子）。
  'Stage 6A 只支持单个研究问题',
  'Stage 6A 不包含自动 Source Planning',
  '请显式填写 Stage 4 work plan',
  'Stage 4 分析',
  'Stage 5 报告',
  '未执行 Stage 4',
  '未执行 Stage 5',
  '未执行 Stage 5 审核',
  'Stage 4 工作项',
  // 开发/工程概念直接上屏。
  '自动研究编排',
  '工作流进度',
  '待处理操作',
  '节点完成',
  '节点：',
  '缺失需求代码',
  '研究回流',
  '回填轮次',
  'Deterministic Check',
  'ReviewAction',
  'Human Review',
  'Research Backflow',
  'Schema 版本',
  'item: ',
  '尚未添加执行工作项',
  '添加工作项',
  '工作项 ',
];

/** 惰性加载全部 .tsx 源码（本测试文件自身 .test.tsx 除外）。 */
const tsxSources = import.meta.glob('../**/*.tsx', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>;

/** 剥离注释（行注释与块注释），保留字符串/JSX 文本区域供匹配。 */
function stripComments(source: string): string {
  return source
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/\/\/[^\n]*/g, '')
    .replace(/<!--[\s\S]*?-->/g, '');
}

/** 拼接全部字符串字面量（单/双引号与 template literal）。 */
function stringContents(source: string): string {
  const hits: string[] = [];
  const stringRe = /(['"`])((?:\\.|(?!\1)[^\\])*?)\1/g;
  for (const match of source.matchAll(stringRe)) {
    hits.push(match[2]);
  }
  return hits.join('\n');
}

describe('user-visible terminology regression (V1.1)', () => {
  const files = Object.keys(tsxSources)
    .filter((path) => !path.endsWith('.test.tsx'))
    .sort();

  it('收集到组件文件', () => {
    expect(files.length).toBeGreaterThan(20);
  });

  for (const file of files) {
    const source = stripComments(tsxSources[file]);
    const contents = stringContents(source);
    for (const phrase of FORBIDDEN_PHRASES) {
      if (contents.includes(phrase)) {
        it(`${file} 不包含用户可见「${phrase}」`, () => {
          expect(contents).not.toContain(phrase);
        });
      }
    }
  }
});
