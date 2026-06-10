import {
  Bot,
  CalendarDays,
  CheckCircle2,
  ChevronRight,
  Clock3,
  GripVertical,
  Menu,
  MessageSquareText,
  MoreHorizontal,
  PanelLeftClose,
  Plus,
  Settings,
  ShieldCheck,
  Sparkles,
  Wrench,
} from "lucide-react";
import { useMemo, useState } from "react";

type PlannerItem = {
  id: number;
  time: string;
  title: string;
  domain: string;
  status: "locked" | "flex" | "needs-input";
  priority: "high" | "medium" | "low";
};

const domains = [
  "Personal",
  "Maestro Development",
  "Praxis",
  "Ophi",
  "USMA",
  "Personal IRAD",
  "L3",
];

const initialPlannerItems: PlannerItem[] = [
  {
    id: 1,
    time: "08:00",
    title: "Daily standup synthesis",
    domain: "Maestro",
    status: "locked",
    priority: "high",
  },
  {
    id: 2,
    time: "09:30",
    title: "Review Praxis follow-up candidates",
    domain: "Praxis",
    status: "needs-input",
    priority: "high",
  },
  {
    id: 3,
    time: "11:00",
    title: "USMA prep block",
    domain: "USMA",
    status: "flex",
    priority: "medium",
  },
  {
    id: 4,
    time: "14:00",
    title: "Maestro implementation window",
    domain: "Maestro Development",
    status: "flex",
    priority: "high",
  },
];

const reports = [
  {
    title: "Morning standup",
    summary: "Awaiting first workflow run.",
    meta: "Maestro / Today",
  },
  {
    title: "Memory curator",
    summary: "Seed package ingestion not configured yet.",
    meta: "Admin / Pending",
  },
  {
    title: "Praxis brief",
    summary: "Domain agent stub will populate this panel.",
    meta: "Praxis / Stub",
  },
];

const agents = [
  "Personal Chief of Staff",
  "Maestro CTO",
  "Praxis CGO",
  "Ophi Research",
  "USMA Teaching",
  "IRAD Project Planner",
];

function statusLabel(status: PlannerItem["status"]) {
  if (status === "locked") return "Locked";
  if (status === "needs-input") return "Needs input";
  return "Flexible";
}

function nextStatus(status: PlannerItem["status"]): PlannerItem["status"] {
  if (status === "locked") return "flex";
  if (status === "flex") return "needs-input";
  return "locked";
}

