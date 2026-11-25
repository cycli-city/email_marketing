import { Activity, Plug, ShieldCheck, type LucideIcon } from "lucide-react";

export interface NavItem {
  title: string;
  href: string;
  icon: LucideIcon;
}

export interface NavGroup {
  label: string;
  items: NavItem[];
}

/**
 * Navigation grows page by page as real, working screens land (no dead links).
 * The full target list is in REQUIREMENTS.md §10 and REQUIREMENTS_AUTONOMY.md §H.
 */
export const NAV_GROUPS: NavGroup[] = [
  {
    label: "Operations",
    items: [{ title: "System", href: "/system", icon: Activity }],
  },
  {
    label: "Settings",
    items: [
      { title: "Integrations", href: "/settings/integrations", icon: Plug },
      { title: "Sending", href: "/settings/sending", icon: ShieldCheck },
    ],
  },
];
