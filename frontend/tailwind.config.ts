import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // main accent (red) used for the primary actions
        brand: { 50: "#fef2f1", 100: "#fde3e1", 200: "#fbc9c5", 500: "#ec4a41", 600: "#e5322d", 700: "#c4231f", 800: "#a11f1c" },
      },
      boxShadow: { card: "0 1px 2px rgba(15,23,42,.06), 0 4px 14px rgba(15,23,42,.05)", lift: "0 6px 24px rgba(15,23,42,.12)" },
    },
  },
  plugins: [],
};
export default config;
