import type { Project } from "../lib/types";

export function ProjectPicker({
  projects,
  value,
  onChange,
  disabled,
  label = "当前项目",
}: {
  projects: Project[];
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
  label?: string;
}) {
  return (
    <label className="project-picker">
      <span>{label}</span>
      <select value={value} onChange={(event) => onChange(event.target.value)} disabled={disabled || !projects.length}>
        {!projects.length ? <option value="">暂无项目</option> : null}
        {projects.map((project) => (
          <option value={project.id} key={project.id}>
            {project.name}
          </option>
        ))}
      </select>
    </label>
  );
}
