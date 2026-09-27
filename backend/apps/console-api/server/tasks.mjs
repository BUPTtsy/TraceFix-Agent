export const seed = () => [
  {id: 1, title: 'Write project brief', status: 'Todo'},
  {id: 2, title: 'Review navigation flow', status: 'Todo'},
  {id: 3, title: 'Prepare release checklist', status: 'Done'},
  {id: 4, title: 'Update design tokens', status: 'Todo'},
  {id: 5, title: 'Schedule user interviews', status: 'Todo'},
  {id: 6, title: 'Archive old notes', status: 'Done'},
];
export function validateTitle(title) {
  if (typeof title !== 'string' || !title.trim()) throw new Error('标题不能为空');
  if (title.trim().length > 120) throw new Error('标题不能超过 120 个字符');
  return title.trim();
}
export function applyFields(task, fields) {
  const next = {...task};
  if ('title' in fields) next.title = validateTitle(fields.title);
  if ('status' in fields) {
    if (!['Todo', 'Done'].includes(fields.status)) throw new Error('状态无效');
    next.status = fields.status;
  }
  return next;
}
