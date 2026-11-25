import type { ReactNode } from "react";

/** Small stroke icons (24x24). Drawn for this project; no external icon set. */
const PATHS: Record<string, ReactNode> = {
  upload: <><path d="M12 16V4m0 0L7 9m5-5 5 5" /><path d="M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3" /></>,
  folder: <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z" />,
  review: <><path d="M9 4h6l1 2h3a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h3l1-2z" /><path d="m9 13 2 2 4-4" /></>,
  check: <><circle cx="12" cy="12" r="9" /><path d="m8 12 3 3 5-6" /></>,
  alert: <><path d="M12 4 3 20h18L12 4z" /><path d="M12 10v4m0 3v.01" /></>,
  search: <><circle cx="11" cy="11" r="6" /><path d="m20 20-4.5-4.5" /></>,
  file: <><path d="M7 3h7l5 5v12a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z" /><path d="M14 3v5h5" /></>,
  sheet: <><rect x="4" y="4" width="16" height="16" rx="1.5" /><path d="M4 10h16M4 15h16M10 4v16" /></>,
  image: <><rect x="3" y="5" width="18" height="14" rx="2" /><circle cx="9" cy="10" r="1.5" /><path d="m4 18 5-5 4 4 3-3 4 4" /></>,
  close: <path d="m6 6 12 12M18 6 6 18" />,
  plus: <path d="M12 5v14M5 12h14" />,
  arrow: <path d="M5 12h14m-5-5 5 5-5 5" />,
};

export type IconName = keyof typeof PATHS;

export function Icon({ name, className = "h-6 w-6" }: { name: IconName; className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{PATHS[name]}</svg>
  );
}

/** Icon for a file by its extension. */
export function fileIcon(name: string): IconName {
  const ext = name.slice(name.lastIndexOf(".") + 1).toLowerCase();
  if (["xlsx", "xls", "csv"].includes(ext)) return "sheet";
  if (["jpg", "jpeg", "png", "tif", "tiff"].includes(ext)) return "image";
  return "file";
}
