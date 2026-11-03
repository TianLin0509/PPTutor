"""Validate model references and verbatim evidence; render concise Markdown."""
from pathlib import Path
from . import VERSION
from .library import PptutorError
from .assessment import assess, required_text, validate_task, validate_quote

def cell(value):
    return str(value or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('|', '\\|').replace('\r', '').replace('\n', '<br>')

def local_link(path, page, label=None):
    normalized = str(Path(path).absolute()).replace('\\','/')
    # Labels and destinations must not let source filenames alter table layout.
    label = str(label or Path(path).name).replace('[','\\[').replace(']','\\]').replace('|','\\|').replace('\n',' ')
    if Path(path).is_file():
        target = normalized.replace('<','%3C').replace('>','%3E')
        return f'[{label}](<{target}>) · P{page}'
    return f'{cell(label)} · P{page}（原文件不可用）'

def prepare(decision, evidence):
    """Apply deterministic condition gates without inventing semantic scores."""
    if not evidence.get('ok') or evidence.get('truncated'):
        raise PptutorError('INCOMPLETE_EVIDENCE', '需要成功的完整页读取结果。')
    pages = {r['id']: r for r in evidence.get('pages', [])}
    if any(p.get('text_truncated', True) for p in pages.values()):
        raise PptutorError('INCOMPLETE_EVIDENCE', '逐条件核对需要完整页正文。')
    task = validate_task(decision)
    rows = decision.get('results', [])
    if not isinstance(rows,list) or len(rows)>20:
        raise PptutorError('INVALID_REPORT', 'results 应为最多 20 条的数组。')
    seen = set(); checked = []
    for row in rows:
        if not isinstance(row, dict):
            raise PptutorError('INVALID_REPORT', '每条结果应为对象。')
        row = dict(row)
        rid = row.get('id')
        if rid not in pages or rid in seen:
            raise PptutorError('UNREAD_REFERENCE', f'结果页 {rid!r} 未读取或重复。')
        seen.add(rid)
        validate_quote(rid, row.get('quote'), pages)
        level = row.get('relevance')
        if level not in ('直接适用','相关背景','待核查','暂不推荐'):
            raise PptutorError('INVALID_RELEVANCE', '契合程度使用：直接适用、相关背景、待核查、暂不推荐。')
        for key in ('use', 'reason', 'conditions'):
            required_text(row.get(key), key)
        row['effective_relevance'] = assess(row, task, pages)
        checked.append((row,pages[rid]))
    answer = decision.get('answer_status','supported' if rows else 'insufficient')
    if answer not in ('supported','partial','insufficient') or (answer=='supported' and not rows):
        raise PptutorError('INVALID_REPORT', '答案状态与结果不一致。')
    # Preserve host-model relevance order within each eligibility group.
    order = {'直接适用': 0, '相关背景': 1, '待核查': 2, '条件不符': 3}
    checked.sort(key=lambda pair: order[pair[0]['effective_relevance']])
    usable = [(r,p) for r,p in checked if r['effective_relevance'] != '条件不符']
    excluded = [(r,p) for r,p in checked if r['effective_relevance'] == '条件不符']
    direct = sum(r['effective_relevance'] == '直接适用' for r,p in usable)
    adjusted = any(r['effective_relevance'] != r['relevance'] and
                   not (r['relevance'] == '暂不推荐' and r['effective_relevance'] == '条件不符')
                   for r,p in checked)
    if not direct:
        answer = 'partial' if any(r['effective_relevance'] == '待核查' for r,p in usable) else 'insufficient'
    elif answer == 'insufficient':
        raise PptutorError('INVALID_REPORT', 'insufficient 不能同时包含直接适用的素材。')
    adjusted = adjusted or answer != decision.get('answer_status', answer)
    return {'task': task, 'pages': pages, 'usable': usable, 'excluded': excluded,
            'answer_status': answer, 'adjusted': adjusted}


def condition_brief(row, task):
    names = {c['id']: c for c in task['conditions']}
    parts = [f"{names[c['condition_id']]['text']}：{c['status']}" for c in row['assessments']
             if c['status'] != '满足']
    return '；'.join(parts) if parts else ('所列条件已获文字支持' if names else '按主题解释与素材互补性筛选')


def condition_overview(row, task):
    if not task['conditions']:
        return '按主题筛选'
    required = {c['id']: c['required'] for c in task['conditions']}
    issues = {}
    for c in row['assessments']:
        if c['status'] != '满足':
            key = ('关键条件' if required[c['condition_id']] else '偏好') + c['status']
            issues[key] = issues.get(key, 0) + 1
    return '；'.join(f'{count}项{key}' for key,count in issues.items()) if issues else '条件符合'


def render(decision, evidence, detail='brief'):
    if detail not in ('brief', 'full'):
        raise PptutorError('INVALID_REPORT', 'detail 使用 brief 或 full。')
    prepared = prepare(decision, evidence)
    task, pages = prepared['task'], prepared['pages']
    checked, excluded = prepared['usable'], prepared['excluded']
    title = str(decision.get('title','素材检索')).replace('\n',' ')[:100]
    lines = [f'## PPTutor · {title}', f'版本 {VERSION} · 纯文本语义检索', '',
             '**本次用途：** ' + cell(task['purpose']), '']
    hard = [c['text'] for c in task['conditions'] if c['required']]
    if hard:
        lines += ['**关键条件：** ' + '；'.join(cell(t) for t in hard), '']
    labels = ('直接适用','相关背景','待核查')
    counts = {label: sum(r['effective_relevance'] == label for r,p in checked) for label in labels}
    count_text = '；'.join(f'{label} {counts[label]} 页' for label in labels if counts[label])
    lines += [('**筛选结果：** ' + (count_text or '暂无可推荐素材') +
               (f'；另有 {len(excluded)} 页因关键条件冲突暂不推荐。' if excluded else '。')), '']
    if decision.get('summary') and not prepared['adjusted']:
        lines += [cell(decision['summary']), '']
    elif prepared['adjusted']:
        lines += ['以下使用判断已按关键条件核对结果调整。', '']
    if checked:
        lines += ['| 推荐 | 素材与页码 | 为什么适合 | 使用判断 |',
                  '|:---:|---|---|---|']
        for i,(row,page) in enumerate(checked,1):
            condition = row['effective_relevance'] + '；' + condition_overview(row, task)
            if page['source_state']!='metadata_matches_index':
                condition += '；原文件已变化或不可用'
            lines.append('| ' + ' | '.join([str(i),local_link(page['ref']['path'],page['ref']['page'],row.get('label')),
                cell(row['reason']),cell(condition)]) + ' |')
        lines.append('')
    else:
        lines += ['本次已检索范围内，没有找到足以支撑该要求的文字素材。', '']
    if excluded:
        lines += ['**暂不推荐：**', '']
        for row,page in excluded:
            lines += ['- ' + local_link(page['ref']['path'],page['ref']['page'],row.get('label')) + '：' + cell(condition_brief(row, task))]
        lines.append('')
    if checked and detail == 'brief':
        lines += ['**原文依据：**', '']
        for i,(row,page) in enumerate(checked,1):
            lines.append(f"- **{i} · P{page['ref']['page']}：** {cell(row['quote'])} — {cell(row['conditions'])}")
        lines.append('')
    if (checked or excluded) and detail == 'full':
        lines += ['### 原文与条件依据', '']
        names = {c['id']: c for c in task['conditions']}
        for i,(row,page) in enumerate(checked + excluded,1):
            name = row.get('label') or page['ref']['name']
            prefix = str(i) if i <= len(checked) else '暂不推荐'
            lines += [f"**{prefix} · {cell(name)} · P{page['ref']['page']}**", '',
                      '**用途：** ' + cell(row['use']), '',
                      f"**本页原文：** {cell(row['quote'])}", '',
                      '**使用边界：** ' + cell(row['conditions']), '']
            for check in row['assessments']:
                c = names[check['condition_id']]
                content = f"{cell(c['text'])}：{check['status']} — {cell(check['reason'])}"
                citations = []
                for ref in check.get('evidence',[]):
                    p = pages[ref['id']]
                    source = '本页' if ref['id'] == row['id'] else '上下文 P' + str(p['ref']['page'])
                    citations.append(f"{source}原文：{cell(ref['quote'])}")
                if citations:
                    content += '；' + '；'.join(citations)
                lines.append('- ' + content)
            if row['assessments']:
                lines.append('')
    if checked or excluded:
        sources = list(dict.fromkeys(p['ref']['path'] for _,p in checked + excluded))
        lines += ['**来源文稿：** ' + '；'.join(cell(Path(p).name) for p in sources), '']
    if decision.get('gaps'):
        lines += ['**素材缺口：** ' + '；'.join(str(x).rstrip('。；; ') for x in decision['gaps']), '']
    if decision.get('search_note') and detail == 'full':
        lines += ['**检索说明：** ' + str(decision['search_note']), '']
    scope = evidence.get('scope',{})
    roots = '、'.join(scope.get('roots') or []) or '当前所选索引'
    lines += [f'**范围：** {roots}；格式 {", ".join(scope.get("extensions",[]))}。仅核对已提取文字；图片、图表和视觉质量留待 P2 核查。', '']
    return '\n'.join(lines)
