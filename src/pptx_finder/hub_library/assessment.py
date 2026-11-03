"""Check task conditions and citations; the host remains responsible for meaning."""
from .library import PptutorError
from .text import comparable


def required_text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise PptutorError('INVALID_REPORT', f'{field} 需要非空文字。')
    return value.strip()


def validate_task(decision):
    task = decision.get('task')
    if not isinstance(task, dict):
        raise PptutorError('MISSING_TASK', '需要 task.purpose 和 task.conditions；只给主题时条件数组可空。')
    purpose = required_text(task.get('purpose'), 'task.purpose')
    conditions = task.get('conditions')
    if not isinstance(conditions, list) or len(conditions) > 12:
        raise PptutorError('INVALID_CONDITIONS', 'task.conditions 应为最多 12 项的数组。')
    ids = set()
    for condition in conditions:
        if not isinstance(condition, dict):
            raise PptutorError('INVALID_CONDITIONS', '每个条件需要 id、text、required。')
        cid = required_text(condition.get('id'), '条件 id')
        required_text(condition.get('text'), '条件 text')
        if cid in ids or not isinstance(condition.get('required'), bool):
            raise PptutorError('INVALID_CONDITIONS', '条件 id 应唯一，required 应为布尔值。')
        ids.add(cid)
    return {'purpose': purpose, 'conditions': conditions}


def validate_quote(rid, quote, pages):
    if rid not in pages:
        raise PptutorError('UNREAD_REFERENCE', f'证据页 {rid!r} 尚未完整读取。')
    if not isinstance(quote, str) or len(quote.strip()) < 4 or comparable(quote) not in comparable(pages[rid]['text']):
        raise PptutorError('UNSUPPORTED_QUOTE', f'{rid} 的原文依据不是已读取文字的连续片段。')


def assess(row, task, pages):
    checks = row.get('assessments')
    if not isinstance(checks, list):
        raise PptutorError('MISSING_ASSESSMENTS', '每页需要 assessments；任务无条件时使用空数组。')
    expected = {c['id']: c for c in task['conditions']}
    seen = set()
    for check in checks:
        if not isinstance(check, dict) or check.get('condition_id') not in expected or check['condition_id'] in seen:
            raise PptutorError('INVALID_ASSESSMENTS', '逐条件判断应与任务条件一一对应。')
        seen.add(check['condition_id'])
        if check.get('status') not in ('满足', '冲突', '未证实'):
            raise PptutorError('INVALID_ASSESSMENTS', '条件判断使用：满足、冲突、未证实。')
        required_text(check.get('reason'), '条件判断 reason')
        refs = check.get('evidence', [])
        if not isinstance(refs, list) or (check['status'] != '未证实' and not refs):
            raise PptutorError('MISSING_CONDITION_EVIDENCE', '满足或冲突需要明确的已读原文依据。')
        for ref in refs:
            if not isinstance(ref, dict):
                raise PptutorError('INVALID_ASSESSMENTS', '条件依据需要 id 和 quote。')
            validate_quote(ref.get('id'), ref.get('quote'), pages)
            if (pages[ref['id']]['ref']['file_id'], pages[ref['id']]['ref'].get('source_id')) != (pages[row['id']]['ref']['file_id'], pages[row['id']]['ref'].get('source_id')):
                raise PptutorError('CROSS_DOCUMENT_EVIDENCE', '其他文稿的条件不能作为本素材的条件证据。')
    if seen != set(expected):
        raise PptutorError('MISSING_ASSESSMENTS', '每页应判断全部任务条件，包括未证实的条件。')
    hard = [c for c in checks if expected[c['condition_id']]['required']]
    if any(c['status'] == '冲突' for c in hard):
        return '条件不符'
    if row['relevance'] == '暂不推荐':
        raise PptutorError('INVALID_ASSESSMENTS', '暂不推荐需要明确的关键条件冲突。')
    if any(c['status'] == '未证实' for c in hard) and row['relevance'] == '直接适用':
        return '待核查'
    return row['relevance']
