import type { DropboxDomain } from "./types";

export const domainLabels: Record<string, string> = {
  global: "Global",
  personal: "Personal",
  "maestro-development": "Maestro Development",
  praxis: "Praxis",
  usma: "USMA",
  "perti-laboratories": "Perti Laboratories",
  l3: "L3",
};

export const dropboxDomainDefaults: DropboxDomain[] = Object.keys(domainLabels).map((key) => ({
  key,
  inbox: 0,
  processing: 0,
  processed: 0,
  failed: 0,
  previews: 0,
}));

export const domains = [
  "Personal",
  "Maestro Development",
  "Praxis",
  "USMA",
  "Perti Laboratories",
  "L3",
];

export const domainKeysByLabel: Record<string, string> = Object.fromEntries(
  Object.entries(domainLabels).map(([key, label]) => [label, key]),
);

export const routedGroups = [
  { key: "human_input", label: "RFIs", empty: "No open RFIs." },
  { key: "task", label: "Tasks", empty: "No open tasks." },
  { key: "event", label: "Events", empty: "No extracted events." },
  { key: "contact", label: "Contacts", empty: "No extracted contacts." },
  { key: "decision_log", label: "Decisions", empty: "No recent decisions." },
];

export const hiddenRoutedStatuses = new Set(["done", "archived"]);
