import type { Metadata } from "next";
import AppHeader from "@/components/AppHeader";
import "./globals.css";

export const metadata: Metadata = {
  applicationName: "Донор",
  title: "Донор — презентации по вашему образцу",
  description: "Создание и проверка презентаций по загруженному шаблону",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="ru" data-scroll-behavior="smooth">
      <body>
        <AppHeader />
        <main className="shell" id="main-content">{children}</main>
      </body>
    </html>
  );
}
