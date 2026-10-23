export function selectedProjectsFromEnvironment(value) {
  return new Set(String(value ?? '').split('|').map((item) => item.trim()).filter(Boolean));
}

export function selectedTaskIdsFromEnvironment(value) {
  return new Set(String(value ?? '').split('|').map((item) => item.trim()).filter(Boolean));
}

export function filterTasksByProjects(tasks, projects) {
  if (!projects || projects.size === 0) return tasks;
  return tasks.filter((task) => projects.has(String(task.project ?? '').trim()));
}

export function filterTasksByIds(tasks, taskIds) {
  if (!taskIds || taskIds.size === 0) return tasks;
  return tasks.filter((task) => taskIds.has(String(task.id ?? '').trim()));
}
