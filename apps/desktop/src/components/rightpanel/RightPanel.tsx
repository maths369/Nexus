import {
  CheckCircle2,
  Circle,
  Loader2,
  AlertCircle,
  ChevronDown,
  ChevronRight,
  Zap,
  FolderOpen,
  FileText,
  Settings2,
} from "lucide-react";
import { useState } from "react";
import type { TaskStep, SkillInfo, WorkingFolder } from "../../types";
import "./RightPanel.css";

interface Props {
  taskSteps: TaskStep[];
  skills: SkillInfo[];
  workingFolders: WorkingFolder[];
}

export default function RightPanel({ taskSteps, skills, workingFolders }: Props) {
  return (
    <aside className="right-panel">
      <div className="right-panel-header titlebar-drag">
        <span />
      </div>

      <ProgressSection steps={taskSteps} />
      <WorkingFoldersSection folders={workingFolders} />
      <ContextSection skills={skills} />
    </aside>
  );
}

function ProgressSection({ steps }: { steps: TaskStep[] }) {
  const [open, setOpen] = useState(true);
  const completed = steps.filter((s) => s.status === "completed").length;
  const total = steps.length;
  const pct = total > 0 ? Math.round((completed / total) * 100) : 0;
  const hasActive = steps.some((s) => s.status === "running" || s.status === "pending");

  return (
    <div className="rp-section">
      <button className="rp-section-header" onClick={() => setOpen(!open)}>
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        <span>Progress</span>
        {total > 0 && (
          <span className="rp-progress-count">{completed}/{total}</span>
        )}
      </button>
      {open && (
        <div className="rp-section-body">
          {total > 0 && (
            <div className="rp-progress-bar-wrapper">
              <div
                className={`rp-progress-bar-fill ${hasActive ? "rp-progress-bar-active" : ""}`}
                style={{ width: `${pct}%` }}
              />
            </div>
          )}
          {steps.length === 0 && (
            <div className="rp-empty">No active tasks</div>
          )}
          {steps.map((step) => (
            <div key={step.id} className={`rp-step rp-step-${step.status}`}>
              <span className="rp-step-icon">
                {step.status === "completed" && (
                  <CheckCircle2 size={16} className="icon-blue" />
                )}
                {step.status === "running" && (
                  <Loader2 size={16} className="icon-blue spinning" />
                )}
                {step.status === "pending" && <Circle size={16} />}
                {step.status === "error" && (
                  <AlertCircle size={16} className="icon-red" />
                )}
              </span>
              <span className="rp-step-label">{step.label}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function WorkingFoldersSection({ folders }: { folders: WorkingFolder[] }) {
  const [open, setOpen] = useState(true);

  return (
    <div className="rp-section">
      <button className="rp-section-header" onClick={() => setOpen(!open)}>
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        <span>Working Folders</span>
      </button>
      {open && (
        <div className="rp-section-body">
          {folders.length === 0 && (
            <div className="rp-empty">No connected folders</div>
          )}
          {folders.map((f) => (
            <div key={f.path} className="rp-folder-item">
              <FolderOpen size={14} className="icon-folder" />
              <div className="rp-folder-info">
                <div className="rp-folder-name-row">
                  <span className="rp-folder-name">{f.name}</span>
                  <span className={`rp-folder-source rp-folder-source-${f.source}`}>
                    {f.source === "hub" ? "Hub" : "Mac"}
                  </span>
                </div>
                <div className="rp-folder-path" title={f.path}>{f.path}</div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function ContextSection({ skills }: { skills: SkillInfo[] }) {
  const [open, setOpen] = useState(true);

  return (
    <div className="rp-section">
      <button className="rp-section-header" onClick={() => setOpen(!open)}>
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        <span>Context</span>
      </button>
      {open && (
        <div className="rp-section-body">
          <div className="rp-context-group">
            <div className="rp-context-label">
              <Settings2 size={13} /> Connectors
            </div>
            <div className="rp-context-item">
              <Zap size={13} className="icon-blue" /> Web search
            </div>
          </div>
          {skills.length > 0 && (
            <div className="rp-context-group">
              <div className="rp-context-label">
                <Settings2 size={13} /> Skills
              </div>
              {skills.map((s) => (
                <div key={s.id} className="rp-context-item">
                  <Zap size={13} className={s.active ? "icon-green" : ""} />
                  <span>{s.name}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