export function App() {
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [activeDomain, setActiveDomain] = useState("Maestro");
  const [plannerItems, setPlannerItems] = useState(initialPlannerItems);
  const [draftMessage, setDraftMessage] = useState("");

  const highPriorityCount = useMemo(
    () => plannerItems.filter((item) => item.priority === "high").length,
    [plannerItems],
  );

  const moveItem = (id: number, direction: -1 | 1) => {
    setPlannerItems((items) => {
      const index = items.findIndex((item) => item.id === id);
      const target = index + direction;
      if (index < 0 || target < 0 || target >= items.length) return items;
      const next = [...items];
      [next[index], next[target]] = [next[target], next[index]];
      return next;
    });
  };

  const cycleItemStatus = (id: number) => {
    setPlannerItems((items) =>
      items.map((item) => (item.id === id ? { ...item, status: nextStatus(item.status) } : item)),
    );
  };

  return (
    <main className="app-shell">
      <aside className={sidebarOpen ? "sidebar" : "sidebar sidebar-closed"}>
        <div className="brand-row">
          <div>
            <p className="eyebrow">Local command</p>
            <h1>Maestro</h1>
          </div>
          <button
            className="icon-button"
            aria-label={sidebarOpen ? "Collapse sidebar" : "Open sidebar"}
            title={sidebarOpen ? "Collapse sidebar" : "Open sidebar"}
            onClick={() => setSidebarOpen((value) => !value)}
          >
            {sidebarOpen ? <PanelLeftClose size={18} /> : <Menu size={18} />}
          </button>
        </div>

        <nav className="domain-nav" aria-label="Domains">
          <button
            className={activeDomain === "Maestro" ? "domain-button active" : "domain-button"}
            onClick={() => setActiveDomain("Maestro")}
          >
            <Sparkles size={17} />
            <span>Maestro</span>
          </button>
          {domains.map((domain) => (
            <button
              key={domain}
              className={activeDomain === domain ? "domain-button active" : "domain-button"}
              onClick={() => setActiveDomain(domain)}
            >
              <ChevronRight size={16} />
              <span>{domain}</span>
            </button>
          ))}
        </nav>

        <div className="sidebar-footer">
          <button className="domain-button">
            <Settings size={17} />
            <span>Settings</span>
          </button>
        </div>
      </aside>

      <section className="workspace">
        <header className="topbar">
          <div>
            <p className="eyebrow">Active surface</p>
            <h2>{activeDomain}</h2>
          </div>
          <div className="status-strip" aria-label="Runtime status">
            <span>
              <ShieldCheck size={16} />
              Local
            </span>
            <span>
              <Clock3 size={16} />
              {highPriorityCount} high priority
            </span>
          </div>
        </header>

        <div className="workspace-grid">
          <section className="chat-panel" aria-labelledby="chat-heading">
            <div className="section-heading">
              <div>
                <p className="eyebrow">Maestro chat</p>
                <h3 id="chat-heading">Command thread</h3>
              </div>
              <button className="icon-button" aria-label="New thread" title="New thread">
                <Plus size={18} />
              </button>
            </div>

            <div className="thread">
              <div className="message user-message">
                <span>You</span>
                <p>Build today around the morning standup and keep the plan adjustable.</p>
              </div>
              <div className="message maestro-message">
                <span>Maestro</span>
                <p>
                  Daily planner is ready as a stub. The standup workflow will populate this with
                  schedule, tasks, blockers, and recommended tradeoffs.
                </p>
              </div>
            </div>

            <form className="composer" onSubmit={(event) => event.preventDefault()}>
              <MessageSquareText size={18} />
              <input
                value={draftMessage}
                onChange={(event) => setDraftMessage(event.target.value)}
                placeholder="Tell Maestro what changed..."
                aria-label="Message Maestro"
              />
              <button type="submit">Send</button>
            </form>
          </section>

          <section className="planner-panel" aria-labelledby="planner-heading">
            <div className="section-heading">
              <div>
                <p className="eyebrow">Daily planner</p>
                <h3 id="planner-heading">Today</h3>
              </div>
              <button className="planner-action">
                <CalendarDays size={17} />
                Adjust
              </button>
            </div>

            <div className="timeline">
              {plannerItems.map((item, index) => (
                <article className="timeline-item" key={item.id}>
                  <div className="time-column">
                    <span>{item.time}</span>
                    <GripVertical size={16} />
                  </div>
                  <div className="timeline-body">
                    <div className="timeline-title-row">
                      <div>
                        <h4>{item.title}</h4>
                        <p>{item.domain}</p>
                      </div>
                      <button
                        className={`status-pill ${item.status}`}
                        onClick={() => cycleItemStatus(item.id)}
                      >
                        {statusLabel(item.status)}
                      </button>
                    </div>
                    <div className="timeline-controls">
                      <button onClick={() => moveItem(item.id, -1)} disabled={index === 0}>
                        Earlier
                      </button>
                      <button
                        onClick={() => moveItem(item.id, 1)}
                        disabled={index === plannerItems.length - 1}
                      >
                        Later
                      </button>
                      <span className={`priority priority-${item.priority}`}>
                        {item.priority} priority
                      </span>
                    </div>
                  </div>
                </article>
              ))}
            </div>
          </section>

          <section className="reports-panel" aria-labelledby="reports-heading">
            <div className="section-heading">
              <div>
                <p className="eyebrow">Recent reports</p>
                <h3 id="reports-heading">Queue</h3>
              </div>
              <button className="icon-button" aria-label="Report menu" title="Report menu">
                <MoreHorizontal size={18} />
              </button>
            </div>
            <div className="report-list">
              {reports.map((report) => (
                <article className="report-card" key={report.title}>
                  <span>{report.meta}</span>
                  <h4>{report.title}</h4>
                  <p>{report.summary}</p>
                </article>
              ))}
            </div>
          </section>

          <section className="domain-panel" aria-labelledby="domain-heading">
            <div className="section-heading">
              <div>
                <p className="eyebrow">Domain page</p>
                <h3 id="domain-heading">Agents & tools</h3>
              </div>
              <button className="icon-button" aria-label="Add agent" title="Add agent">
                <Bot size={18} />
              </button>
            </div>

            <div className="agent-list">
              {agents.map((agent) => (
                <button className="agent-row" key={agent}>
                  <span>
                    <Bot size={17} />
                    {agent}
                  </span>
                  <CheckCircle2 size={17} />
                </button>
              ))}
            </div>

            <div className="tool-shell">
              <div>
                <Wrench size={18} />
                <span>Tool credentials and descriptions will open here.</span>
              </div>
              <button className="planner-action">Configure</button>
            </div>
          </section>
        </div>
      </section>
    </main>
  );
}
